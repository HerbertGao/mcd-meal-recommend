#!/usr/bin/env python3
"""Load reusable meal profiles for score_lunch / build_candidates.

Profiles live in profiles/*.json. A new scene (e.g. dinner) = copy a JSON
and edit fields; core scorers stay generic and key off mealPeriod + prefs.
"""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
PROFILES_DIR = SCRIPT_DIR / "profiles"


def profiles_dir() -> Path:
    return PROFILES_DIR


def list_profiles() -> List[str]:
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def resolve_profile_path(name_or_path: str) -> Path:
    raw = Path(name_or_path)
    if raw.is_file():
        return raw
    # bare name → profiles/<name>.json
    stem = raw.stem if raw.suffix == ".json" else raw.name
    cand = PROFILES_DIR / f"{stem}.json"
    if cand.is_file():
        return cand
    raise FileNotFoundError(
        f"profile not found: {name_or_path!r} (looked at {cand}; available={list_profiles()})"
    )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Dict merge; overlay wins. Nested dicts merge; lists/scalars replace."""
    out = deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def resolve_history_path(path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = SCRIPT_DIR / p
    return p


def empty_history_doc() -> Dict[str, Any]:
    """Canonical on-disk history: mains/sides/drinks tracks + flat entries."""
    return {"version": 2, "mains": [], "sides": [], "drinks": [], "entries": []}


def _entry_at_iso(entry: Dict[str, Any]) -> str:
    at = entry.get("at")
    if at:
        return str(at)
    d = entry.get("date")
    if d:
        return f"{d}T12:00:00+08:00"
    return ""


def normalize_history_doc(data: Any) -> Dict[str, Any]:
    """Accept legacy list[{date,rotationKey,mainCode}] or v2 {mains,sides,entries}."""
    doc = empty_history_doc()
    if data is None:
        return doc
    if isinstance(data, list):
        for h in data:
            if not isinstance(h, dict):
                continue
            entry = {
                "date": str(h.get("date") or "")[:10],
                "rotationKey": str(h.get("rotationKey") or ""),
                "priceFen": h.get("priceFen"),
                "mainCode": str(h["mainCode"]) if h.get("mainCode") else "",
                "sideCode": str(h["sideCode"]) if h.get("sideCode") else "",
                "at": _entry_at_iso(h),
            }
            doc["entries"].append(entry)
            if entry["mainCode"]:
                doc["mains"].append(
                    {
                        "code": entry["mainCode"],
                        "name": str(h.get("mainName") or ""),
                        "at": entry["at"] or entry["date"],
                        "rotationKey": entry["rotationKey"],
                    }
                )
            if entry["sideCode"]:
                doc["sides"].append(
                    {
                        "code": entry["sideCode"],
                        "name": str(h.get("sideName") or ""),
                        "at": entry["at"] or entry["date"],
                        "rotationKey": entry["rotationKey"],
                    }
                )
            drink_code = str(h.get("drinkCode") or "")
            if drink_code:
                doc["drinks"].append(
                    {
                        "code": drink_code,
                        "name": str(h.get("drinkName") or ""),
                        "at": entry["at"] or entry["date"],
                        "rotationKey": entry["rotationKey"],
                    }
                )
                entry["drinkCode"] = drink_code
        return doc
    if not isinstance(data, dict):
        return doc
    if "version" in data or "mains" in data or "sides" in data or "drinks" in data or "entries" in data:
        doc["version"] = int(data.get("version") or 2)
        doc["mains"] = [dict(x) for x in (data.get("mains") or []) if isinstance(x, dict)]
        doc["sides"] = [dict(x) for x in (data.get("sides") or []) if isinstance(x, dict)]
        doc["drinks"] = [dict(x) for x in (data.get("drinks") or []) if isinstance(x, dict)]
        doc["entries"] = [
            dict(x)
            for x in (data.get("entries") or data.get("history") or [])
            if isinstance(x, dict)
        ]
        # backfill empty tracks from entries (legacy files)
        if not doc["mains"]:
            for h in doc["entries"]:
                mc = str(h.get("mainCode") or "")
                if not mc:
                    continue
                doc["mains"].append(
                    {
                        "code": mc,
                        "name": str(h.get("mainName") or ""),
                        "at": _entry_at_iso(h) or str(h.get("date") or ""),
                        "rotationKey": str(h.get("rotationKey") or ""),
                    }
                )
        if not doc["sides"]:
            for h in doc["entries"]:
                codes = []
                sc = str(h.get("sideCode") or "")
                if sc:
                    codes.append(sc)
                extra = h.get("extraSideCodes") or []
                if isinstance(extra, list):
                    for code in extra:
                        c = str(code or "")
                        if c and c not in codes:
                            codes.append(c)
                for sc in codes:
                    doc["sides"].append(
                        {
                            "code": sc,
                            "name": str(h.get("sideName") or "") if sc == str(h.get("sideCode") or "") else "",
                            "at": _entry_at_iso(h) or str(h.get("date") or ""),
                            "rotationKey": str(h.get("rotationKey") or ""),
                        }
                    )
        if not doc["drinks"]:
            for h in doc["entries"]:
                codes = []
                dc = str(h.get("drinkCode") or "")
                if dc:
                    codes.append(dc)
                extra = h.get("extraDrinkCodes") or []
                if isinstance(extra, list):
                    for code in extra:
                        c = str(code or "")
                        if c and c not in codes:
                            codes.append(c)
                for dc in codes:
                    doc["drinks"].append(
                        {
                            "code": dc,
                            "name": str(h.get("drinkName") or "") if dc == str(h.get("drinkCode") or "") else "",
                            "at": _entry_at_iso(h) or str(h.get("date") or ""),
                            "rotationKey": str(h.get("rotationKey") or ""),
                        }
                    )
        return doc
    # unknown dict → try nested
    return normalize_history_doc(data.get("history") or data.get("entries") or [])


def load_history_file(path: Optional[str]) -> List[Dict[str, Any]]:
    """Backward-compatible: return flat entries list for request.history."""
    doc = load_history_doc(path)
    return list(doc.get("entries") or [])


def load_history_doc(path: Optional[str]) -> Dict[str, Any]:
    p = resolve_history_path(path)
    if not p or not p.is_file():
        return empty_history_doc()
    return normalize_history_doc(load_json(p))


def save_history_doc(path: str, doc: Dict[str, Any]) -> Path:
    p = resolve_history_path(path)
    assert p is not None
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": int(doc.get("version") or 2),
        "mains": list(doc.get("mains") or []),
        "sides": list(doc.get("sides") or []),
        "drinks": list(doc.get("drinks") or []),
        "entries": list(doc.get("entries") or []),
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def append_history_pick(
    path: str,
    *,
    rotation_key: str,
    price_fen: int,
    main_code: str = "",
    main_name: str = "",
    side_code: str = "",
    side_name: str = "",
    drink_code: str = "",
    drink_name: str = "",
    at: Optional[str] = None,
    seed: Optional[int] = None,
) -> Dict[str, Any]:
    """Append one pick onto mains/sides tracks + entries; return updated doc."""
    from datetime import datetime, timedelta, timezone

    cst = timezone(timedelta(hours=8))
    now = at or datetime.now(cst).replace(microsecond=0).isoformat()
    day = str(now)[:10]
    doc = load_history_doc(path)
    entry: Dict[str, Any] = {
        "date": day,
        "at": now,
        "rotationKey": str(rotation_key or ""),
        "priceFen": int(price_fen or 0),
        "mainCode": str(main_code or ""),
        "sideCode": str(side_code or ""),
        "drinkCode": str(drink_code or ""),
    }
    if seed is not None:
        entry["seed"] = int(seed)
    if main_name:
        entry["mainName"] = main_name
    if side_name:
        entry["sideName"] = side_name
    if drink_name:
        entry["drinkName"] = drink_name
    doc.setdefault("entries", []).append(entry)
    if main_code:
        doc.setdefault("mains", []).append(
            {
                "code": str(main_code),
                "name": str(main_name or ""),
                "at": now,
                "rotationKey": str(rotation_key or ""),
                **({"seed": int(seed)} if seed is not None else {}),
            }
        )
    if side_code:
        doc.setdefault("sides", []).append(
            {
                "code": str(side_code),
                "name": str(side_name or ""),
                "at": now,
                "rotationKey": str(rotation_key or ""),
                **({"seed": int(seed)} if seed is not None else {}),
            }
        )
    if drink_code:
        doc.setdefault("drinks", []).append(
            {
                "code": str(drink_code),
                "name": str(drink_name or ""),
                "at": now,
                "rotationKey": str(rotation_key or ""),
                **({"seed": int(seed)} if seed is not None else {}),
            }
        )
    save_history_doc(path, doc)
    return doc



def _cst_now():
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=8)))


def normalize_reservation(raw: Any) -> Dict[str, Any]:
    """Normalize reservation config.

    Preferred shape: {enabled, time: "HH:mm", dayOffset: int}.
    Legacy flat fields (reservationTime / reservationDate / reservation.enabled) also accepted
    when passed via normalize_fulfillment / profile_to_request.
    """
    out: Dict[str, Any] = {"enabled": False, "time": None, "dayOffset": 0}
    if raw is None:
        return out
    if isinstance(raw, dict):
        en = raw.get("enabled")
        if en is None and (raw.get("time") or raw.get("dayOffset") not in (None, 0)):
            en = True
        out["enabled"] = bool(en)
        t = raw.get("time") or raw.get("reservationTime")
        if t:
            out["time"] = str(t).strip()
        if raw.get("dayOffset") is not None:
            out["dayOffset"] = int(raw.get("dayOffset") or 0)
        # allow absolute date override inside reservation block
        if raw.get("date"):
            out["date"] = str(raw["date"]).strip()
        return out
    if isinstance(raw, bool):
        out["enabled"] = raw
        return out
    return out


def compute_reservation_date(
    reservation: Optional[Dict[str, Any]] = None,
    *,
    reservation_date: Any = None,
    reservation_time: Any = None,
    now=None,
) -> Optional[str]:
    """Return MCP reservationDate `yyyy-MM-dd HH:mm`, or None if not reserved."""
    from datetime import datetime, timedelta

    # Explicit absolute date wins (overlay / materialized request)
    if reservation_date not in (None, ""):
        s = str(reservation_date).strip()
        # already full → keep; date-only → append time
        if len(s) >= 16:
            return s[:16] if s[10:11] == " " else s
        t = None
        if isinstance(reservation, dict):
            t = reservation.get("time")
        t = t or reservation_time or "07:00"
        return f"{s[:10]} {str(t)[:5]}"

    res = normalize_reservation(reservation)
    if reservation_time and not res.get("time"):
        res["time"] = str(reservation_time).strip()
    if not res.get("enabled"):
        return None
    if res.get("date"):
        t = res.get("time") or "07:00"
        d = str(res["date"])[:10]
        return f"{d} {str(t)[:5]}"
    t = res.get("time") or "07:00"
    day_offset = int(res.get("dayOffset") or 0)
    base = now or _cst_now()
    target = (base + timedelta(days=day_offset)).date()
    return f"{target.isoformat()} {str(t)[:5]}"


def normalize_fulfillment(profile: Dict[str, Any]) -> Dict[str, Any]:
    """Extract fulfillment fields from a profile (or overlay). Does not invent a store."""
    dining = str(profile.get("dining") or "").strip() or None
    be_type = profile.get("beType")
    order_type = profile.get("orderType")
    # infer dining from beType when omitted
    if not dining:
        if be_type in (2, "2"):
            dining = "delivery"
        elif be_type in (1, "1", None):
            dining = "eat-in"
    address_name = profile.get("addressName") or profile.get("addressLabel")
    address_id = profile.get("addressId")
    be_code = profile.get("beCode")
    store = profile.get("storeCode")
    res = normalize_reservation(profile.get("reservation"))
    # legacy flat fields
    if profile.get("reservationTime") and not res.get("time"):
        res["time"] = str(profile.get("reservationTime")).strip()
    # Legacy: flat reservationDate/Time without a reservation object ⇒ treat as enabled
    if not isinstance(profile.get("reservation"), dict):
        if profile.get("reservationDate") not in (None, "") or profile.get("reservationTime"):
            res["enabled"] = True
    # delivery defaults for types when dining says so
    if dining == "delivery":
        if be_type is None:
            be_type = 2
        if order_type is None:
            order_type = 2
    elif dining in ("eat-in", "take-in-store"):
        if be_type is None:
            be_type = 1
        if order_type is None:
            order_type = 1
    reservation_date = compute_reservation_date(
        res,
        reservation_date=profile.get("reservationDate"),
        reservation_time=profile.get("reservationTime") or res.get("time"),
    )
    out: Dict[str, Any] = {
        "dining": dining or "eat-in",
        "beType": int(be_type if be_type is not None else 1),
        "orderType": int(order_type if order_type is not None else 1),
        "reservation": res,
    }
    if store not in (None, ""):
        out["storeCode"] = str(store)
    if be_code not in (None, ""):
        out["beCode"] = str(be_code)
    if address_id not in (None, ""):
        out["addressId"] = str(address_id)
    if address_name not in (None, ""):
        out["addressName"] = str(address_name)
        out["addressLabel"] = str(address_name)  # alias for older callers
    if res.get("time"):
        out["reservationTime"] = str(res["time"])[:5]
    if reservation_date:
        out["reservationDate"] = reservation_date
    return out


def fulfillment_summary(req_or_profile: Dict[str, Any]) -> Dict[str, Any]:
    """Human/agent-facing fulfillment snapshot (no candidates)."""
    f = normalize_fulfillment(req_or_profile)
    # merge top-level if already materialized
    for k in (
        "storeCode",
        "beCode",
        "addressId",
        "addressName",
        "addressLabel",
        "reservationDate",
        "reservationTime",
        "dining",
        "beType",
        "orderType",
        "mealPeriod",
        "budgetFen",
        "candidateFamilies",
        "profileId",
        "id",
    ):
        if req_or_profile.get(k) not in (None, "") and k not in f:
            f[k] = req_or_profile[k]
        elif req_or_profile.get(k) not in (None, "") and k in (
            "mealPeriod",
            "budgetFen",
            "candidateFamilies",
            "profileId",
            "id",
            "storeCode",
        ):
            f[k] = req_or_profile[k]
    pid = req_or_profile.get("profileId") or req_or_profile.get("id")
    return {
        "profileId": pid,
        "mealPeriod": req_or_profile.get("mealPeriod") or f.get("mealPeriod"),
        "dining": f.get("dining"),
        "storeCode": f.get("storeCode") or req_or_profile.get("storeCode"),
        "beType": f.get("beType"),
        "orderType": f.get("orderType"),
        "beCode": f.get("beCode"),
        "addressId": f.get("addressId"),
        "addressName": f.get("addressName") or f.get("addressLabel"),
        "reservation": f.get("reservation"),
        "reservationDate": f.get("reservationDate") or req_or_profile.get("reservationDate"),
        "budgetFen": req_or_profile.get("budgetFen"),
        "candidateFamilies": req_or_profile.get("candidateFamilies"),
    }


def profile_to_request(profile: Dict[str, Any], overlay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Materialize a RecommendRequest-shaped dict from a profile (+ optional overlay).

    Fulfillment (dining / store / delivery / reservation) is read from the profile —
    never invent a scene-specific store beyond soft type defaults.
    """
    ful = normalize_fulfillment(profile)
    store = str(ful.get("storeCode") or profile.get("storeCode") or "")
    req: Dict[str, Any] = {
        "storeCode": store,
        "beType": int(ful.get("beType") or 1),
        "orderType": int(ful.get("orderType") or 1),
        "budgetFen": int(profile.get("budgetFen") or 3500),
        "dining": str(ful.get("dining") or "eat-in"),
        "mealPeriod": str(profile.get("mealPeriod") or "lunch"),
        "prefs": deepcopy(profile.get("prefs") or {}),
        "selection": deepcopy(profile.get("selection") or {
            "mode": "rank_then_random",
            "topK": 5,
            "random": "uniform",
            "diversifyByMain": True,
            "maxPerMainInTopK": 1,
        }),
        "reservation": deepcopy(ful.get("reservation") or {"enabled": False}),
    }
    for k in ("beCode", "addressId", "addressName", "addressLabel", "reservationDate", "reservationTime"):
        if ful.get(k) not in (None, ""):
            req[k] = ful[k]
    if "satiety" in profile:
        req["satiety"] = deepcopy(profile["satiety"])
    if "historyWindowDays" in profile:
        req["historyWindowDays"] = int(profile["historyWindowDays"])
    if "historyWindowPicks" in profile:
        req["historyWindowPicks"] = int(profile["historyWindowPicks"])
    # history: inline wins; else historyPath (v2 mains/sides tracks + entries)
    if isinstance(profile.get("history"), (list, dict)):
        doc = normalize_history_doc(profile["history"])
        req["history"] = deepcopy(doc.get("entries") or [])
        req["historyTracks"] = {
            "mains": deepcopy(doc.get("mains") or []),
            "sides": deepcopy(doc.get("sides") or []),
            "drinks": deepcopy(doc.get("drinks") or []),
        }
        if profile.get("historyPath"):
            req["historyPath"] = profile["historyPath"]
    else:
        path = profile.get("historyPath")
        if path:
            req["historyPath"] = path
            doc = load_history_doc(path)
            req["history"] = deepcopy(doc.get("entries") or [])
            req["historyTracks"] = {
                "mains": deepcopy(doc.get("mains") or []),
                "sides": deepcopy(doc.get("sides") or []),
                "drinks": deepcopy(doc.get("drinks") or []),
            }
    # pass-through builder hints (ignored by schema consumers that strip unknown)
    if "candidateFamilies" in profile:
        req["candidateFamilies"] = deepcopy(profile["candidateFamilies"])
    if "profileId" in profile:
        req["profileId"] = profile["profileId"]
    elif profile.get("id"):
        req["profileId"] = profile["id"]
    if "paths" in profile:
        req["_paths"] = deepcopy(profile["paths"])
    if overlay:
        req = deep_merge(req, overlay)
    store = str(req.get("storeCode") or "")
    if not store.isdigit():
        raise ValueError("storeCode is required and must contain digits only; edit the profile or overlay")
    req["storeCode"] = store
    # ensure mealPeriod mirrored into prefs for scorers that peek prefs only
    prefs = req.setdefault("prefs", {})
    prefs.setdefault("mealPeriod", req.get("mealPeriod"))
    return req


def load_profile(name_or_path: str, overlay: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    path = resolve_profile_path(name_or_path)
    profile = load_json(path)
    if not isinstance(profile, dict):
        raise ValueError(f"profile root must be object: {path}")
    profile.setdefault("id", path.stem)
    req = profile_to_request(profile, overlay)
    req["_profilePath"] = str(path)
    return req


_FULFILLMENT_KEYS = {
    "dining",
    "storeCode",
    "beCode",
    "beType",
    "orderType",
    "addressId",
    "addressName",
    "addressLabel",
    "reservation",
    "reservationDate",
    "reservationTime",
    "budgetFen",
    "selection",
    "mealPeriod",
    "candidateFamilies",
    "history",
    "historyPath",
    "historyWindowDays",
    "historyWindowPicks",
    "satiety",
    "prefs",
}


def apply_prefs_file(req: Dict[str, Any], prefs_path: str) -> Dict[str, Any]:
    data = load_json(Path(prefs_path))
    if not isinstance(data, dict):
        raise ValueError("prefs-file root must be object")
    # allow either full request/fulfillment fragment or bare {prefs-like keys}
    if any(k in data for k in _FULFILLMENT_KEYS):
        merged = deep_merge(req, data)
        # If overlay touches reservation config but not an absolute reservationDate,
        # drop stale materialized date so normalize recomputes from enabled/time/dayOffset.
        if "reservation" in data and "reservationDate" not in data:
            merged.pop("reservationDate", None)
            # also drop flat time if nested reservation.time provided
            if isinstance(data.get("reservation"), dict) and data["reservation"].get("time"):
                merged["reservationTime"] = data["reservation"]["time"]
        ful = normalize_fulfillment(merged)
        for k, v in ful.items():
            if v not in (None, ""):
                merged[k] = v
        if not (ful.get("reservation") or {}).get("enabled"):
            merged.pop("reservationDate", None)
        return merged
    return deep_merge(req, {"prefs": data})


def main(argv: Optional[List[str]] = None) -> int:
    """CLI: print fulfillment summary for one or more profiles."""
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="mcd-lunch profile_lib helpers")
    ap.add_argument("profiles", nargs="*", default=["lunch", "breakfast"], help="profile names")
    ap.add_argument("--prefs-file", help="optional overlay JSON")
    ap.add_argument("--pretty", action="store_true", default=True)
    args = ap.parse_args(argv)
    rows = []
    try:
        for name in args.profiles:
            req = load_profile(name)
            if args.prefs_file:
                req = apply_prefs_file(req, args.prefs_file)
            rows.append(fulfillment_summary(req))
    except (OSError, ValueError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(rows if len(rows) != 1 else rows[0], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
