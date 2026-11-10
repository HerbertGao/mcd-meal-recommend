"""Exposure / skip / swap / deal tracks, recorded separately and idempotently.

History files only contain paid picks. That makes "shown yesterday, not taken"
invisible to the scorer, so the same combo can look completely unseen the next
day. This module keeps the other tracks in their own document:

    exposure  the combo was offered to the user
    skip      the user declined the offer (never a deal, never "disliked")
    swap      the user changed part of the offer before deciding
    deal      the user confirmed and paid

Rules encoded here:

* Tracks never merge. A skip is not a deal; an exposure is not a deal; a swap is
  neither a skip of the final combo nor a deal by itself.
* Recording is idempotent: the same (scope, kind, rotationKey, date, seq) is
  written once, so retrying a routine cannot inflate counts.
* Small samples are not preferences. ``rotation_penalty`` is a mild, bounded
  rotation nudge with the counts attached so a caller can explain the decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

CST = timezone(timedelta(hours=8))

EXPOSURE = "exposure"
SKIP = "skip"
SWAP = "swap"
DEAL = "deal"
KINDS = (EXPOSURE, SKIP, SWAP, DEAL)

SCOPE_FIELDS = ("storeCode", "dining", "mealPeriod", "reservationDate")

DEFAULT_WINDOW_DAYS = 7


def _now() -> datetime:
    return datetime.now(CST)


def parse_ts(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    if len(text) == 10:
        text += "T00:00:00+08:00"
    if len(text) == 16 and text[10:11] == " ":
        text = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed


def empty_log() -> Dict[str, Any]:
    return {"version": 1, "events": []}


def normalize_event(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in KINDS:
        return None
    rotation_key = str(raw.get("rotationKey") or "").strip()
    if not rotation_key:
        return None
    at = str(raw.get("at") or raw.get("date") or "")
    event = {
        "kind": kind,
        "rotationKey": rotation_key,
        "at": at,
        "date": str(raw.get("date") or at[:10] or ""),
        "seq": int(raw.get("seq") or 0),
    }
    for field_name in SCOPE_FIELDS:
        value = raw.get(field_name)
        if value not in (None, ""):
            event[field_name] = str(value)
    if raw.get("priceFen") is not None:
        try:
            event["priceFen"] = int(raw["priceFen"])
        except (TypeError, ValueError):
            pass
    if raw.get("priceState"):
        event["priceState"] = str(raw["priceState"])
    if raw.get("note"):
        event["note"] = str(raw["note"])
    if raw.get("source"):
        event["source"] = str(raw["source"])
    # Identity is recomputed. A caller-supplied eventKey must not bypass dedup.
    event["eventKey"] = event_key(event)
    return event


def event_key(event: Mapping[str, Any]) -> str:
    """Deterministic identity used for idempotent writes."""
    parts = [str(event.get("kind") or "")]
    parts += [str(event.get(field_name) or "") for field_name in SCOPE_FIELDS]
    parts += [str(event.get("date") or ""), str(event.get("rotationKey") or ""), str(event.get("seq") or 0)]
    blob = "|".join(parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:20]


def normalize_log(data: Any) -> Dict[str, Any]:
    doc = empty_log()
    if isinstance(data, dict):
        events = data.get("events") or data.get("history") or []
    elif isinstance(data, list):
        events = data
    else:
        events = []
    seen = set()
    for raw in events:
        event = normalize_event(raw)
        if event is None:
            continue
        key = event["eventKey"]
        if key in seen:
            continue
        seen.add(key)
        doc["events"].append(event)
    return doc


def read_log(path: Optional[str]) -> Tuple[Dict[str, Any], str]:
    """Load a log. Status is ``missing``, ``ok``, or ``corrupt``.

    A corrupt file is not treated as an empty history: callers must not
    overwrite it or read it as "never shown".
    """
    if not path:
        return empty_log(), "missing"
    p = Path(path)
    if not p.is_file():
        return empty_log(), "missing"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "events": [], "unreadable": True}, "corrupt"
    return normalize_log(raw), "ok"


def load_log(path: Optional[str]) -> Dict[str, Any]:
    doc, _status = read_log(path)
    return doc


def save_log(path: str, doc: Mapping[str, Any]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "events": list(doc.get("events") or [])}
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


@dataclass(frozen=True)
class RecordResult:
    status: str  # recorded | duplicate
    event: Dict[str, Any]


def record(path: str, raw: Mapping[str, Any], *, now: Optional[datetime] = None) -> RecordResult:
    """Append one event unless an identical one already exists."""
    now = now or _now()
    payload = dict(raw)
    payload.setdefault("at", now.replace(microsecond=0).isoformat())
    event = normalize_event(payload)
    if event is None:
        raise ValueError(f"invalid event: kind must be one of {KINDS} and rotationKey is required")
    doc, status = read_log(path)
    if status == "corrupt":
        raise ValueError("exposure log is unreadable; refusing to overwrite")
    existing = {str(item.get("eventKey") or "") for item in doc["events"]}
    if event["eventKey"] in existing:
        return RecordResult("duplicate", event)
    doc["events"].append(event)
    save_log(path, doc)
    return RecordResult("recorded", event)


def parse_scope(source: Mapping[str, Any]) -> Dict[str, str]:
    """Extract the isolation scope used to keep tracks per store/meal context."""
    scope: Dict[str, str] = {}
    for field_name in SCOPE_FIELDS:
        value = source.get(field_name)
        if value not in (None, ""):
            scope[field_name] = str(value)
    return scope


def _same_scope(event: Mapping[str, Any], scope: Mapping[str, Any]) -> bool:
    for field_name in SCOPE_FIELDS:
        expected = str(scope.get(field_name) or "")
        actual = str(event.get(field_name) or "")
        if expected and actual and expected != actual:
            return False
    return True


def summarize(
    events: Sequence[Mapping[str, Any]],
    *,
    scope: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Dict[str, Dict[str, Any]]:
    """Per-rotationKey counts for the recent window: ``{rk: {exposure, skip, swap, deal, ...}}``."""
    now = now or _now()
    out: Dict[str, Dict[str, Any]] = {}
    for raw in events:
        event = raw if isinstance(raw, dict) and raw.get("eventKey") else normalize_event(raw)
        if not event:
            continue
        if scope and not _same_scope(event, scope):
            continue
        at = parse_ts(event.get("at")) or parse_ts(event.get("date"))
        if at is not None and window_days > 0 and (now - at).days > window_days:
            continue
        rk = str(event.get("rotationKey") or "")
        row = out.setdefault(
            rk,
            {"rotationKey": rk, "exposure": 0, "skip": 0, "swap": 0, "deal": 0, "lastAt": "", "lastKind": ""},
        )
        kind = str(event.get("kind") or "")
        if kind in KINDS:
            row[kind] += 1
        at_text = str(event.get("at") or event.get("date") or "")
        if at_text >= str(row.get("lastAt") or ""):
            row["lastAt"] = at_text
            row["lastKind"] = kind
    for row in out.values():
        row["shownNotAccepted"] = max(0, row["exposure"] - row["deal"])
        row["declined"] = row["skip"] > 0 and row["deal"] == 0
    return out


def rotation_penalty(
    summary: Mapping[str, Any],
    *,
    factor_per_exposure: float = 0.88,
    factor_per_skip: float = 0.9,
    floor: float = 0.5,
) -> Tuple[float, str]:
    """Bounded multiplier for "shown before, not taken". Never zero, never a fact claim."""
    exposures = int(summary.get("exposure") or 0)
    skips = int(summary.get("skip") or 0)
    deals = int(summary.get("deal") or 0)
    if deals > 0:
        # Accepted before: recency/rotation is handled by the deal-history tracks.
        return 1.0, "accepted_before"
    penalty = 1.0
    reasons: List[str] = []
    if exposures > 0:
        penalty *= factor_per_exposure ** min(exposures, 4)
        reasons.append(f"exposed{exposures}")
    if skips > 0:
        penalty *= factor_per_skip ** min(skips, 3)
        reasons.append(f"skipped{skips}")
    penalty = max(floor, min(1.0, penalty))
    return penalty, ",".join(reasons) if reasons else "none"


def apply_to_candidate(cand: Any, summary: Mapping[str, Any], *, factor: Optional[float] = None) -> float:
    """Attach exposure stats to a scored candidate and apply the penalty to score."""
    meta = getattr(cand, "meta", None)
    if not isinstance(meta, dict):
        return 1.0
    penalty, reason = rotation_penalty(summary) if factor is None else (factor, "explicit")
    meta["priorExposure"] = bool(summary.get("exposure"))
    meta["exposureCount"] = int(summary.get("exposure") or 0)
    meta["skipCount"] = int(summary.get("skip") or 0)
    meta["swapCount"] = int(summary.get("swap") or 0)
    meta["exposureDealCount"] = int(summary.get("deal") or 0)
    meta["shownNotAccepted"] = int(summary.get("shownNotAccepted") or 0)
    meta["exposurePenalty"] = round(penalty, 4)
    meta["exposurePenaltyReason"] = reason
    if penalty < 1.0:
        cand.score = round(cand.score * penalty, 2)
    return penalty


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Record exposure/skip/swap/deal events separately (idempotent; never calls MCP or orders)"
    )
    ap.add_argument("--file", required=True, help="event log path (gitignored runs/ location)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_scope(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--store", dest="storeCode", default="")
        parser.add_argument("--dining", default="")
        parser.add_argument("--meal-period", default="")
        parser.add_argument("--reservation-date", default="")

    rec = sub.add_parser("record", help="append one event (duplicates are ignored)")
    add_scope(rec)
    rec.add_argument("--kind", required=True, choices=list(KINDS))
    rec.add_argument("--rotation-key", required=True)
    rec.add_argument("--at", default="")
    rec.add_argument("--seq", type=int, default=0)
    rec.add_argument("--price-fen", type=int, default=None)
    rec.add_argument("--price-state", default="")
    rec.add_argument("--note", default="")
    rec.add_argument("--source", default="")

    show = sub.add_parser("summary", help="print per-rotationKey counts for the window")
    add_scope(show)
    show.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)

    args = ap.parse_args(argv)
    if args.cmd == "record":
        payload: Dict[str, Any] = {
            "kind": args.kind,
            "rotationKey": args.rotation_key,
            "seq": args.seq,
            "note": args.note,
            "source": args.source,
        }
        for field_name in ("storeCode", "dining", "mealPeriod", "reservationDate"):
            value = getattr(args, field_name, "")
            if value:
                payload[field_name] = value
        if args.at:
            payload["at"] = args.at
        if args.price_fen is not None:
            payload["priceFen"] = args.price_fen
        if args.price_state:
            payload["priceState"] = args.price_state
        try:
            result = record(args.file, payload)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"status": result.status, "event": result.event}, ensure_ascii=False, indent=2))
        return 0 if result.status in ("recorded", "duplicate") else 1

    scope = {field_name: getattr(args, field_name) for field_name in SCOPE_FIELDS}
    doc = load_log(args.file)
    summary = summarize(doc["events"], scope={k: v for k, v in scope.items() if v}, window_days=args.window_days)
    print(json.dumps({"file": args.file, "windowDays": args.window_days, "rotationKeys": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
