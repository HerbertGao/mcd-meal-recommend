import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
EXAMPLES = ENGINE / "examples"
sys.path.insert(0, str(ENGINE))

import mcp_cache  # noqa: E402
import recommend_run  # noqa: E402
import score_lunch  # noqa: E402


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class StableFingerprintTest(unittest.TestCase):
    def test_volatile_fields_do_not_change_hash(self):
        base = load(EXAMPLES / "fixtures" / "menu.json")
        a = deepcopy(base)
        b = deepcopy(base)
        b["fetchedAt"] = "2026-11-27T11:00:00+08:00"
        b["traceId"] = "abc"
        b["apiDatetime"] = "2026-11-27T11:00:01+08:00"
        self.assertEqual(mcp_cache.stable_menu_fingerprint(a), mcp_cache.stable_menu_fingerprint(b))

    def test_content_change_changes_hash(self):
        base = load(EXAMPLES / "fixtures" / "menu.json")
        other = deepcopy(base)
        other["categories"][0]["meals"][0]["currentPrice"] = "99.00"
        self.assertNotEqual(mcp_cache.stable_menu_fingerprint(base), mcp_cache.stable_menu_fingerprint(other))


class PresentCountTest(unittest.TestCase):
    def test_presented_not_same_as_ranked_prefix(self):
        req = load(EXAMPLES / "request-offline.json")
        sel = dict(req.get("selection") or {})
        sel["presentCount"] = 3
        sel["mode"] = "rank_only"
        sel["diversifyByFamily"] = True
        sel["diversifyByMain"] = True
        req["selection"] = sel
        cands, _ = score_lunch.candidates_from_request(req)
        passed = []
        for c in cands:
            if score_lunch.hard_filter(c, req):
                continue
            score_lunch.score_candidate(c, req)
            passed.append(c)
        ranked = sorted(passed, key=lambda c: (-c.score, c.price_fen, c.rotation_key))
        presented = score_lunch.select_presented(ranked, req, seed=7)
        self.assertGreaterEqual(len(presented), 2)
        self.assertEqual(presented[0].rotation_key, ranked[0].rotation_key)
        keys = [c.rotation_key for c in presented]
        self.assertEqual(len(keys), len(set(keys)))
        if len(ranked) >= 3:
            self.assertNotEqual(keys, [c.rotation_key for c in ranked[:3]])


class RecommendRunCliTest(unittest.TestCase):
    def test_ingest_menu_and_check_hit(self):
        menu = load(EXAMPLES / "fixtures" / "menu.json")
        wrapped = {"success": True, "data": menu, "datetime": "2026-11-27T12:00:00+08:00", "traceId": "t1"}
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "menu-raw.json"
            raw.write_text(json.dumps(wrapped), encoding="utf-8")
            cache_root = Path(tmp) / "cache"
            out_dir = Path(tmp) / "prewarm"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "recommend_run.py"),
                    "ingest-menu",
                    "--profile",
                    "lunch",
                    "--raw",
                    str(raw),
                    "--cache-root",
                    str(cache_root),
                    "--out-dir",
                    str(out_dir),
                ],
                capture_output=True,
                text=True,
                cwd=str(ENGINE),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            manifest = load(out_dir / "manifest.json")
            fp = manifest["menuFingerprint"]
            needed = load(out_dir / "needed-details.json").get("productCodes") or []
            fixture_details = EXAMPLES / "fixtures" / "meal-details"
            for code in needed:
                src = fixture_details / f"{code}.json"
                if src.is_file():
                    (out_dir / f"{code}.json").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            noon = deepcopy(wrapped)
            noon["datetime"] = "2026-11-27T12:05:00+08:00"
            noon["traceId"] = "t2"
            noon_path = Path(tmp) / "noon.json"
            noon_path.write_text(json.dumps(noon), encoding="utf-8")
            chk = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "recommend_run.py"),
                    "check-menu",
                    "--profile",
                    "lunch",
                    "--raw",
                    str(noon_path),
                    "--prewarm-dir",
                    str(out_dir),
                ],
                capture_output=True,
                text=True,
                cwd=str(ENGINE),
            )
            self.assertEqual(chk.returncode, 0, chk.stdout + chk.stderr)
            body = json.loads(chk.stdout.strip())
            self.assertEqual(body["status"], "hit")
            self.assertEqual(body["menuFingerprint"], fp)


class BugbotFixesTest(unittest.TestCase):
    def test_parse_run_seed_accepts_iso_date(self):
        self.assertEqual(recommend_run.parse_run_seed("2026-11-27"), 20261127)
        self.assertEqual(recommend_run.parse_run_seed("20261127"), 20261127)

    def test_load_details_dir_ignores_menu_slim(self):
        menu = load(EXAMPLES / "fixtures" / "menu.json")
        detail = load(EXAMPLES / "fixtures" / "meal-details" / "9900016655.json")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "menu-slim.json").write_text(json.dumps(menu), encoding="utf-8")
            (d / "manifest.json").write_text("{}", encoding="utf-8")
            (d / "9900016655.json").write_text(json.dumps(detail), encoding="utf-8")
            out = recommend_run._load_details_dir(d)
            self.assertIn("9900016655", out)
            self.assertEqual(len(out), 1)

    def test_merge_quotes_uses_envelope_datetime(self):
        items = [
            {
                "productCode": "9900016655",
                "quantity": 1,
                "roundList": [
                    {"round": "1", "comboItemList": [{"code": "1010", "quantity": 1}]},
                    {
                        "round": "2",
                        "comboItemList": [
                            {
                                "code": "522021",
                                "quantity": 1,
                                "modification": {"values": [{"code": "200002", "key": "0-0", "quantity": 1}]},
                            }
                        ],
                    },
                ],
            }
        ]
        row = {
            "presentLabel": "A",
            "rotationKey": "rk_merge_test",
            "humanSummary": "测试套餐",
            "items": items,
            "storeCode": "400001",
            "dining": "eat-in",
            "mealPeriod": "lunch",
            "priceFen": 2390,
            "takeWayCodeHint": "eat-in",
        }
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            score_path = base / "score.json"
            score_path.write_text(json.dumps({"presented": [row]}), encoding="utf-8")
            calc_dir = base / "calc-raw"
            calc_dir.mkdir()
            cst = timezone(timedelta(hours=8))
            quoted_at = datetime.now(cst).replace(microsecond=0).isoformat()
            calc_dir.joinpath("A.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "datetime": quoted_at,
                        "data": {"price": 2390, "productPrice": 2390, "items": items},
                    }
                ),
                encoding="utf-8",
            )
            out_dir = base / "out"
            args = argparse.Namespace(
                profile="lunch",
                score=str(score_path),
                calc_raw_dir=str(calc_dir),
                out_dir=str(out_dir),
            )
            self.assertEqual(recommend_run.cmd_merge_quotes(args), 0)
            merged = load(out_dir / "merged-score.json")
            primary = merged["presented"][0]
            self.assertTrue(primary.get("priceVerified"))
            self.assertEqual(primary.get("payableFen"), 2390)

    def test_ingest_menu_reservation_override_for_promote(self):
        menu = load(EXAMPLES / "fixtures" / "menu.json")
        wrapped = {"success": True, "data": menu, "datetime": "2026-11-27T11:00:00+08:00"}
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.json"
            raw.write_text(json.dumps(wrapped), encoding="utf-8")
            cache_root = Path(tmp) / "cache"
            out_dir = Path(tmp) / "prewarm"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "recommend_run.py"),
                    "ingest-menu",
                    "--profile",
                    "lunch",
                    "--raw",
                    str(raw),
                    "--cache-root",
                    str(cache_root),
                    "--out-dir",
                    str(out_dir),
                    "--reservation-date",
                    "2026-11-27 12:00",
                ],
                capture_output=True,
                text=True,
                cwd=str(ENGINE),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            promote = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "recommend_run.py"),
                    "promote",
                    "--profile",
                    "lunch",
                    "--menu",
                    str(out_dir / "menu-slim.json"),
                    "--from-reservation",
                    "2026-11-27 12:00",
                    "--to-reservation",
                    "",
                    "--from-cache-root",
                    str(cache_root),
                    "--to-cache-root",
                    str(Path(tmp) / "noon-cache"),
                ],
                capture_output=True,
                text=True,
                cwd=str(ENGINE),
            )
            self.assertEqual(promote.returncode, 0, promote.stderr + promote.stdout)


class PromoteKeysTest(unittest.TestCase):
    def test_promote_copies_reservation_dimension(self):
        menu = load(EXAMPLES / "fixtures" / "menu.json")
        detail = load(EXAMPLES / "fixtures" / "meal-details" / "9900016655.json")
        with tempfile.TemporaryDirectory() as tmp:
            prof_path = Path(tmp) / "lunch-test.json"
            prof = deepcopy(recommend_run.load_profile_request("lunch"))
            prof["storeCode"] = "400001"
            prof_path.write_text(json.dumps(prof), encoding="utf-8")
            scope = recommend_run.cache_scope_from_request(prof)
            fp = mcp_cache.stable_menu_fingerprint(menu)
            src_root = Path(tmp) / "src"
            dst_root = Path(tmp) / "dst"
            src = mcp_cache.SnapshotCache(src_root)
            src.put(
                mcp_cache.CacheKey(
                    kind=mcp_cache.KIND_MENU,
                    store_code=scope["storeCode"],
                    dining=scope["dining"],
                    meal_period=scope["mealPeriod"],
                    reservation_date="2026-11-27 12:00",
                ),
                menu,
            )
            src.put(
                mcp_cache.CacheKey(
                    kind=mcp_cache.KIND_DETAIL,
                    store_code=scope["storeCode"],
                    dining=scope["dining"],
                    meal_period=scope["mealPeriod"],
                    reservation_date="2026-11-27 12:00",
                    product_code="9900016655",
                    menu_fingerprint=fp,
                ),
                detail,
            )
            menu_path = Path(tmp) / "slim.json"
            menu_path.write_text(json.dumps(menu), encoding="utf-8")
            code = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "recommend_run.py"),
                    "promote",
                    "--profile",
                    str(prof_path),
                    "--menu",
                    str(menu_path),
                    "--from-reservation",
                    "2026-11-27 12:00",
                    "--to-reservation",
                    "",
                    "--from-cache-root",
                    str(src_root),
                    "--to-cache-root",
                    str(dst_root),
                ],
                capture_output=True,
                text=True,
                cwd=str(ENGINE),
            )
            self.assertEqual(code.returncode, 0, code.stderr + code.stdout)
            dst = mcp_cache.SnapshotCache(dst_root)
            hit = dst.get(
                mcp_cache.CacheKey(
                    kind=mcp_cache.KIND_MENU,
                    store_code="400001",
                    dining=scope["dining"],
                    meal_period=scope["mealPeriod"],
                    reservation_date="",
                )
            )
            self.assertTrue(hit.ok)
            detail_hit = dst.get(
                mcp_cache.CacheKey(
                    kind=mcp_cache.KIND_DETAIL,
                    store_code="400001",
                    dining=scope["dining"],
                    meal_period=scope["mealPeriod"],
                    reservation_date="",
                    product_code="9900016655",
                    menu_fingerprint=fp,
                )
            )
            self.assertTrue(detail_hit.ok)


class StageTimerCliTest(unittest.TestCase):
    def test_run_start_and_stage_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "lunch.jsonl"
            wrapper = Path(tmp) / "wrapper.sh"
            wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
            subprocess.run(
                [sys.executable, str(ENGINE / "stage_timer.py"), "run_start", "--log", str(log), "--label", "lunch", "--wrapper", str(wrapper)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [sys.executable, str(ENGINE / "stage_timer.py"), "stage-start", "--log", str(log), "query-meals"],
                check=True,
            )
            subprocess.run(
                [sys.executable, str(ENGINE / "stage_timer.py"), "stage-end", "--log", str(log), "query-meals"],
                check=True,
            )
            fin = subprocess.run(
                [sys.executable, str(ENGINE / "stage_timer.py"), "finish", "--log", str(log)],
                check=True,
                capture_output=True,
                text=True,
            )
            summary = json.loads(fin.stdout)
            self.assertIn("stages", summary)


if __name__ == "__main__":
    unittest.main()