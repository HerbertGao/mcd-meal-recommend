"""Price state, cache isolation, exposure tracks, named-combo ordering, snapshot sync."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
EXAMPLES = ENGINE / "examples"
sys.path.insert(0, str(ENGINE))

import combo_edit  # noqa: E402
import exposure_log  # noqa: E402
import mcp_cache  # noqa: E402
import quote_state  # noqa: E402
import score_lunch  # noqa: E402
from budget import apply_quote, budget_check, payable_fen  # noqa: E402

CST = timezone(timedelta(hours=8))


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def items(product_code="9900016671", main="515295", side="4810", drink="3060"):
    return [
        {
            "productCode": product_code,
            "quantity": 1,
            "roundList": [
                {"round": "1", "comboItemList": [{"code": main, "quantity": 1}]},
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


def cand(**kwargs):
    base = {
        "rotation_key": "db6671_515295_4810_3060",
        "human_summary": "套餐（双层脆鸡堡+中薯条+雪碧中杯去冰）≈¥23.9",
        "price_fen": 2390,
        "items": items(),
    }
    base.update(kwargs)
    return score_lunch.ScoredCandidate(**base)


class PriceStateTest(unittest.TestCase):
    def test_estimate_requires_quote_even_for_eat_in(self):
        # Reported symptom: priceVerified=false while needsQuoteBeforeConfirm=false.
        c = cand()
        req = {"budgetFen": 3500, "dining": "eat-in"}
        self.assertIsNone(score_lunch.hard_filter(c, req))
        self.assertTrue(c.meta["needsQuoteBeforeConfirm"])
        self.assertEqual(c.meta["priceState"], quote_state.ESTIMATE)
        resp = score_lunch.build_response(c, [], [c], req, [], False, "rank_only")
        self.assertFalse(resp["priceVerified"])
        self.assertTrue(resp["needsQuoteBeforeConfirm"])
        self.assertIn("估价", resp["humanSummary"])
        self.assertNotIn("实付", resp["humanSummary"])

    def test_fresh_quote_confirms_and_renders_paid(self):
        c = cand()
        now = datetime(2026, 11, 10, 12, 19, 16, tzinfo=CST)
        quote = {"price": 2390, "productPrice": 2390, "quotedAt": now.isoformat(), "items": items()}
        apply_quote(c, quote, req={"dining": "eat-in"}, now=now)
        req = {"budgetFen": 3500, "dining": "eat-in"}
        self.assertIsNone(score_lunch.hard_filter(c, req))
        c.score = 50.19
        resp = score_lunch.build_response(c, [], [c], req, [], False, "rank_only")
        self.assertTrue(resp["priceVerified"])
        self.assertFalse(resp["needsQuoteBeforeConfirm"])
        self.assertEqual(resp["priceState"], quote_state.QUOTED)
        self.assertIn("实付 ¥23.9", resp["humanSummary"])
        self.assertNotIn("≈", resp["humanSummary"])

    def test_expired_quote_drops_back_to_estimate(self):
        c = cand()
        quoted = datetime(2026, 11, 10, 9, 0, 0, tzinfo=CST)
        quote = {"price": 2390, "productPrice": 2390, "quotedAt": quoted.isoformat(), "items": items()}
        now = quoted + timedelta(minutes=31)
        apply_quote(c, quote, req={"dining": "eat-in", "quoteTtlMinutes": 30}, now=now)
        self.assertFalse(c.price_verified)
        self.assertIsNone(c.payable_fen)
        self.assertTrue(c.meta.get("quoteStale"))
        req = {"budgetFen": 3500, "dining": "eat-in", "quoteTtlMinutes": 30}
        self.assertTrue(c.meta.get("quoteRejected") is None or c.meta.get("quoteStale"))
        c.score = 50.0
        resp = score_lunch.build_response(c, [], [c], req, [], False, "rank_only")
        self.assertFalse(resp["priceVerified"])
        self.assertTrue(resp["needsQuoteBeforeConfirm"])
        self.assertEqual(resp["priceState"], quote_state.STALE)
        self.assertIn("重新核价", resp["humanSummary"])
        self.assertNotIn("实付", resp["humanSummary"])

    def test_quote_bound_to_different_items_is_rejected(self):
        c = cand()
        now = datetime(2026, 11, 10, 12, 19, 16, tzinfo=CST)
        other = items(side="4800")
        quote = {"price": 2390, "productPrice": 2390, "quotedAt": now.isoformat(), "items": other}
        apply_quote(c, quote, req={"dining": "eat-in"}, now=now)
        self.assertFalse(c.price_verified)
        self.assertEqual(c.meta["quoteRejected"], "items_mismatch")
        self.assertIsNone(payable_fen(c, {"dining": "eat-in"}) if c.meta.get("quoteRejected") else None)

    def test_quote_for_another_store_is_rejected(self):
        c = cand()
        now = datetime(2026, 11, 10, 12, 19, 16, tzinfo=CST)
        quote = {
            "price": 2390,
            "productPrice": 2390,
            "quotedAt": now.isoformat(),
            "storeCode": "999999",
            "items": items(),
        }
        apply_quote(c, quote, req={"dining": "eat-in", "storeCode": "400001"}, now=now)
        self.assertFalse(c.price_verified)
        self.assertTrue(str(c.meta.get("quoteRejected") or "").startswith("context_mismatch"))

    def test_budget_uses_payable_only_when_verified(self):
        c = cand(price_fen=1690)
        c.items = items(product_code="9900016670", side="4810")
        req = {"budgetFen": 2000, "dining": "delivery"}
        c.payable_fen = 2290
        c.price_verified = False
        # An unverified stale payable must not drive the budget decision.
        self.assertEqual(budget_check(c, req), "unverified_delivery_total")
        now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=CST)
        apply_quote(c, {"price": 2290, "productPrice": 1690, "quotedAt": now.isoformat(), "items": c.items}, req=req, now=now)
        self.assertEqual(budget_check(c, req), "over_budget_payable:2290>2000")


class CacheIsolationTest(unittest.TestCase):
    def key(self, **kwargs):
        base = dict(
            kind=mcp_cache.KIND_DETAIL,
            store_code="400001",
            dining="eat-in",
            meal_period="lunch",
            product_code="9900016671",
            menu_fingerprint="m1",
        )
        base.update(kwargs)
        return mcp_cache.CacheKey(**base)

    def test_hit_requires_every_dimension(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = mcp_cache.SnapshotCache(tmp)
            key = self.key()
            cache.put(key, {"ok": True}, now=datetime(2026, 11, 10, 12, 0, 0, tzinfo=CST))
            now = datetime(2026, 11, 10, 12, 5, 0, tzinfo=CST)
            self.assertTrue(cache.get(key, now=now).ok)
            for changed in (
                {"kind": mcp_cache.KIND_MENU},
                {"store_code": "1950895"},
                {"dining": "delivery"},
                {"meal_period": "breakfast"},
                {"reservation_date": "2026-11-11 07:00"},
                {"be_code": "BE2"},
                {"address_id": "addr-1"},
                {"product_code": "9900016651"},
            ):
                other = self.key(**changed)
                result = cache.get(other, now=now)
                self.assertFalse(result.ok, msg=f"isolation leak for {changed}")
                self.assertIn(result.status, {"missing", "context_changed"})

    def test_new_menu_fingerprint_invalidates_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = mcp_cache.SnapshotCache(tmp)
            now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=CST)
            cache.put(self.key(), {"ok": True}, now=now)
            stale = cache.get(self.key(menu_fingerprint="m2"), now=now)
            self.assertEqual(stale.status, "context_changed")
            self.assertEqual(stale.reason, "menu_fingerprint")

    def test_expired_entry_is_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = mcp_cache.SnapshotCache(tmp)
            start = datetime(2026, 11, 10, 12, 0, 0, tzinfo=CST)
            cache.put(self.key(), {"ok": True}, now=start, ttl_seconds=60)
            self.assertEqual(cache.get(self.key(), now=start + timedelta(seconds=61)).status, "expired")

    def test_failure_tombstone_blocks_reuse_until_next_put(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = mcp_cache.SnapshotCache(tmp)
            now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=CST)
            key = self.key()
            cache.put(key, {"ok": True}, now=now)
            cache.fail(key, "mcp timeout", now=now)
            blocked = cache.get(key, now=now)
            self.assertEqual(blocked.status, "invalidated")
            self.assertEqual(blocked.reason, "previous_error")
            cache.put(key, {"ok": True}, now=now)
            self.assertTrue(cache.get(key, now=now).ok)

    def test_validator_rejects_unusable_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = mcp_cache.SnapshotCache(tmp)
            now = datetime(2026, 11, 10, 12, 0, 0, tzinfo=CST)
            cache.put(self.key(), {"rounds": []}, now=now)
            result = cache.get(self.key(), now=now, validator=lambda payload: bool(payload.get("rounds")))
            self.assertEqual(result.status, "error")

    def test_missing_store_code_is_rejected(self):
        with self.assertRaises(ValueError):
            mcp_cache.CacheKey(kind=mcp_cache.KIND_MENU, store_code="")
        with self.assertRaises(ValueError):
            mcp_cache.CacheKey(kind=mcp_cache.KIND_DETAIL, store_code="1")

    def test_cached_price_is_never_marked_verified(self):
        # A cache entry only ever holds a list-price snapshot; scoring it still
        # produces an estimate that needs a live quote.
        c = cand()
        req = {"budgetFen": 3500, "dining": "eat-in"}
        score_lunch.hard_filter(c, req)
        self.assertFalse(c.price_verified)
        self.assertTrue(c.meta["needsQuoteBeforeConfirm"])


class ExposureTrackTest(unittest.TestCase):
    def event(self, kind, rk="db6671_515295_4810_3060", day="2026-11-09", seq=0):
        return {
            "kind": kind,
            "rotationKey": rk,
            "at": f"{day}T12:00:00+08:00",
            "date": day,
            "seq": seq,
            "storeCode": "400001",
            "dining": "eat-in",
            "mealPeriod": "lunch",
        }

    def test_tracks_stay_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.json")
            exposure_log.record(path, self.event("exposure"))
            exposure_log.record(path, self.event("skip"))
            doc = exposure_log.load_log(path)
            kinds = [row["kind"] for row in doc["events"]]
            self.assertEqual(kinds, ["exposure", "skip"])
            summary = exposure_log.summarize(
                doc["events"],
                scope={"storeCode": "400001"},
                now=datetime(2026, 11, 10, 12, 0, 0, tzinfo=CST),
            )
            row = summary["db6671_515295_4810_3060"]
            self.assertEqual(row["exposure"], 1)
            self.assertEqual(row["skip"], 1)
            self.assertEqual(row["deal"], 0)
            self.assertEqual(row["shownNotAccepted"], 1)

    def test_skip_is_not_recorded_as_deal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.json")
            exposure_log.record(path, self.event("skip"))
            doc = exposure_log.load_log(path)
            self.assertNotIn("deal", [row["kind"] for row in doc["events"]])

    def test_recording_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "events.json")
            first = exposure_log.record(path, self.event("exposure"))
            second = exposure_log.record(path, self.event("exposure"))
            self.assertEqual(first.status, "recorded")
            self.assertEqual(second.status, "duplicate")
            self.assertEqual(len(exposure_log.load_log(path)["events"]), 1)
            # A genuinely later exposure (different seq) is a new fact.
            third = exposure_log.record(path, self.event("exposure", day="2026-11-10", seq=1))
            self.assertEqual(third.status, "recorded")
            self.assertEqual(len(exposure_log.load_log(path)["events"]), 2)

    def test_penalty_is_bounded_and_explained(self):
        factor, reason = exposure_log.rotation_penalty({"exposure": 9, "skip": 9, "deal": 0})
        self.assertGreaterEqual(factor, 0.5)
        self.assertLess(factor, 1.0)
        self.assertIn("exposed", reason)
        accepted, reason_ok = exposure_log.rotation_penalty({"exposure": 3, "skip": 0, "deal": 1})
        self.assertEqual(accepted, 1.0)
        self.assertEqual(reason_ok, "accepted_before")

    def test_exposure_rotation_does_not_touch_deal_facts(self):
        # Rotation may reorder, but the candidate keeps its price/items and no
        # history entry is invented.
        c = cand(score=50.19, tags=["dabao"])
        req = {
            "storeCode": "400001",
            "dining": "eat-in",
            "mealPeriod": "lunch",
            "exposureEvents": [self.event("exposure"), self.event("skip")],
        }
        before_items = json.dumps(c.items, sort_keys=True)
        score_lunch.apply_exposure_rotation([c], req)
        self.assertTrue(c.meta["priorExposure"])
        self.assertEqual(c.meta["exposureCount"], 1)
        self.assertEqual(c.meta["skipCount"], 1)
        self.assertLess(c.score, 50.19)
        self.assertEqual(json.dumps(c.items, sort_keys=True), before_items)
        self.assertNotIn("deal", c.meta)

    def test_no_log_means_no_inference(self):
        c = cand(score=50.19)
        score_lunch.apply_exposure_rotation([c], {"dining": "eat-in"})
        self.assertFalse(c.meta.get("priorExposure"))
        self.assertEqual(c.score, 50.19)

    def test_other_store_scope_is_ignored(self):
        c = cand(score=50.19)
        req = {
            "storeCode": "1000000",
            "dining": "eat-in",
            "mealPeriod": "lunch",
            "exposureEvents": [self.event("exposure")],
        }
        score_lunch.apply_exposure_rotation([c], req)
        self.assertEqual(c.score, 50.19)


class NamedComboOrderingTest(unittest.TestCase):
    def detail(self):
        return {
            "name": "示例四件套",
            "rounds": [
                {
                    "id": "1",
                    "name": "选择主食",
                    "choices": [
                        {"code": "1100", "name": "巨无霸", "diffPrice": "+ ¥0", "isDefault": 1}
                    ],
                },
                {
                    "id": "2",
                    "name": "选择小食",
                    "choices": [
                        {"code": "4810", "name": "中薯条", "diffPrice": "+ ¥0", "isDefault": 1},
                        {"code": "4437", "name": "玉米杯", "diffPrice": "+ ¥0"},
                    ],
                },
                {
                    "id": "3",
                    "name": "选择饮料",
                    "choices": [
                        {"code": "3050", "name": "可乐中杯", "diffPrice": "+ ¥0", "isDefault": 1},
                        {"code": "522021", "name": "无糖可乐大杯", "diffPrice": "+ ¥2.00"},
                    ],
                },
            ],
        }

    def test_recent_side_is_not_the_default_pick(self):
        plan = combo_edit.plan_named_combo(
            self.detail(), recent_by_round={"2": ["4810"]}
        )
        round2 = next(row for row in plan["decisions"] if row["round"] == "2")
        self.assertEqual(round2["chosen"]["code"], "4437")
        self.assertIn("recent_on_track", next(
            item["reasons"] for item in round2["order"] if item["code"] == "4810"
        ))

    def test_explicit_preference_beats_recency(self):
        plan = combo_edit.plan_named_combo(
            self.detail(),
            prefer_by_round={"3": ["522021"]},
            recent_by_round={"3": ["522021"]},
        )
        round3 = next(row for row in plan["decisions"] if row["round"] == "3")
        self.assertEqual(round3["chosen"]["code"], "522021")
        self.assertTrue(round3["chosen"]["preferred"])
        self.assertGreater(round3["chosen"]["diffFen"], 0)

    def test_zero_price_option_does_not_win_by_being_free(self):
        no_pref = combo_edit.order_choices(
            [
                {"code": "A", "name": "零加价", "diffFen": 0},
                {"code": "B", "name": "加价", "diffFen": 200},
            ]
        )
        self.assertEqual(no_pref[0].code, "A")  # cheaper only breaks a tie
        with_bonus = combo_edit.order_choices(
            [
                {"code": "A", "name": "零加价", "diffFen": 0},
                {"code": "B", "name": "加价", "diffFen": 200},
            ],
            zero_price_bonus=5.0,
        )
        self.assertEqual(with_bonus[0].code, "A")
        self.assertIn("zero_diff_bonus", with_bonus[0].reasons)

    def test_pinned_round_must_exist_and_is_respected(self):
        plan = combo_edit.plan_named_combo(self.detail(), keep={"3": "522021"})
        round3 = next(row for row in plan["decisions"] if row["round"] == "3")
        self.assertEqual(round3["chosen"]["code"], "522021")
        self.assertTrue(round3["pinned"])
        # Unknown codes never appear in the plan.
        plan2 = combo_edit.plan_named_combo(self.detail(), keep={"3": "not-on-menu"})
        round3b = next(row for row in plan2["decisions"] if row["round"] == "3")
        self.assertNotEqual(round3b["chosen"]["code"], "not-on-menu")

    def test_swapping_a_side_changes_only_that_round(self):
        before = {"items": items()}
        after = {"items": items(side="4800")}
        changes = combo_edit.describe_swap(before, after)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["round"], "2")
        self.assertEqual(changes[0]["before"], ["4810"])
        self.assertEqual(changes[0]["after"], ["4800"])


class SnapshotSyncTest(unittest.TestCase):
    def pending(self, **kwargs):
        base = {
            "ok": True,
            "rotationKey": "db6671_515295_4810_3060",
            "humanSummary": "套餐（双层脆鸡堡+中薯条+雪碧中杯去冰），实付¥23.9",
            "priceFen": 2390,
            "payableFen": 2390,
            "priceVerified": True,
            "priceState": "quoted",
            "priceLabel": "已核价",
            "items": items(),
            "storeCode": "400001",
            "dining": "eat-in",
            "phase": "quoted_awaiting_confirm",
        }
        base.update(kwargs)
        return base

    def calc(self, **kwargs):
        now = datetime(2026, 11, 10, 12, 19, 16, tzinfo=CST)
        base = {
            "ok": True,
            "rotationKey": "db6671_515295_4810_3060",
            "humanSummary": "套餐（双层脆鸡堡+中薯条+雪碧中杯去冰）",
            "priceFen": 2390,
            "payableFen": 2390,
            "priceVerified": True,
            "quotedAt": now.isoformat(),
            "fetchedAt": now.isoformat(),
            "items": items(),
            "storeCode": "400001",
            "dining": "eat-in",
        }
        base.update(kwargs)
        return base

    def test_consistent_snapshots_quote_as_paid(self):
        result = combo_edit.sync_snapshots(
            self.pending(), self.calc(), now=datetime(2026, 11, 10, 12, 40, 0, tzinfo=CST)
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["pending"]["priceVerified"])
        self.assertEqual(result["pending"]["priceState"], quote_state.QUOTED)
        self.assertIn("实付 ¥23.9", result["pending"]["humanSummary"])
        self.assertEqual(result["pending"]["phase"], "quoted_awaiting_confirm")
        self.assertFalse(result["pending"]["orderCreated"])

    def test_expired_calc_does_not_keep_paid_wording(self):
        result = combo_edit.sync_snapshots(
            self.pending(),
            self.calc(),
            now=datetime(2026, 11, 10, 15, 0, 0, tzinfo=CST),
        )
        self.assertTrue(result["ok"])
        merged = result["pending"]
        self.assertFalse(merged["priceVerified"])
        self.assertIsNone(merged["payableFen"])
        self.assertTrue(merged["needsQuoteBeforeConfirm"])
        self.assertNotIn("实付", merged["humanSummary"])
        self.assertEqual(merged["phase"], "estimate_requote_required")

    def test_swap_between_pending_and_calc_is_refused(self):
        stale_pending = self.pending()
        stale_pending["items"] = items(side="4800")
        result = combo_edit.sync_snapshots(stale_pending, self.calc())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "snapshots_describe_different_orders")
        self.assertIn("round_list_mismatch", result["issues"])

    def test_calc_without_quote_timestamp_is_treated_as_estimate(self):
        calc = self.calc()
        calc.pop("quotedAt")
        calc.pop("fetchedAt")
        result = combo_edit.sync_snapshots(self.pending(), calc)
        merged = result["pending"]
        self.assertFalse(merged["priceVerified"])
        self.assertIn("估价", merged["humanSummary"])

    def test_summary_price_wording_mismatch_is_reported(self):
        calc = self.calc()
        calc["humanSummary"] = "套餐（双层脆鸡堡+中薯条+雪碧中杯去冰）"
        report = quote_state.consistency_report(self.pending(), calc)
        self.assertIn("human_summary_price_wording_mismatch", report["issues"])
        self.assertFalse(report["consistent"])


class CandidateFamilyExplanationTest(unittest.TestCase):
    """built 66 项声明 dual_1plus1 却 0 条时必须给出可解释原因。"""

    def menu(self):
        return {
            "categories": [
                {
                    "name": "随心配1+1",
                    "meals": [
                        {"code": "9900016651", "name": "随心配1+1三件套!!!", "currentPrice": "21.90"},
                        {"code": "9900016655", "name": "双吉随心配1+1", "currentPrice": "13.90"},
                        {"code": "9900016668", "name": "真有料随心配1+1", "currentPrice": "14.90"},
                    ],
                }
            ],
            "mealIndex": {
                "9900016651": {"name": "随心配1+1三件套!!!", "currentPrice": "21.90"},
                "9900016655": {"name": "双吉随心配1+1", "currentPrice": "13.90"},
                "9900016668": {"name": "真有料随心配1+1", "currentPrice": "14.90"},
            },
        }

    def detail_left_three(self):
        return {
            "name": "随心配1+1三件套!!!",
            "rounds": [
                {"id": "1", "name": "红区", "choices": [{"code": "522032", "name": "酥酥多笋卷"}]},
                {"id": "2", "name": "小薯条", "choices": [{"code": "4800", "name": "小薯条"}]},
                {"id": "3", "name": "白区", "choices": [{"code": "522020", "name": "可乐大杯"}]},
            ],
        }

    def detail_right(self):
        return {
            "name": "真有料随心配1+1",
            "rounds": [
                {"id": "1", "name": "红区", "choices": [{"code": "505416", "name": "脆汁鸡琵琶腿"}]},
                {"id": "2", "name": "白区", "choices": [{"code": "4105", "name": "菠萝派"}]},
            ],
        }

    def test_declared_family_with_zero_yield_names_the_blocker(self):
        import build_candidates as bc

        details = {"9900016651": self.detail_left_three(), "9900016668": self.detail_right()}
        cands = bc.expand_dual_1plus1(details, self.menu(), ["522021"], "0-0", budget_fen=3500)
        self.assertEqual(cands, [])
        diag = bc.explain_families(details, self.menu(), {}, ["dual_1plus1"], 3500, cands)
        row = diag["dual_1plus1"]
        self.assertEqual(row["generated"], 0)
        self.assertTrue(row["explained"])
        self.assertTrue(any(item.startswith("missing_detail:9900016655") for item in row["blockers"]))

    def test_budget_blocker_reports_the_amount(self):
        import build_candidates as bc

        details = {"9900016651": self.detail_left_three(), "9900016668": self.detail_right()}
        diag = bc.explain_families(details, self.menu(), {}, ["dual_1plus1"], 3500, [])
        blockers = diag["dual_1plus1"]["blockers"]
        self.assertTrue(any("over_budget:9900016651+9900016668=3680>3500" == item for item in blockers))

    def test_family_with_candidates_is_not_flagged(self):
        import build_candidates as bc

        details = {
            "9900016655": {
                "name": "双吉随心配1+1",
                "rounds": [
                    {"id": "1", "name": "红区", "choices": [{"code": "1120", "name": "双层吉士汉堡"}]},
                    {"id": "2", "name": "饮料", "choices": [{"code": "522021", "name": "无糖可乐大杯"}]},
                ],
            },
            "9900016668": self.detail_right(),
        }
        cands = bc.expand_dual_1plus1(details, self.menu(), ["522021"], "0-0", budget_fen=3500)
        self.assertTrue(cands)
        diag = bc.explain_families(details, self.menu(), {}, ["dual_1plus1"], 3500, cands)
        self.assertGreater(diag["dual_1plus1"]["generated"], 0)
        self.assertTrue(diag["dual_1plus1"]["explained"])


class SummaryRenderingTest(unittest.TestCase):
    def test_price_suffix_is_stripped_before_rerender(self):
        for summary in (
            "套餐（甲+乙）≈¥23.9",
            "套餐（甲+乙），实付¥23.9",
            "套餐（甲+乙）约 ¥23.9",
            "套餐（甲+乙）¥23.9",
        ):
            self.assertEqual(quote_state.strip_price_suffix(summary), "套餐（甲+乙）", msg=summary)

    def test_estimate_label_uses_approx(self):
        status = quote_state.PriceStatus(
            state=quote_state.ESTIMATE,
            price_fen=2390,
            payable_fen=None,
            verified=False,
            needs_quote=True,
            label="估价",
            reason="no_quote",
        )
        text = quote_state.compose_summary("套餐（甲+乙）≈¥23.9", status)
        self.assertEqual(text, "套餐（甲+乙），估价 ¥23.9")
        self.assertNotIn("实付", text)


if __name__ == "__main__":
    unittest.main()
