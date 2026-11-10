"""Named-combo editing: choose round items by merit, then keep snapshots in sync.

Two problems this module addresses.

1. **No silent zero-price default.** When the caller expands a named combo, the
   first zero-price choice of each round is not automatically the best pick. The
   ordering here combines explicit preferences, recent-side history and price
   difference, and it reports *why* each option was ordered where it is. It never
   invents a SKU: only choices present in the round can be selected.

2. **Swap must not desynchronise pending and calc.** After the user changes a
   side or drink, the pending snapshot, the ``calculate-price`` payload and the
   displayed summary all have to describe the same ``roundList`` and the same
   price state. ``sync_snapshots`` derives the pending view from the calc
   snapshot with one renderer (``quote_state``) and refuses to merge when the
   payloads differ.

Nothing here calls MCP, prices an order, or writes user history.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import quote_state

CST = timezone(timedelta(hours=8))

# Recent sides/drinks push an option down, but never remove it: a small sample is
# not a preference.
DEFAULT_RECENT_PENALTY = 0.55
DEFAULT_ZERO_PRICE_BONUS = 0.0


@dataclass
class ChoiceOrder:
    code: str
    name: str
    diff_fen: int
    is_default: bool
    preferred: bool
    recent: bool
    score: float
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "diffFen": self.diff_fen,
            "isDefault": self.is_default,
            "preferred": self.preferred,
            "recent": self.recent,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }


def _norm(value: Any) -> str:
    return str(value or "").strip()


def order_choices(
    choices: Sequence[Mapping[str, Any]],
    *,
    prefer_codes: Sequence[str] = (),
    recent_codes: Sequence[str] = (),
    round_name: str = "",
    recent_weight: float = DEFAULT_RECENT_PENALTY,
    zero_price_bonus: float = DEFAULT_ZERO_PRICE_BONUS,
) -> List[ChoiceOrder]:
    """Rank the options of one round; never drops an option.

    Ordering (highest score first):

    * explicit ``prefer_codes`` — the caller's stated preference
    * not recently eaten on the same track — mild, bounded
    * lower ``diffFen`` — a tie-breaker, not the primary rule
    * the round's declared default — informational only

    ``zero_price_bonus`` defaults to 0 so a zero-difference option does not win
    by being free; a caller that genuinely wants the cheapest can raise it.
    """
    prefer = [_norm(code) for code in prefer_codes if _norm(code)]
    recent = {_norm(code) for code in recent_codes if _norm(code)}
    ordered: List[ChoiceOrder] = []
    for raw in choices or []:
        if not isinstance(raw, Mapping):
            continue
        code = _norm(raw.get("code") or raw.get("productCode"))
        if not code:
            continue
        try:
            diff = int(raw.get("diffFen") or 0)
        except (TypeError, ValueError):
            diff = 0
        name = _norm(raw.get("name") or raw.get("productName"))
        is_default = bool(raw.get("isDefault")) or str(raw.get("isDefault")) == "1"

        score = 0.0
        reasons: List[str] = []
        preferred = code in prefer
        if preferred:
            # Earlier preference wins, but never so large it hides history.
            score += 10.0 - prefer.index(code) * 0.5
            reasons.append(f"preferred:{prefer.index(code)}")
        if code in recent:
            score -= abs(recent_weight) * 5.0
            reasons.append("recent_on_track")
        else:
            reasons.append("not_recent")
        if diff == 0 and zero_price_bonus:
            score += zero_price_bonus
            reasons.append("zero_diff_bonus")
        elif diff == 0:
            reasons.append("zero_diff_no_bonus")
        # Cheaper options break ties only; 1 fen ≈ 0.001 score.
        score -= diff / 1000.0
        if is_default:
            reasons.append("round_default")
        ordered.append(
            ChoiceOrder(
                code=code,
                name=name,
                diff_fen=diff,
                is_default=is_default,
                preferred=preferred,
                recent=code in recent,
                score=score,
                reasons=reasons + ([f"round:{round_name}"] if round_name else []),
            )
        )
    ordered.sort(key=lambda item: (-item.score, item.diff_fen, item.code))
    return ordered


def pick_choice(
    choices: Sequence[Mapping[str, Any]],
    *,
    prefer_codes: Sequence[str] = (),
    recent_codes: Sequence[str] = (),
    round_name: str = "",
    recent_weight: float = DEFAULT_RECENT_PENALTY,
    zero_price_bonus: float = DEFAULT_ZERO_PRICE_BONUS,
) -> Optional[ChoiceOrder]:
    ranked = order_choices(
        choices,
        prefer_codes=prefer_codes,
        recent_codes=recent_codes,
        round_name=round_name,
        recent_weight=recent_weight,
        zero_price_bonus=zero_price_bonus,
    )
    return ranked[0] if ranked else None


def _rounds_of(detail: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    rounds = detail.get("rounds") or detail.get("roundList") or []
    return [rnd for rnd in rounds if isinstance(rnd, Mapping)]


def _round_choices(rnd: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    raw = rnd.get("choices") or rnd.get("comboItemList") or rnd.get("products") or []
    return [choice for choice in raw if isinstance(choice, Mapping)]


def _choice_diff(choice: Mapping[str, Any]) -> int:
    if choice.get("diffFen") is not None:
        try:
            return int(choice.get("diffFen") or 0)
        except (TypeError, ValueError):
            return 0
    raw = str(choice.get("diffPrice") or "")
    digits = "".join(ch for ch in raw if ch.isdigit() or ch == ".")
    if not digits:
        return 0
    try:
        value = float(digits)
    except ValueError:
        return 0
    return -int(round(value * 100)) if "-" in raw else int(round(value * 100))


def plan_named_combo(
    detail: Mapping[str, Any],
    *,
    prefer_by_round: Optional[Mapping[str, Sequence[str]]] = None,
    recent_by_round: Optional[Mapping[str, Sequence[str]]] = None,
    keep: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Propose one option per round for a named product (read-only).

    ``keep`` pins a round to a code the user explicitly asked for; pinned codes
    must exist in that round. Returns the proposed ``roundList`` plus per-round
    ranking so the caller can show alternatives.
    """
    prefer_by_round = prefer_by_round or {}
    recent_by_round = recent_by_round or {}
    keep = {_norm(k): _norm(v) for k, v in (keep or {}).items() if _norm(v)}
    rounds = _rounds_of(detail)
    round_list: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []
    base_diff = 0
    for rnd in rounds:
        round_id = _norm(rnd.get("id") or rnd.get("round"))
        if not round_id:
            continue
        round_name = _norm(rnd.get("name"))
        choices = _round_choices(rnd)
        if not choices:
            continue
        ranked = order_choices(
            [dict(choice, diffFen=_choice_diff(choice)) for choice in choices],
            prefer_codes=prefer_by_round.get(round_id) or prefer_by_round.get(round_name) or (),
            recent_codes=recent_by_round.get(round_id) or recent_by_round.get(round_name) or (),
            round_name=round_name,
        )
        pinned = keep.get(round_id) or keep.get(round_name)
        chosen = next((item for item in ranked if item.code == pinned), None) if pinned else None
        if chosen is None:
            chosen = ranked[0]
        base_diff += chosen.diff_fen
        round_list.append(
            {"round": round_id, "comboItemList": [{"code": chosen.code, "quantity": 1}]}
        )
        decisions.append(
            {
                "round": round_id,
                "roundName": round_name,
                "pinned": bool(pinned),
                "requested": pinned or None,
                "chosen": chosen.as_dict(),
                "order": [item.as_dict() for item in ranked],
            }
        )
    return {
        "roundList": round_list,
        "decisions": decisions,
        "diffFen": base_diff,
        "note": "zero-difference options do not win by default; recent/coded preferences and price break ties",
    }


def _item_map(items: Optional[Sequence[Mapping[str, Any]]]) -> Dict[str, Mapping[str, Any]]:
    out: Dict[str, Mapping[str, Any]] = {}
    for item in items or []:
        if isinstance(item, Mapping):
            out[_norm(item.get("productCode"))] = item
    return out


def _round_map(item: Optional[Mapping[str, Any]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for rnd in (item or {}).get("roundList") or []:
        if not isinstance(rnd, Mapping):
            continue
        round_id = _norm(rnd.get("round"))
        out[round_id] = [
            _norm((ci or {}).get("code"))
            for ci in rnd.get("comboItemList") or []
            if isinstance(ci, Mapping) and _norm(ci.get("code"))
        ]
    return out


def describe_swap(before: Mapping[str, Any], after: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """List round-level differences between two snapshots (pending vs calc)."""
    before_items = _item_map(before.get("items"))
    after_items = _item_map(after.get("items"))
    changes: List[Dict[str, Any]] = []
    for product_code in sorted(set(before_items) | set(after_items)):
        before_rounds = _round_map(before_items.get(product_code))
        after_rounds = _round_map(after_items.get(product_code))
        for round_id in sorted(set(before_rounds) | set(after_rounds)):
            old = before_rounds.get(round_id) or []
            new = after_rounds.get(round_id) or []
            if old != new:
                changes.append(
                    {
                        "productCode": product_code,
                        "round": round_id,
                        "before": old,
                        "after": new,
                    }
                )
    return changes


def sync_snapshots(
    pending: Mapping[str, Any],
    calc: Mapping[str, Any],
    *,
    require_identical_items: bool = True,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Derive a pending view from a calc snapshot, or refuse when they disagree.

    The calc snapshot is the only source of price verification. The pending view
    borrows its ``roundList`` and its price state, so a summary can never say
    ``实付`` for an estimate or show items the quote did not price.
    """
    now = now or datetime.now(CST)
    report = quote_state.consistency_report(pending, calc)
    items = list(calc.get("items") or pending.get("items") or [])
    if require_identical_items and not report["consistent"]:
        item_issues = [
            issue
            for issue in report["issues"]
            if issue
            in {
                "product_codes_mismatch",
                "round_list_mismatch",
                "combo_codes_mismatch",
                "pending_claims_verified_without_calc",
            }
        ]
        if item_issues:
            return {
                "ok": False,
                "error": "snapshots_describe_different_orders",
                "issues": report["issues"],
                "changes": describe_swap(pending, calc),
            }

    quote = {
        "price": calc.get("payableFen", calc.get("priceFen")),
        "productPrice": calc.get("priceFen"),
        "quotedAt": calc.get("quotedAt") or calc.get("fetchedAt"),
        "items": items,
    }
    check = quote_state.check_quote(
        quote,
        items=items,
        context=quote_state.context_of(calc),
        now=now,
        ttl_seconds=quote_state._quote_ttl(calc),
        require_timestamp=True,
    )
    status = quote_state.PriceStatus(
        state=quote_state.QUOTED if check.ok else (quote_state.STALE if check.expired else quote_state.ESTIMATE),
        price_fen=int(calc.get("priceFen") or pending.get("priceFen") or 0),
        payable_fen=int(quote["price"]) if check.ok and isinstance(quote["price"], int) else None,
        verified=check.ok,
        needs_quote=not check.ok,
        label=quote_state.PRICE_LABELS[
            quote_state.QUOTED if check.ok else (quote_state.STALE if check.expired else quote_state.ESTIMATE)
        ],
        reason=check.reason,
        quote_age_seconds=check.age_seconds,
    )

    merged = dict(pending)
    merged["items"] = items
    merged["combos"] = list(calc.get("combos") or pending.get("combos") or items)
    merged["priceFen"] = status.price_fen
    merged["payableFen"] = status.payable_fen
    merged["priceVerified"] = status.verified
    merged["priceState"] = status.state
    merged["priceLabel"] = status.label
    merged["quoteReason"] = status.reason
    merged["needsQuoteBeforeConfirm"] = status.needs_quote
    base_summary = quote_state.strip_price_suffix(str(calc.get("humanSummary") or pending.get("humanSummary") or ""))
    merged["humanSummary"] = quote_state.compose_summary(base_summary, status)
    if calc.get("rotationKey"):
        merged["rotationKey"] = calc["rotationKey"]
    if calc.get("calcTraceId"):
        merged["calcTraceId"] = calc["calcTraceId"]
    merged["syncedAt"] = now.replace(microsecond=0).isoformat()
    if not status.verified:
        # Never keep a confirmation phase for an unverified amount.
        merged["phase"] = "estimate_requote_required" if status.state == quote_state.STALE else "estimate_awaiting_quote"
    else:
        merged["phase"] = "quoted_awaiting_confirm"
    merged["orderCreated"] = False
    return {
        "ok": True,
        "issues": report["issues"],
        "changes": describe_swap(pending, calc),
        "pending": merged,
    }


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Named-combo ordering and pending/calc sync (offline only)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    order = sub.add_parser("order-choices", help="rank one round's options from a detail snapshot")
    order.add_argument("--detail", required=True, help="meal-detail JSON (snapshot)")
    order.add_argument("--round", default="", help="round id or name; default: all rounds")
    order.add_argument("--prefer", default="", help="comma separated preferred codes")
    order.add_argument("--recent", default="", help="comma separated recently accepted codes")

    sync = sub.add_parser("sync", help="rebuild a pending snapshot from its calc snapshot")
    sync.add_argument("--pending", required=True)
    sync.add_argument("--calc", required=True)
    sync.add_argument("--out", default=None, help="write merged pending JSON here (default stdout)")

    args = ap.parse_args(argv)
    if args.cmd == "order-choices":
        try:
            detail = json.loads(Path(args.detail).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"detail error: {exc}", file=sys.stderr)
            return 2
        prefer = [x.strip() for x in str(args.prefer or "").split(",") if x.strip()]
        recent = [x.strip() for x in str(args.recent or "").split(",") if x.strip()]
        rounds = _rounds_of(detail)
        wanted = _norm(args.round)
        payload = []
        for rnd in rounds:
            round_id = _norm(rnd.get("id") or rnd.get("round"))
            round_name = _norm(rnd.get("name"))
            if wanted and wanted not in (round_id, round_name):
                continue
            ranked = order_choices(
                [dict(choice, diffFen=_choice_diff(choice)) for choice in _round_choices(rnd)],
                prefer_codes=prefer,
                recent_codes=recent,
                round_name=round_name,
            )
            payload.append(
                {
                    "round": round_id,
                    "roundName": round_name,
                    "order": [item.as_dict() for item in ranked],
                }
            )
        print(json.dumps({"rounds": payload}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "sync":
        try:
            pending = json.loads(Path(args.pending).read_text(encoding="utf-8"))
            calc = json.loads(Path(args.calc).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"input error: {exc}", file=sys.stderr)
            return 2
        result = sync_snapshots(pending, calc)
        if not result.get("ok"):
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        text = json.dumps(result, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(json.dumps({"wrote": args.out, "issues": result["issues"]}, ensure_ascii=False))
        else:
            print(text)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
