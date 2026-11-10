"""Offline snapshot cache for MCP menu / meal-detail payloads.

The repository never calls MCP. This module is a plain file store the caller can
use so a warm snapshot is reused only while every isolation dimension still
matches. It does not know about prices as facts: a cached price is a list price
and can never become a verified amount (see ``quote_state``).

Isolation key (all fields matter, none default to "any"):

    kind            menu | detail
    storeCode       eating store (eat-in) or deliverable store (delivery)
    dining          eat-in / take-in-store / delivery
    mealPeriod      lunch / breakfast / dinner / snack
    reservationDate materialized ``yyyy-MM-dd HH:mm`` or "" for immediate
    beCode          delivery business code ("" for eat-in)
    addressId       delivery address ("" for eat-in)
    productCode     detail only
    menuFingerprint detail only; required. Different menus are different keys.

Invalidation rules:

* expired by TTL or explicit ``expiresAt``
* any isolation field differs, including ``menuFingerprint`` (separate files)
* stored metadata disagrees with the lookup key (treated as ``context_changed``)
* the caller recorded a failure for the entry (``fail``), including a corrupt tombstone
* the entry is unreadable, fails its payload fingerprint, or fails the caller's validator

A detail lookup that omits ``menuFingerprint`` is rejected. It is not a wildcard
and must not reuse a detail captured under another menu.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

CST = timezone(timedelta(hours=8))

KIND_MENU = "menu"
KIND_DETAIL = "detail"

DEFAULT_TTL_SECONDS = {
    KIND_MENU: 900,
    KIND_DETAIL: 86400,
}

KEY_FIELDS = (
    "kind",
    "storeCode",
    "dining",
    "mealPeriod",
    "reservationDate",
    "beCode",
    "addressId",
    "productCode",
    "menuFingerprint",
)


def _now() -> datetime:
    return datetime.now(CST)


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


def fingerprint(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class CacheKey:
    kind: str
    store_code: str
    dining: str = "eat-in"
    meal_period: str = "lunch"
    reservation_date: str = ""
    be_code: str = ""
    address_id: str = ""
    product_code: str = ""
    menu_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.kind not in (KIND_MENU, KIND_DETAIL):
            raise ValueError(f"unsupported cache kind: {self.kind!r}")
        store = str(self.store_code or "").strip()
        if not store:
            raise ValueError("cache key requires storeCode")
        object.__setattr__(self, "store_code", store)
        if self.kind == KIND_DETAIL:
            product = str(self.product_code or "").strip()
            menu_fp = str(self.menu_fingerprint or "").strip()
            if not product:
                raise ValueError("detail cache key requires productCode")
            if not menu_fp:
                raise ValueError("detail cache key requires menuFingerprint")
            object.__setattr__(self, "product_code", product)
            object.__setattr__(self, "menu_fingerprint", menu_fp)
        else:
            # Menu entries are the menu. A product code or fingerprint here would
            # either fragment one menu or let a detail key alias it.
            object.__setattr__(self, "product_code", "")
            object.__setattr__(self, "menu_fingerprint", "")

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "CacheKey":
        kind = str(data.get("kind") or KIND_MENU)
        return cls(
            kind=kind,
            store_code=str(data.get("storeCode") or ""),
            dining=str(data.get("dining") or "eat-in"),
            meal_period=str(data.get("mealPeriod") or "lunch"),
            reservation_date=str(data.get("reservationDate") or ""),
            be_code=str(data.get("beCode") or ""),
            address_id=str(data.get("addressId") or ""),
            product_code=str(data.get("productCode") or "") if kind == KIND_DETAIL else "",
            menu_fingerprint=str(data.get("menuFingerprint") or ""),
        )

    def scope(self) -> Dict[str, str]:
        """Dimensions that must match between write and read.

        ``menuFingerprint`` is part of the detail key, so two menus never share
        one file. Menu entries store an empty fingerprint; they are not looked
        up by it.
        """
        return {
            "kind": self.kind,
            "storeCode": self.store_code,
            "dining": self.dining,
            "mealPeriod": self.meal_period,
            "reservationDate": self.reservation_date,
            "beCode": self.be_code,
            "addressId": self.address_id,
            "productCode": self.product_code,
            "menuFingerprint": self.menu_fingerprint,
        }

    def label(self) -> str:
        bits = [
            self.kind,
            f"store={self.store_code}",
            f"dining={self.dining}",
            f"meal={self.meal_period}",
            f"reservation={self.reservation_date or '-'}",
        ]
        if self.be_code:
            bits.append(f"be={self.be_code}")
        if self.address_id:
            bits.append(f"addr={self.address_id}")
        if self.product_code:
            bits.append(f"product={self.product_code}")
        return "|".join(bits)

    def digest(self) -> str:
        payload = json.dumps(self.scope(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    def dirname(self, root: Path) -> Path:
        return Path(root) / self.kind / self.digest()


@dataclass(frozen=True)
class CacheResult:
    status: str  # hit | missing | expired | context_changed | invalidated | error
    payload: Optional[Any] = None
    entry_meta: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "hit"


class SnapshotCache:
    """File-backed cache rooted at ``root`` (gitignored by default)."""

    def __init__(self, root: "str | Path") -> None:
        self.root = Path(root)

    def entry_path(self, key: CacheKey) -> Path:
        return key.dirname(self.root) / "entry.json"

    def tombstone_path(self, key: CacheKey) -> Path:
        return key.dirname(self.root) / "failed.json"

    def _read_json_status(self, path: Path) -> tuple:
        """Return ``(missing|ok|corrupt, data)``. A bad file is not a miss."""
        if not path.exists():
            return "missing", None
        try:
            return "ok", json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "corrupt", None

    def get(
        self,
        key: CacheKey,
        *,
        now: Optional[datetime] = None,
        ttl_seconds: Optional[int] = None,
        validator: Optional[Callable[[Any], bool]] = None,
    ) -> CacheResult:
        now = now or _now()
        tomb_state, tomb = self._read_json_status(self.tombstone_path(key))
        entry_state, entry = self._read_json_status(self.entry_path(key))
        meta: Dict[str, Any] = {}
        written = None
        if entry_state == "ok" and isinstance(entry, dict) and isinstance(entry.get("meta"), dict):
            meta = entry["meta"]
            written = parse_ts(meta.get("writtenAt"))

        if tomb_state == "corrupt" or (tomb_state == "ok" and not isinstance(tomb, dict)):
            return CacheResult("invalidated", entry_meta=meta, reason="corrupt_tombstone")
        if tomb_state == "ok" and isinstance(tomb, dict):
            failed_at = parse_ts(tomb.get("at"))
            # A newer successful put wins. An unreadable or older failure does not.
            entry_is_newer = written is not None and failed_at is not None and written >= failed_at
            if not entry_is_newer:
                return CacheResult(
                    "invalidated",
                    entry_meta={"failedAt": tomb.get("at"), "error": tomb.get("error")},
                    reason="previous_error",
                )

        if entry_state == "missing":
            return CacheResult("missing", reason="no_entry")
        if entry_state == "corrupt" or not isinstance(entry, dict):
            return CacheResult("error", entry_meta=meta, reason="corrupt_entry")

        payload = entry.get("payload")
        stored_scope = {field_name: str(meta.get(field_name) or "") for field_name in key.scope()}
        if stored_scope != key.scope():
            diffs = sorted(name for name, val in key.scope().items() if stored_scope.get(name) != val)
            return CacheResult("context_changed", entry_meta=meta, reason=",".join(diffs) or "context")

        expires = parse_ts(meta.get("expiresAt"))
        ttl = DEFAULT_TTL_SECONDS[key.kind] if ttl_seconds is None else int(ttl_seconds)
        if expires is not None and now > expires:
            return CacheResult("expired", entry_meta=meta, reason="expiresAt")
        if ttl > 0 and written is not None and (now - written).total_seconds() > ttl:
            return CacheResult("expired", entry_meta=meta, reason="ttl")

        if payload is None:
            return CacheResult("invalidated", entry_meta=meta, reason="empty_payload")
        stored_fp = str(meta.get("payloadFingerprint") or "")
        if not stored_fp or stored_fp != fingerprint(payload):
            return CacheResult("error", entry_meta=meta, reason="payload_fingerprint_mismatch")
        if validator is not None:
            try:
                accepted = bool(validator(payload))
            except Exception:
                return CacheResult("error", entry_meta=meta, reason="validator_error")
            if not accepted:
                return CacheResult("error", entry_meta=meta, reason="validator_rejected")
        return CacheResult("hit", payload=payload, entry_meta=meta, reason="")

    def put(
        self,
        key: CacheKey,
        payload: Any,
        *,
        now: Optional[datetime] = None,
        ttl_seconds: Optional[int] = None,
        expires_at: Optional[str] = None,
        source: str = "",
    ) -> Path:
        now = now or _now()
        ttl = DEFAULT_TTL_SECONDS[key.kind] if ttl_seconds is None else int(ttl_seconds)
        meta: Dict[str, Any] = {
            **key.scope(),
            "writtenAt": now.replace(microsecond=0).isoformat(),
            "expiresAt": expires_at
            or ((now + timedelta(seconds=ttl)).replace(microsecond=0).isoformat() if ttl > 0 else ""),
            "payloadFingerprint": fingerprint(payload),
            "source": source,
        }
        if key.kind == KIND_DETAIL:
            meta["menuFingerprint"] = key.menu_fingerprint
        directory = key.dirname(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        path = self.entry_path(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"meta": meta, "payload": payload}, ensure_ascii=False),
            encoding="utf-8",
        )
        # Drop the tombstone before publishing so a crash cannot leave a fresh
        # entry permanently hidden behind the previous failure.
        self.tombstone_path(key).unlink(missing_ok=True)
        tmp.replace(path)
        return path

    def fail(self, key: CacheKey, error: str, *, now: Optional[datetime] = None) -> Path:
        """Mark an entry unusable after an MCP/parse error until the next ``put``."""
        now = now or _now()
        directory = key.dirname(self.root)
        directory.mkdir(parents=True, exist_ok=True)
        self.entry_path(key).unlink(missing_ok=True)
        path = self.tombstone_path(key)
        path.write_text(
            json.dumps({"at": now.replace(microsecond=0).isoformat(), "error": str(error), **key.scope()}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def invalidate(self, key: CacheKey) -> None:
        self.entry_path(key).unlink(missing_ok=True)
        self.tombstone_path(key).unlink(missing_ok=True)

    def _entry_is_corrupt(self, data: Any) -> bool:
        if not isinstance(data, dict):
            return True
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        payload = data.get("payload")
        stored_fp = str(meta.get("payloadFingerprint") or "")
        if payload is None or not stored_fp:
            return True
        return stored_fp != fingerprint(payload)

    def prune(self, *, now: Optional[datetime] = None, keep_expired_seconds: int = 86400) -> int:
        """Delete corrupt entries and ones expired longer than ``keep_expired_seconds``.

        Tombstones are kept. A failure still blocks that key until the next
        successful ``put``; pruning must not turn a recorded error back into a hit.
        """
        now = now or _now()
        removed = 0
        if not self.root.exists():
            return 0
        for entry in list(self.root.glob("*/*/entry.json")):
            state, data = self._read_json_status(entry)
            drop = state != "ok" or self._entry_is_corrupt(data)
            if not drop and isinstance(data, dict):
                meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
                expires = parse_ts(meta.get("expiresAt"))
                if expires is not None and (now - expires).total_seconds() > keep_expired_seconds:
                    drop = True
            if drop:
                entry.unlink(missing_ok=True)
                removed += 1
        return removed

    def stats(self, *, now: Optional[datetime] = None) -> Dict[str, int]:
        now = now or _now()
        counts = {"hit": 0, "expired": 0, "failed": 0, "corrupt": 0}
        for entry in self.root.glob("*/*/entry.json"):
            if (entry.parent / "failed.json").exists():
                continue
            state, data = self._read_json_status(entry)
            if state != "ok" or self._entry_is_corrupt(data):
                counts["corrupt"] += 1
                continue
            meta = data.get("meta") if isinstance(data, dict) and isinstance(data.get("meta"), dict) else {}
            expires = parse_ts(meta.get("expiresAt"))
            if expires is not None and now > expires:
                counts["expired"] += 1
            else:
                counts["hit"] += 1
        counts["failed"] = len(list(self.root.glob("*/*/failed.json")))
        return counts


def load_detail_cache(
    cache: SnapshotCache,
    *,
    store_code: str,
    dining: str,
    meal_period: str,
    reservation_date: str = "",
    be_code: str = "",
    address_id: str = "",
    product_codes: Optional[list] = None,
    menu_fingerprint: str = "",
    now: Optional[datetime] = None,
    validator: Optional[Callable[[Any], bool]] = None,
) -> Dict[str, Any]:
    """Return ``{productCode: detail}`` for fresh, context-matching entries."""
    if product_codes and not str(menu_fingerprint or "").strip():
        raise ValueError("detail cache lookup requires menuFingerprint")
    out: Dict[str, Any] = {}
    for code in product_codes or []:
        key = CacheKey(
            kind=KIND_DETAIL,
            store_code=str(store_code),
            dining=str(dining),
            meal_period=str(meal_period),
            reservation_date=str(reservation_date or ""),
            be_code=str(be_code or ""),
            address_id=str(address_id or ""),
            product_code=str(code),
            menu_fingerprint=str(menu_fingerprint or ""),
        )
        result = cache.get(key, now=now, validator=validator)
        if result.ok:
            out[str(code)] = result.payload
    return out


def build_key(args: argparse.Namespace) -> CacheKey:
    return CacheKey(
        kind=args.kind,
        store_code=args.store,
        dining=args.dining,
        meal_period=args.meal_period,
        reservation_date=args.reservation_date or "",
        be_code=args.be_code or "",
        address_id=args.address_id or "",
        product_code=args.product_code or "",
        menu_fingerprint=args.menu_fingerprint or "",
    )


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Inspect or edit the offline MCP snapshot cache (never calls MCP, never prices an order)"
    )
    ap.add_argument("--root", default="engine/runs/cache", help="cache root (gitignored)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_key_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--kind", choices=[KIND_MENU, KIND_DETAIL], required=True)
        parser.add_argument("--store", required=True, help="storeCode (digits only)")
        parser.add_argument("--dining", default="eat-in")
        parser.add_argument("--meal-period", default="lunch")
        parser.add_argument("--reservation-date", default="", help='materialized "yyyy-MM-dd HH:mm"')
        parser.add_argument("--be-code", default="")
        parser.add_argument("--address-id", default="")
        parser.add_argument("--product-code", default="")
        parser.add_argument("--menu-fingerprint", default="")

    put = sub.add_parser("put", help="store a snapshot payload from a local file")
    add_key_args(put)
    put.add_argument("--payload", required=True, help="local JSON file (already fetched by the caller)")
    put.add_argument("--ttl-seconds", type=int, default=None)
    put.add_argument("--expires-at", default=None)
    put.add_argument("--source", default="")

    get = sub.add_parser("get", help="print a cached payload if every dimension still matches")
    add_key_args(get)
    get.add_argument("--ttl-seconds", type=int, default=None)
    get.add_argument("--out", default=None, help="write payload to this path instead of stdout")

    fail = sub.add_parser("fail", help="record a failure so the entry is not reused")
    add_key_args(fail)
    fail.add_argument("--error", required=True)

    inv = sub.add_parser("invalidate", help="delete one entry (and its failure marker)")
    add_key_args(inv)

    sub.add_parser("stats", help="count fresh / expired / failed entries")
    pr = sub.add_parser("prune", help="drop entries expired for more than a day")
    pr.add_argument("--keep-expired-seconds", type=int, default=86400)

    args = ap.parse_args(argv)
    cache = SnapshotCache(args.root)

    if args.cmd == "stats":
        print(json.dumps(cache.stats(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "prune":
        removed = cache.prune(keep_expired_seconds=args.keep_expired_seconds)
        print(json.dumps({"removed": removed}, ensure_ascii=False))
        return 0

    try:
        key = build_key(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.cmd == "put":
        try:
            payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"payload error: {exc}", file=sys.stderr)
            return 2
        path = cache.put(key, payload, ttl_seconds=args.ttl_seconds, expires_at=args.expires_at, source=args.source)
        print(json.dumps({"stored": str(path), "key": key.label()}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "get":
        result = cache.get(key, ttl_seconds=getattr(args, "ttl_seconds", None))
        if not result.ok:
            print(json.dumps({"status": result.status, "reason": result.reason, "key": key.label()}, ensure_ascii=False, indent=2))
            return 1
        text = json.dumps(result.payload, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).write_text(text + "\n", encoding="utf-8")
            print(json.dumps({"status": "hit", "wrote": args.out}, ensure_ascii=False))
        else:
            print(text)
        return 0
    if args.cmd == "fail":
        path = cache.fail(key, args.error)
        print(json.dumps({"failed": str(path), "key": key.label()}, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "invalidate":
        cache.invalidate(key)
        print(json.dumps({"invalidated": key.label()}, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
