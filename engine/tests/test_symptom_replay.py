"""Replay the 2026-11-10 symptoms with desensitized fixtures (no live MCP).

The fixtures are synthetic: a generic store, generic product codes from the
slim snapshot's structure, and minimal history. No address, order id, or
personal history is stored here.
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
sys.path.insert(0, str(ENGINE))

import combo_edit  # noqa: E402
import exposure_log  # noqa: E402
import quote_state  # noqa: E402
import score_lunch  # noqa: E402
import stage_timer  # noqa: E402

CST = timezone(timedelta(hours=8))

# 2026-11-10 lunch: rank1 combo shown but not accepted. Same combo had been
# offered the day before (transcript-only), so deal history alone cannot see it.
ROTATION_KEY = "db6671_515295_4810_3060"


def today_score():
    return {
        "generatedAt": "2026-11-10T12:16:03+08:00",
        "storeCode": "000000",
        "mealPeriod": "lunch",
        "dining": "eat-in",
        "rotationKey": ROTATION_KEY,
        "humanSummary": "示例三件套（甲+乙+丙去冰）≈¥23.9",
        "priceFen": 2390,
        "priceVerified": False,
        "needsQuoteBeforeConfirm": False,
        "score": 50.19,
        "rank": 1,
        "unseenMain": True,
        "unseenSide": True,
        "unseenDrink": True,
    }


def combo_items(side="4810", drink="3060"):
    return [
        {
            "productCode": "9900016671",
            "productName": "示例三件套",
            "quantity": 1,
            "roundList": [
                {"round": "1", "comboItemList": [{"code": "515295", "quantity": 1}]},
                {"round": "2", "comboItemList": [{"code": side, "quantity": 1}]},
                {
                    "round": "3",
                    "comboItemList": [
                        {
                            "code": drink,
                            "quantity": 1,
                            "modification": {"values": [{"code": "200002", "key": "0-0", "quantity": 1}]},
                        }
                    ],
                },
            ],
        }
    ]


class TodaysScoreRegressionTest(unittest.TestCase):
    def test_reported_snapshot_is_not_a_confirmable_price(self):
        score = today_score()
        # The fixture itself records the defect: unverified but not flagged.
        self.assertFalse(score["priceVerified"])
        self.assertFalse(score["needsQuoteBeforeConfirm"])

        cand = score_lunch.ScoredCandidate(
            rotation_key=score["rotationKey"],
            human_summary=score["humanSummary"],
            price_fen=score["priceFen"],
            items=combo_items(),
            tags=["dabao"],
        )
        req = {"budgetFen": 3500, "dining": "eat-in", "storeCode": score["storeCode"]}
        self.assertIsNone(score_lunch.hard_filter(cand, req))
        # After the fix, an estimate can never look confirmable.
        self.assertTrue(cand.meta["needsQuoteBeforeConfirm"])
        cand.score = score["score"]
        resp = score_lunch.build_response(cand, [], [cand], req, [], False, "rank_only")
        self.assertFalse(resp["priceVerified"])
        self.assertTrue(resp["needsQuoteBeforeConfirm"])
        self.assertEqual(resp["priceState"], quote_state.ESTIMATE)
        self.assertIn("估价", resp["humanSummary"])

    def test_prior_exposure_breaks_the_unseen_claim(self):
        c = score_lunch.ScoredCandidate(
            rotation_key=ROTATION_KEY,
            human_summary="示例三件套（甲+乙+丙去冰）≈¥23.9",
            price_fen=2390,
            items=combo_items(),
            score=50.19,
            tags=["dabao"],
        )
        events = [
            {
                "kind": "exposure",
                "rotationKey": ROTATION_KEY,
                "at": "2026-11-09T12:21:00+08:00",
                "date": "2026-11-09",
                "storeCode": "000000",
                "dining": "eat-in",
                "mealPeriod": "lunch",
            }
        ]
        req = {
            "storeCode": "000000",
            "dining": "eat-in",
            "mealPeriod": "lunch",
            "exposureEvents": events,
        }
        score_lunch.apply_exposure_rotation([c], req)
        self.assertTrue(c.meta["priorExposure"])
        self.assertLess(c.score, 50.19)
        # Exposure is not a deal: no history is invented.
        self.assertNotIn("deal", json.dumps(c.meta))
        self.assertGreaterEqual(c.meta["exposurePenalty"], 0.5)

    def test_skip_does_not_become_a_deal_or_a_dislike(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "exposure.json")
            exposure_log.record(
                path,
                {
                    "kind": "skip",
                    "rotationKey": ROTATION_KEY,
                    "at": "2026-11-10T12:23:00+08:00",
                    "date": "2026-11-10",
                    "storeCode": "000000",
                    "mealPeriod": "lunch",
                },
            )
            doc = exposure_log.load_log(path)
            self.assertEqual([row["kind"] for row in doc["events"]], ["skip"])
            summary = exposure_log.summarize(
                doc["events"],
                scope={"storeCode": "000000"},
                now=datetime(2026, 11, 10, 13, 0, 0, tzinfo=CST),
            )
            row = summary[ROTATION_KEY]
            self.assertEqual(row["deal"], 0)
            self.assertTrue(row["declined"])
            factor, _reason = exposure_log.rotation_penalty(row)
            self.assertGreaterEqual(factor, 0.5)  # one skip is a nudge, not a ban


class PendingCalcParityTest(unittest.TestCase):
    """11/10 pending and calc agreed on items but not on price wording."""

    def calc(self):
        quoted = datetime(2026, 11, 10, 12, 19, 16, tzinfo=CST)
        return {
            "ok": True,
            "rotationKey": ROTATION_KEY,
            "humanSummary": "示例三件套（甲+乙+丙去冰）",
            "priceFen": 2390,
            "payableFen": 2390,
            "priceVerified": True,
            "quotedAt": quoted.isoformat(),
            "items": combo_items(),
            "storeCode": "000000",
            "dining": "eat-in",
        }

    def test_one_renderer_for_pending_and_calc(self):
        pending = {
            "ok": True,
            "rotationKey": ROTATION_KEY,
            "humanSummary": "示例三件套（甲+乙+丙去冰），实付¥23.9",
            "priceFen": 2390,
            "payableFen": 2390,
            "priceVerified": True,
            "priceState": "quoted",
            "priceLabel": "已核价",
            "items": combo_items(),
            "storeCode": "000000",
            "dining": "eat-in",
        }
        result = combo_edit.sync_snapshots(
            pending, self.calc(), now=datetime(2026, 11, 10, 12, 40, 0, tzinfo=CST)
        )
        self.assertTrue(result["ok"])
        merged = result["pending"]
        expected = quote_state.compose_summary(
            quote_state.strip_price_suffix(pending["humanSummary"]),
            quote_state.PriceStatus(
                state=quote_state.QUOTED,
                price_fen=2390,
                payable_fen=2390,
                verified=True,
                needs_quote=False,
                label="已核价",
                reason="ok",
            ),
        )
        self.assertEqual(merged["humanSummary"], expected)
        self.assertNotIn("≈", merged["humanSummary"])

    def test_named_combo_swap_keeps_rounds_and_price_state_in_step(self):
        detail = {
            "name": "示例三件套",
            "rounds": [
                {"id": "1", "name": "主食", "choices": [{"code": "515295", "name": "主堡", "diffPrice": "+ ¥0"}]},
                {
                    "id": "2",
                    "name": "小食",
                    "choices": [
                        {"code": "4810", "name": "中薯条", "diffPrice": "+ ¥0", "isDefault": 1},
                        {"code": "4437", "name": "玉米杯", "diffPrice": "+ ¥0"},
                    ],
                },
                {
                    "id": "3",
                    "name": "饮料",
                    "choices": [
                        {"code": "3060", "name": "雪碧中杯", "diffPrice": "+ ¥0", "isDefault": 1},
                        {"code": "3071", "name": "无糖可乐中杯", "diffPrice": "+ ¥0"},
                    ],
                },
            ],
        }
        plan = combo_edit.plan_named_combo(
            detail, prefer_by_round={"3": ["3071"]}, recent_by_round={"2": ["4810"]}
        )
        rounds = {row["round"]: row["chosen"]["code"] for row in plan["decisions"]}
        self.assertEqual(rounds["2"], "4437")
        self.assertEqual(rounds["3"], "3071")
        codes = [
            ci["code"]
            for rnd in plan["roundList"]
            for ci in rnd["comboItemList"]
        ]
        self.assertEqual(codes, ["515295", "4437", "3071"])


class StageTimerTest(unittest.TestCase):
    def test_duration_only_when_start_and_end_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "timing.jsonl")
            timer = stage_timer.StageTimer(path, run_label="test")
            timer.mark_dependency("mcp-wrapper", present=False, detail="missing at run start")
            with timer.stage("query-meals"):
                pass
            with timer.stage("calculate-price"):
                pass
            summary = timer.finish()
            stages = {row["stage"]: row for row in summary["stages"]}
            self.assertIn("query-meals", stages)
            self.assertIn("calculate-price", stages)
            self.assertIsNotNone(stages["query-meals"]["durationSeconds"])
            deps = {row["name"]: row for row in summary["dependencies"]}
            self.assertFalse(deps["mcp-wrapper"]["present"])
            # No stage covers the time between run start and the first stage.
            self.assertTrue(all("reason" in gap for gap in summary["unattributed"]))

    def test_missing_end_is_reported_not_guessed(self):
        events = [
            {"event": "stage_start", "stage": "query-meals", "startedAt": "2026-11-10T12:08:05+08:00"},
            {"event": "stage_end", "stage": "calculate-price", "endedAt": "2026-11-10T12:19:16+08:00", "durationSeconds": 1.0},
        ]
        summary = stage_timer.summarize_events(events)
        self.assertEqual([row["stage"] for row in summary["missingEnd"]], ["query-meals"])
        self.assertEqual(summary["measuredStageSeconds"], 1.0)
        self.assertTrue(summary["unattributed"])
        self.assertIn("do not attribute", summary["note"])

    def test_error_stage_is_timed_and_reraised(self):
        with tempfile.TemporaryDirectory() as tmp:
            timer = stage_timer.StageTimer(str(Path(tmp) / "t.jsonl"))
            with self.assertRaises(RuntimeError):
                with timer.stage("calculate-price"):
                    raise RuntimeError("boom")
            summary = stage_timer.summarize_events(timer.events)
            self.assertEqual(summary["stages"][0]["status"], "error:RuntimeError")

    def test_no_wall_clock_speedup_claim(self):
        events = [
            {"event": "stage_start", "stage": "a", "startedAt": "2026-11-10T12:00:00+08:00"},
            {"event": "stage_end", "stage": "a", "endedAt": "2026-11-10T12:00:10+08:00", "durationSeconds": 10.0},
            {"event": "stage_start", "stage": "b", "startedAt": "2026-11-10T12:05:00+08:00"},
            {"event": "stage_end", "stage": "b", "endedAt": "2026-11-10T12:05:20+08:00", "durationSeconds": 20.0},
        ]
        summary = stage_timer.summarize_events(events)
        gap = [row for row in summary["unattributed"] if row["durationSeconds"] >= 200]
        self.assertEqual(len(gap), 1)
        self.assertEqual(gap[0]["from"], "2026-11-10T12:00:10+08:00")
        self.assertEqual(gap[0]["to"], "2026-11-10T12:05:00+08:00")


if __name__ == "__main__":
    unittest.main()
