"""Price state for candidates and pending snapshots.

A list price, a cached snapshot price, or an expired ``calculate-price`` result
is never a confirmed amount. This module keeps the distinction explicit so the
scorer, a pending snapshot, and a calc snapshot can agree on wording.

States
------
``estimate``  list/estimate price only. Must be shown as 估价 and requires a
              live ``calculate-price`` before the user confirms.
``quoted``    live quote bound to the exact item snapshot (and request context)
              and still inside its freshness window. Only then may a total be
              described as 实付.
``stale``     a quote existed but expired, or items/context changed afterwards.
              It must be re-quoted; the old amount is never reused.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

CST = timezone(timedelta(hours=8))

ESTIMATE = "estimate"
QUOTED = "quoted"
STALE = "stale"

DEFAULT_QUOTE_TTL_SECONDS = 1800

PRICE_LABELS = {
    ESTIMATE: "估价",
    QUOTED: "已核价",
    STALE: "核价已过期",
}

# Trailing price clauses produced by callers. Kept narrow on purpose: only the
# end of the summary is rewritten, item names are never touched.
_PRICE_SUFFIX_RE = re.compile(
    r"(\s*(?:[，,、;；]\s*)?(?:实付|应付|估价|约|共计|合计)?\s*[≈~约]?\s*[¥￥]\s*\d+(?:\.\d+)?\s*元?)\s*$"
)
_CONTEXT_KEYS = (
    "storeCode",
    "dining",
    "beCode",
    "beType",
    "orderType",
    "addressId",
    "reservationDate",
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:16]


def _item_view(items: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, Any]]:
    view: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rounds = []
        for rnd in item.get("roundList") or []:
            if not isinstance(rnd, dict):
                continue
            choices = []
            for ci in rnd.get("comboItemList") or []:
                if not isinstance(ci, dict):
                    continue
                mod = ci.get("modification") or {}
                keys = []
                for val in mod.get("values") or []:
                    if isinstance(val, dict):
                        keys.append([str(val.get("code") or ""), str(val.get("key") or ""), int(val.get("quantity") or 0)])
                choices.append([str(ci.get("code") or ""), int(ci.get("quantity") or 1), keys])
            rounds.append([str(rnd.get("round") or ""), choices])
        view.append([str(item.get("productCode") or ""), int(item.get("quantity") or 1), rounds])
    return view


def items_fingerprint(items: Optional[Sequence[Mapping[str, Any]]]) -> str:
    """Stable identity of the exact order payload (codes, rounds, modifiers)."""
    return _digest(_item_view(items))


def context_fingerprint(context: Optional[Mapping[str, Any]]) -> str:
    ctx = {key: str((context or {}).get(key) or "") for key in _CONTEXT_KEYS}
    return _digest(ctx)


def context_of(source: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    return {key: str((source or {}).get(key) or "") for key in _CONTEXT_KEYS}


def parse_ts(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    if len(text) == 16 and text[10:11] == " ":
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed


@dataclass(frozen=True)
class QuoteCheck:
    ok: bool
    reason: str
    age_seconds: Optional[int] = None
    items_bound: bool = False
    expired: bool = False


def _quote_items(quote: Mapping[str, Any]) -> Optional[Sequence[Any]]:
    for key in ("items", "combos", "productList"):
        raw = quote.get(key)
        if isinstance(raw, list) and raw:
            # productList entries carry no roundList; only trust round payloads.
            if key == "productList" and not any(isinstance(entry, dict) and entry.get("roundList") for entry in raw):
                continue
            return raw
    return None


def check_quote(
    quote: Optional[Mapping[str, Any]],
    *,
    items: Optional[Sequence[Mapping[str, Any]]] = None,
    context: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
    ttl_seconds: Optional[int] = None,
    require_timestamp: bool = False,
) -> QuoteCheck:
    """Validate a ``calculate-price`` payload before treating it as confirmed."""
    if not isinstance(quote, dict):
        return QuoteCheck(False, "missing_quote")
    payable = quote.get("price")
    if not isinstance(payable, int) or isinstance(payable, bool) or payable <= 0:
        return QuoteCheck(False, "missing_payable")

    now = now or datetime.now(CST)
    ttl = DEFAULT_QUOTE_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)

    quoted_at = parse_ts(quote.get("quotedAt") or quote.get("fetchedAt") or quote.get("datetime"))
    expires_at = parse_ts(quote.get("expiresAt"))
    if require_timestamp and quoted_at is None and expires_at is None:
        # Without a time there is no way to show the amount is still current.
        return QuoteCheck(False, "missing_quote_time")
    age: Optional[int] = None
    if quoted_at is not None:
        age = int((now - quoted_at).total_seconds())
    if expires_at is not None and now > expires_at:
        return QuoteCheck(False, "expired", age_seconds=age, expired=True)
    if ttl > 0 and age is not None and age > ttl:
        return QuoteCheck(False, "expired", age_seconds=age, expired=True)

    if context:
        for key in _CONTEXT_KEYS:
            expected = str(context.get(key) or "")
            actual = str(quote.get(key) or "")
            if expected and actual and expected != actual:
                return QuoteCheck(False, f"context_mismatch:{key}", age_seconds=age)

    quoted_items = _quote_items(quote)
    if quoted_items is not None and items:
        if items_fingerprint(quoted_items) != items_fingerprint(items):
            return QuoteCheck(False, "items_mismatch", age_seconds=age, items_bound=True)
        return QuoteCheck(True, "ok", age_seconds=age, items_bound=True)
    # A quote without an item payload cannot be bound locally. Accept it (older
    # callers only pass the price fields) but report it as unbound so callers
    # can decide to re-quote before payment.
    return QuoteCheck(True, "ok_unbound_items", age_seconds=age, items_bound=False)


@dataclass(frozen=True)
class PriceStatus:
    state: str
    price_fen: int
    payable_fen: Optional[int]
    verified: bool
    needs_quote: bool
    label: str
    reason: str
    quote_age_seconds: Optional[int] = None

    @property
    def price_text(self) -> str:
        return render_price_text(self)


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _quote_ttl(req: Optional[Mapping[str, Any]]) -> Optional[int]:
    if not isinstance(req, Mapping):
        return None
    for key in ("quoteTtlMinutes", "priceQuoteTtlMinutes"):
        raw = req.get(key)
        if raw is None:
            continue
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            continue
        return int(minutes * 60)
    prefs = req.get("prefs") or {}
    raw = prefs.get("quoteTtlMinutes") if isinstance(prefs, Mapping) else None
    if raw is not None:
        try:
            return int(float(raw) * 60)
        except (TypeError, ValueError):
            return None
    return None


def price_status(
    cand: Any,
    req: Optional[Mapping[str, Any]] = None,
    *,
    now: Optional[datetime] = None,
) -> PriceStatus:
    """Derive the state of one candidate without trusting display fields."""
    price_fen = int(_get(cand, "price_fen", 0) or 0)
    payable = _get(cand, "payable_fen")
    verified = bool(_get(cand, "price_verified", False))
    meta = _get(cand, "meta", {}) or {}
    if not isinstance(meta, Mapping):
        meta = {}

    if meta.get("quoteStale") or meta.get("quoteRejected") == "expired":
        return PriceStatus(STALE, price_fen, None, False, True, PRICE_LABELS[STALE], "expired")

    if verified and isinstance(payable, int) and payable > 0:
        return PriceStatus(
            QUOTED,
            price_fen,
            payable,
            True,
            False,
            PRICE_LABELS[QUOTED],
            str(meta.get("quoteCheck") or "ok"),
            meta.get("quoteAgeSeconds"),
        )

    if meta.get("quoteRejected"):
        return PriceStatus(
            STALE,
            price_fen,
            None,
            False,
            True,
            PRICE_LABELS[STALE],
            str(meta["quoteRejected"]),
            meta.get("quoteAgeSeconds"),
        )

    return PriceStatus(ESTIMATE, price_fen, None, False, True, PRICE_LABELS[ESTIMATE], "no_quote")


def render_price_text(status: PriceStatus) -> str:
    yuan = f"¥{status.price_fen / 100:.1f}"
    if status.state == QUOTED and status.payable_fen:
        paid = f"¥{status.payable_fen / 100:.1f}"
        if status.payable_fen != status.price_fen:
            return f"实付 {paid}（商品 {yuan}）"
        return f"实付 {paid}"
    if status.state == STALE:
        return f"估价 {yuan}（核价已过期，需重新核价）"
    return f"估价 {yuan}"


def strip_price_suffix(summary: str) -> str:
    """Remove a trailing price clause so summaries can be compared by items."""
    text = str(summary or "").strip()
    return _PRICE_SUFFIX_RE.sub("", text).strip()


def compose_summary(item_summary: str, status: PriceStatus, *, suffix: str = "") -> str:
    """Render one summary for a given price state (pending and calc share it)."""
    base = strip_price_suffix(item_summary)
    text = f"{base}，{status.price_text}"
    tail = str(suffix or "").strip()
    if tail:
        text += tail if tail[0] in "，,。 " else f"，{tail}"
    return text


def _code_list(items: Optional[Sequence[Mapping[str, Any]]]) -> List[str]:
    return [str(item.get("productCode") or "") for item in items or [] if isinstance(item, dict)]


def _flat_codes(items: Optional[Sequence[Mapping[str, Any]]]) -> List[str]:
    out: List[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for rnd in item.get("roundList") or []:
            for ci in (rnd or {}).get("comboItemList") or []:
                code = str((ci or {}).get("code") or "")
                if code:
                    out.append(code)
    return out


def _calc_verified(calc: Mapping[str, Any]) -> bool:
    """A raw calculate-price payload proves a quote by its amount, not a flag."""
    payable = calc.get("payableFen", calc.get("price"))
    if isinstance(payable, int) and not isinstance(payable, bool) and payable > 0:
        return True
    return bool(calc.get("priceVerified"))


def consistency_report(pending: Mapping[str, Any], calc: Mapping[str, Any]) -> Dict[str, Any]:
    """Compare a pending snapshot with its calc snapshot.

    Returns item/price/wording checks. ``consistent`` is true only when the
    order payload, the amounts, and the price wording agree.
    """
    issues: List[str] = []
    pending_items = list(pending.get("items") or [])
    calc_items = list(calc.get("items") or pending_items)

    if _code_list(pending_items) != _code_list(calc_items):
        issues.append("product_codes_mismatch")
    if items_fingerprint(pending_items) != items_fingerprint(calc_items):
        issues.append("round_list_mismatch")
    if _flat_codes(pending_items) != _flat_codes(calc_items):
        issues.append("combo_codes_mismatch")

    pending_price = pending.get("payableFen", pending.get("priceFen"))
    calc_price = calc.get("payableFen", calc.get("priceFen"))
    if pending_price is not None and calc_price is not None and int(pending_price) != int(calc_price):
        issues.append("price_mismatch")

    pending_verified = bool(pending.get("priceVerified"))
    calc_verified = _calc_verified(calc)
    if pending_verified and not calc_verified:
        issues.append("pending_claims_verified_without_calc")

    pending_label = str(pending.get("priceLabel") or pending.get("priceState") or "")
    calc_label = str(calc.get("priceLabel") or calc.get("priceState") or "")
    if pending_label and calc_label and pending_label != calc_label:
        issues.append("price_label_mismatch")

    pending_text = strip_price_suffix(str(pending.get("humanSummary") or ""))
    calc_text = strip_price_suffix(str(calc.get("humanSummary") or ""))
    if pending_text and calc_text and pending_text != calc_text:
        issues.append("human_summary_items_mismatch")

    pending_full = str(pending.get("humanSummary") or "")
    calc_full = str(calc.get("humanSummary") or "")
    if pending_full and calc_full and (pending_full != calc_full):
        if strip_price_suffix(pending_full) == strip_price_suffix(calc_full):
            issues.append("human_summary_price_wording_mismatch")
        elif pending_text != calc_text:
            pass  # already reported as item mismatch

    return {
        "consistent": not issues,
        "issues": issues,
        "checks": {
            "productCode": _code_list(pending_items),
            "itemsFingerprint": items_fingerprint(pending_items),
            "payableFen": pending_price,
            "priceState": pending_label or None,
        },
    }


def iter_price_suffixes(summaries: Iterable[str]) -> List[str]:
    """Debug helper: collect stripped price clauses from summaries."""
    out = []
    for summary in summaries:
        text = str(summary or "")
        match = _PRICE_SUFFIX_RE.search(text)
        if match:
            out.append(match.group(1).strip())
    return out
