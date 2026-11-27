#!/usr/bin/env python3
"""Offline lunch recommend run orchestration (no MCP calls).

Ingests MCP tool results from caller-written files, chains build/score/calc-request
generation, checks prewarm fingerprints, and merges calculate-price snapshots.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import build_candidates as bc
import combo_edit
import score_lunch
from build_candidates import resolve_families
from mcp_cache import KIND_DETAIL, KIND_MENU, CacheKey, SnapshotCache, stable_menu_fingerprint
from mcp_contract import ContractError, load_snapshot, normalize_menu, rounds_of
from profile_lib import SCRIPT_DIR, load_profile, profile_to_request

CST = timezone(timedelta(hours=8))
PRESENT_LABELS = ("A", "B", "C", "D", "E")
PREWARM_ARTIFACT_JSON = frozenset({"manifest.json", "needed-details.json", "menu-slim.json"})


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def unwrap_mcp_envelope(raw: Any) -> Any:
    """Unwrap {success, data, datetime, traceId} written by CallDynamicTool."""
    if not isinstance(raw, dict):
        return raw
    if "data" in raw and any(k in raw for k in ("success", "traceId", "datetime")):
        return raw["data"]
    return raw


def slim_menu(menu: Any) -> Dict[str, Any]:
    return normalize_menu(deepcopy(menu))


def is_lunch_menu(menu: Mapping[str, Any]) -> bool:
    """Heuristic: lunch prewarm must not promote breakfast-only snapshots."""
    mp = str(menu.get("mealPeriod") or "").lower()
    if mp in ("breakfast", "bf", "早餐"):
        return False
    if mp in ("lunch", "午餐", ""):
        # empty mealPeriod on slim menus is normal for lunch eat-in
        pass
    text = json.dumps(menu, ensure_ascii=False)
    if re.search(r"早餐|麦满分", text) and not re.search(r"午餐|burger|堡|随心配", text, re.I):
        return False
    return True


def load_profile_request(profile_name: str) -> Dict[str, Any]:
    path = Path(profile_name)
    if path.is_file():
        prof = _load_json(path)
        return profile_to_request(prof) if prof.get("id") else prof
    try:
        return load_profile(profile_name)
    except FileNotFoundError:
        path = SCRIPT_DIR / "profiles" / "templates" / f"{profile_name}.example.json"
        if not path.is_file():
            raise
        prof = _load_json(path)
        if str(prof.get("storeCode") or "") == "YOUR_STORE_CODE":
            prof = deepcopy(prof)
            prof["storeCode"] = "400001"
        return profile_to_request(prof)


def cache_scope_from_request(req: Mapping[str, Any], *, reservation_date: str = "") -> Dict[str, str]:
    explicit = str(reservation_date or "").strip()
    return {
        "storeCode": str(req.get("storeCode") or ""),
        "dining": str(req.get("dining") or "eat-in"),
        "mealPeriod": str(req.get("mealPeriod") or "lunch"),
        "reservationDate": explicit if explicit else str(req.get("reservationDate") or ""),
        "beCode": str(req.get("beCode") or ""),
        "addressId": str(req.get("addressId") or ""),
    }


def parse_run_seed(spec: str) -> int:
    """Accept YYYYMMDD, YYYY-MM-DD, or integer RNG seeds."""
    text = str(spec).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return int(text.replace("-", ""))
    if re.match(r"^\d{8}$", text):
        return int(text)
    return int(text)


def calc_payload_from_file(raw_doc: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """Unwrap calculate-price MCP JSON; keep envelope datetime for quote time."""
    envelope_dt = str(raw_doc.get("datetime") or "") if isinstance(raw_doc, dict) else ""
    calc_raw = unwrap_mcp_envelope(raw_doc)
    if isinstance(calc_raw, dict) and isinstance(calc_raw.get("data"), dict):
        calc_data: Dict[str, Any] = calc_raw["data"]
    elif isinstance(calc_raw, dict):
        calc_data = calc_raw
    else:
        calc_data = {}
    quoted_at = str(calc_data.get("quotedAt") or calc_data.get("fetchedAt") or "") or envelope_dt or None
    return calc_data, quoted_at


def needed_detail_codes(menu: Dict[str, Any], req: Mapping[str, Any], details: Optional[Dict[str, Any]] = None) -> List[str]:
    details = details or {}
    budget = int(req.get("budgetFen") or 3500)
    families = resolve_families(dict(req))
    codes: set = set()
    codes.update(bc.discover_mcard_four_codes(menu, details))
    codes.update(bc.discover_dabao_codes(menu, details))
    legs = bc.discover_dual_legs(menu, details)
    codes.update(legs.left)
    codes.update(legs.right)
    for product in bc.iter_menu_products(menu):
        if bc._dual_signaled(product):
            codes.add(str(product["code"]))
    if "classic_three" in families:
        codes.update(bc.discover_classic_three_codes(menu, budget, details))
    return sorted(c for c in codes if c)


def cmd_ingest_menu(args: argparse.Namespace) -> int:
    req = load_profile_request(args.profile)
    raw = unwrap_mcp_envelope(_load_json(Path(args.raw)))
    menu = slim_menu(raw)
    if not is_lunch_menu(menu):
        print("refusing ingest: payload does not look like a lunch menu", file=sys.stderr)
        return 2
    fp = stable_menu_fingerprint(menu)
    scope = cache_scope_from_request(req, reservation_date=str(getattr(args, "reservation_date", "") or ""))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    slim_path = out_dir / "menu-slim.json"
    _write_json(slim_path, menu)
    needed = needed_detail_codes(menu, req)
    _write_json(
        out_dir / "needed-details.json",
        {"menuFingerprint": fp, "productCodes": needed, "count": len(needed)},
    )
    manifest = {
        "profile": args.profile,
        "ingestedAt": datetime.now(CST).replace(microsecond=0).isoformat(),
        "menuFingerprint": fp,
        "menuPath": str(slim_path),
        "neededDetails": needed,
        **scope,
    }
    _write_json(out_dir / "manifest.json", manifest)
    cache = SnapshotCache(args.cache_root)
    key = CacheKey(
        kind=KIND_MENU,
        store_code=scope["storeCode"],
        dining=scope["dining"],
        meal_period=scope["mealPeriod"],
        reservation_date=scope["reservationDate"],
        be_code=scope["beCode"],
        address_id=scope["addressId"],
    )
    put_kwargs: Dict[str, Any] = {"source": "recommend_run.ingest-menu"}
    if args.expires_at:
        put_kwargs["expires_at"] = args.expires_at
    cache.put(key, menu, **put_kwargs)
    print(json.dumps({"menuFingerprint": fp, "manifest": str(out_dir / "manifest.json")}, ensure_ascii=False))
    return 0


def cmd_ingest_details(args: argparse.Namespace) -> int:
    req = load_profile_request(args.profile)
    scope = cache_scope_from_request(req, reservation_date=str(getattr(args, "reservation_date", "") or ""))
    menu_fp = str(args.menu_fingerprint or "").strip()
    if not menu_fp:
        print("menu-fingerprint required", file=sys.stderr)
        return 2
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir) if args.out_dir else raw_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = SnapshotCache(args.cache_root)
    ingested: List[str] = []
    for path in sorted(raw_dir.glob("*.json")):
        if path.name in PREWARM_ARTIFACT_JSON:
            continue
        try:
            detail = unwrap_mcp_envelope(_load_json(path))
            rounds_of(detail, path=str(path))
        except (ContractError, ValueError) as exc:
            print(f"skip {path.name}: {exc}", file=sys.stderr)
            continue
        code = path.stem
        dest = out_dir / f"{code}.json"
        _write_json(dest, detail)
        key = CacheKey(
            kind=KIND_DETAIL,
            store_code=scope["storeCode"],
            dining=scope["dining"],
            meal_period=scope["mealPeriod"],
            reservation_date=scope["reservationDate"],
            be_code=scope["beCode"],
            address_id=scope["addressId"],
            product_code=code,
            menu_fingerprint=menu_fp,
        )
        cache.put(key, detail, source="recommend_run.ingest-details")
        ingested.append(code)
    print(json.dumps({"ingested": ingested, "count": len(ingested)}, ensure_ascii=False, indent=2))
    return 0


def _prewarm_manifest(prewarm_dir: Path) -> Optional[Dict[str, Any]]:
    path = prewarm_dir / "manifest.json"
    if not path.is_file():
        return None
    data = _load_json(path)
    return data if isinstance(data, dict) else None


def cmd_check_menu(args: argparse.Namespace) -> int:
    raw = unwrap_mcp_envelope(_load_json(Path(args.raw)))
    try:
        noon_menu = slim_menu(raw)
    except ContractError as exc:
        print(json.dumps({"status": "unusable", "reason": str(exc)}, ensure_ascii=False))
        return 2
    noon_fp = stable_menu_fingerprint(noon_menu)
    prewarm_dir = Path(args.prewarm_dir)
    manifest = _prewarm_manifest(prewarm_dir)
    if not manifest:
        print(json.dumps({"status": "fallback", "reason": "no_prewarm_manifest"}, ensure_ascii=False))
        return 2
    prewarm_fp = str(manifest.get("menuFingerprint") or "")
    if not prewarm_fp or prewarm_fp != noon_fp:
        print(
            json.dumps(
                {"status": "fallback", "reason": "fingerprint_mismatch", "noon": noon_fp, "prewarm": prewarm_fp},
                ensure_ascii=False,
            )
        )
        return 2
    needed_path = prewarm_dir / "needed-details.json"
    needed = []
    if needed_path.is_file():
        doc = _load_json(needed_path)
        needed = list(doc.get("productCodes") or [])
    elif manifest.get("neededDetails"):
        needed = list(manifest.get("neededDetails") or [])
    details_dir = prewarm_dir
    missing = [code for code in needed if not (details_dir / f"{code}.json").is_file()]
    if missing:
        print(json.dumps({"status": "topup", "missingProductCodes": missing, "menuFingerprint": noon_fp}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "hit", "menuFingerprint": noon_fp}, ensure_ascii=False))
    return 0


def _load_details_dir(path: Path) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    for p in path.glob("*.json"):
        if p.name in PREWARM_ARTIFACT_JSON:
            continue
        try:
            detail = load_snapshot(p)
            rounds_of(detail, path=str(p))
        except ContractError:
            continue
        details[p.stem] = detail
    return details


def _build_request(
    profile: str,
    menu_path: Path,
    details_dir: Path,
    exposure_path: Optional[str],
) -> Dict[str, Any]:
    req = load_profile_request(profile)
    menu = normalize_menu(load_snapshot(menu_path), path=str(menu_path))
    details = _load_details_dir(details_dir)
    families = resolve_families(req)
    cands = bc.expand(
        details,
        menu,
        req.get("prefs") or {},
        families=families,
        budget_fen=int(req.get("budgetFen") or 3500),
    )
    trimmed = bc.trim_candidates(cands, 100, families)
    out = deepcopy(req)
    for k in list(out.keys()):
        if str(k).startswith("_"):
            out.pop(k, None)
    out["candidateFamilies"] = families
    out["candidates"] = trimmed
    out["menuSnapshot"] = menu
    out["mealDetails"] = details
    if exposure_path:
        out["exposurePath"] = exposure_path
    return out


def _presented_response(cand: score_lunch.ScoredCandidate, ranked: List[score_lunch.ScoredCandidate], req: Dict[str, Any], warnings: List[str], mode: str, label: str) -> Dict[str, Any]:
    alts = [c for c in ranked if c.rotation_key != cand.rotation_key][:5]
    resp = score_lunch.build_response(cand, alts, ranked, req, warnings, False, mode)
    resp["presentLabel"] = label
    return resp


def cmd_build_score_calc(args: argparse.Namespace) -> int:
    req = _build_request(args.profile, Path(args.menu), Path(args.details_dir), args.exposure_path)
    seed = parse_run_seed(args.seed) if args.seed else None
    warnings: List[str] = []
    passed: List[score_lunch.ScoredCandidate] = []
    for c in req.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        cand = score_lunch.ScoredCandidate(
            rotation_key=str(c.get("rotationKey") or ""),
            human_summary=str(c.get("humanSummary") or ""),
            price_fen=int(c.get("listPriceFen") or c.get("priceFen") or 0),
            items=deepcopy(c.get("items") or []),
            tags=list(c.get("tags") or []),
            source="candidates",
            meta={"primaryMainCode": c.get("primaryMainCode")},
        )
        reason = score_lunch.hard_filter(cand, req)
        if reason:
            continue
        score_lunch.score_candidate(cand, req)
        passed.append(cand)
    score_lunch.apply_family_rotation(passed, req)
    score_lunch.apply_exposure_rotation(passed, req)
    ranked = sorted(passed, key=lambda c: (-c.score, c.price_fen, c.rotation_key))
    presented_cands = score_lunch.select_presented(ranked, req, seed=seed)
    mode = str((req.get("selection") or {}).get("mode") or "rank_then_random")
    presented: List[Dict[str, Any]] = []
    for i, cand in enumerate(presented_cands):
        label = PRESENT_LABELS[i] if i < len(PRESENT_LABELS) else str(i + 1)
        presented.append(_presented_response(cand, ranked, req, warnings, mode, label))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    score_doc = {
        "ok": bool(presented),
        "generatedAt": score_lunch.now_cst_iso(),
        "profileId": req.get("profileId"),
        "storeCode": req.get("storeCode"),
        "mealPeriod": req.get("mealPeriod"),
        "seed": seed,
        "primary": presented[0] if presented else None,
        "presented": presented,
        "rankedCount": len(ranked),
        "warnings": warnings,
    }
    score_path = out_dir / "score.json"
    _write_json(score_path, score_doc)
    calc_dir = out_dir / "calc-requests"
    calc_dir.mkdir(parents=True, exist_ok=True)
    for i, row in enumerate(presented):
        label = PRESENT_LABELS[i] if i < len(PRESENT_LABELS) else str(i + 1)
        calc_req = {
            "storeCode": req.get("storeCode"),
            "beType": req.get("beType"),
            "orderType": req.get("orderType"),
            "dining": req.get("dining"),
            "mealPeriod": req.get("mealPeriod"),
            "reservationDate": req.get("reservationDate"),
            "rotationKey": row.get("rotationKey"),
            "items": row.get("items"),
            "presentLabel": label,
        }
        _write_json(calc_dir / f"{label}.json", calc_req)
    print(json.dumps({"score": str(score_path), "presented": len(presented), "calcRequests": str(calc_dir)}, ensure_ascii=False))
    return 0 if score_doc["ok"] else 1


def cmd_merge_quotes(args: argparse.Namespace) -> int:
    score_path = Path(args.score)
    score_doc = _load_json(score_path)
    calc_raw_dir = Path(args.calc_raw_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_sets: List[Dict[str, Any]] = []
    for row in score_doc.get("presented") or [score_doc.get("primary")]:
        if not isinstance(row, dict):
            continue
        label = str(row.get("presentLabel") or "A")
        calc_path = calc_raw_dir / f"{label}.json"
        if not calc_path.is_file():
            # accept envelope-wrapped calc file
            alt = next(calc_raw_dir.glob(f"*{label}*.json"), None)
            calc_path = alt if alt else calc_path
        if not calc_path.is_file():
            pending = dict(row)
            pending["payableFen"] = None
            pending["priceVerified"] = False
            pending["needsQuoteBeforeConfirm"] = True
            pending["priceState"] = "estimate"
            pending["quoteReason"] = "missing_calc_raw"
            merged_sets.append(pending)
            continue
        raw_doc = _load_json(calc_path)
        calc_data, quoted_at = calc_payload_from_file(raw_doc)
        calc_snap = {
            "rotationKey": row.get("rotationKey"),
            "humanSummary": row.get("humanSummary"),
            "items": calc_data.get("items") or row.get("items"),
            "priceFen": calc_data.get("productPrice") or calc_data.get("price") or row.get("priceFen"),
            "payableFen": calc_data.get("price"),
            "quotedAt": quoted_at,
            "storeCode": row.get("storeCode"),
            "dining": row.get("takeWayCodeHint") or row.get("dining") or "eat-in",
            "mealPeriod": row.get("mealPeriod"),
        }
        sync = combo_edit.sync_snapshots(row, calc_snap)
        if not sync.get("ok"):
            pending = dict(row)
            pending["payableFen"] = None
            pending["priceVerified"] = False
            pending["needsQuoteBeforeConfirm"] = True
            pending["syncError"] = sync.get("error")
            merged_sets.append(pending)
        else:
            merged_sets.append(sync["pending"])
    result = {
        "ok": all(bool(x.get("priceVerified")) for x in merged_sets) if merged_sets else False,
        "mergedAt": datetime.now(CST).replace(microsecond=0).isoformat(),
        "presented": merged_sets,
        "primary": merged_sets[0] if merged_sets else None,
    }
    out_path = out_dir / "merged-score.json"
    _write_json(out_path, result)
    print(json.dumps({"wrote": str(out_path), "presented": len(merged_sets)}, ensure_ascii=False))
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    req = load_profile_request(args.profile)
    scope = cache_scope_from_request(req)
    src_reservation = str(args.from_reservation or "").strip()
    dst_reservation = str(args.to_reservation or "")
    if not src_reservation:
        print("--from-reservation required (e.g. 'YYYY-MM-DD 12:00')", file=sys.stderr)
        return 2
    menu_path = Path(args.menu) if args.menu else None
    if menu_path and menu_path.is_file():
        menu = slim_menu(unwrap_mcp_envelope(_load_json(menu_path)))
        if not is_lunch_menu(menu):
            print("refusing promote: not a lunch menu", file=sys.stderr)
            return 2
    src_cache = SnapshotCache(args.from_cache_root)
    dst_cache = SnapshotCache(args.to_cache_root)
    src_menu_key = CacheKey(
        kind=KIND_MENU,
        store_code=scope["storeCode"],
        dining=scope["dining"],
        meal_period=scope["mealPeriod"],
        reservation_date=src_reservation,
        be_code=scope["beCode"],
        address_id=scope["addressId"],
    )
    hit = src_cache.get(src_menu_key)
    if not hit.ok:
        print(json.dumps({"status": "missing", "reason": hit.reason}, ensure_ascii=False))
        return 2
    menu_payload = hit.payload
    if not is_lunch_menu(menu_payload if isinstance(menu_payload, dict) else {}):
        print("refusing promote: cached menu is not lunch", file=sys.stderr)
        return 2
    menu_fp = stable_menu_fingerprint(menu_payload)
    dst_menu_key = CacheKey(
        kind=KIND_MENU,
        store_code=scope["storeCode"],
        dining=scope["dining"],
        meal_period=scope["mealPeriod"],
        reservation_date=dst_reservation,
        be_code=scope["beCode"],
        address_id=scope["addressId"],
    )
    dst_cache.put(dst_menu_key, menu_payload, source="recommend_run.promote")
    needed = needed_detail_codes(menu_payload, req)
    promoted_details = 0
    for code in needed:
        src_detail_key = CacheKey(
            kind=KIND_DETAIL,
            store_code=scope["storeCode"],
            dining=scope["dining"],
            meal_period=scope["mealPeriod"],
            reservation_date=src_reservation,
            be_code=scope["beCode"],
            address_id=scope["addressId"],
            product_code=code,
            menu_fingerprint=menu_fp,
        )
        detail_hit = src_cache.get(src_detail_key)
        if not detail_hit.ok:
            continue
        dst_detail_key = CacheKey(
            kind=KIND_DETAIL,
            store_code=scope["storeCode"],
            dining=scope["dining"],
            meal_period=scope["mealPeriod"],
            reservation_date=dst_reservation,
            be_code=scope["beCode"],
            address_id=scope["addressId"],
            product_code=code,
            menu_fingerprint=menu_fp,
        )
        dst_cache.put(dst_detail_key, detail_hit.payload, source="recommend_run.promote")
        promoted_details += 1
    print(
        json.dumps(
            {
                "menuFingerprint": menu_fp,
                "promotedDetails": promoted_details,
                "fromReservation": src_reservation,
                "toReservation": dst_reservation or "(immediate)",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def add_cache_root(parser: argparse.ArgumentParser, required: bool = True) -> None:
    parser.add_argument("--cache-root", "--root", dest="cache_root", required=required, help="isolated cache root")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Offline lunch recommend run steps (never calls MCP)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    im = sub.add_parser("ingest-menu")
    im.add_argument("--profile", required=True)
    im.add_argument("--raw", required=True, help="write_to_file path from query-meals")
    add_cache_root(im)
    im.add_argument("--out-dir", required=True)
    im.add_argument("--expires-at", default="", help="optional readability marker only")
    im.add_argument(
        "--reservation-date",
        default="",
        help='cache isolation override (e.g. "YYYY-MM-DD 12:00" for prewarm; default profile reservationDate)',
    )

    idet = sub.add_parser("ingest-details")
    idet.add_argument("--profile", required=True)
    idet.add_argument("--raw-dir", required=True)
    idet.add_argument("--menu-fingerprint", required=True)
    add_cache_root(idet)
    idet.add_argument("--out-dir", default="")
    idet.add_argument("--reservation-date", default="", help="same as ingest-menu")

    cm = sub.add_parser("check-menu")
    cm.add_argument("--profile", required=True)
    cm.add_argument("--raw", required=True, help="noon query-meals write_to_file")
    cm.add_argument("--prewarm-dir", required=True)

    bsc = sub.add_parser("build-score-calc")
    bsc.add_argument("--profile", required=True)
    bsc.add_argument("--seed", required=True, help="YYYYMMDD, YYYY-MM-DD, or integer seed")
    bsc.add_argument("--menu", required=True)
    bsc.add_argument("--details-dir", required=True)
    bsc.add_argument("--exposure-path", default="")
    bsc.add_argument("--out-dir", required=True)

    mq = sub.add_parser("merge-quotes")
    mq.add_argument("--profile", required=True)
    mq.add_argument("--score", required=True)
    mq.add_argument("--calc-raw-dir", required=True)
    mq.add_argument("--out-dir", required=True)

    pr = sub.add_parser("promote")
    pr.add_argument("--profile", required=True)
    pr.add_argument("--menu", default="", help="optional slim menu to verify lunch content")
    pr.add_argument("--from-reservation", required=True)
    pr.add_argument("--to-reservation", default="")
    pr.add_argument("--from-cache-root", required=True)
    pr.add_argument("--to-cache-root", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "ingest-menu":
        if args.expires_at == "":
            args.expires_at = None
        return cmd_ingest_menu(args)
    if args.cmd == "ingest-details":
        return cmd_ingest_details(args)
    if args.cmd == "check-menu":
        return cmd_check_menu(args)
    if args.cmd == "build-score-calc":
        return cmd_build_score_calc(args)
    if args.cmd == "merge-quotes":
        return cmd_merge_quotes(args)
    if args.cmd == "promote":
        return cmd_promote(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())