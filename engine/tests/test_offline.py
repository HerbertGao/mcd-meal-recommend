import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
EXAMPLES = ENGINE / "examples"
sys.path.insert(0, str(ENGINE))

import score_lunch  # noqa: E402
from mcp_contract import ContractError, normalize_menu, rounds_of  # noqa: E402
from profile_lib import profile_to_request  # noqa: E402


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def validate(instance, schema_name):
    jsonschema.Draft202012Validator(load(ENGINE / "schema" / schema_name)).validate(instance)


class OfflineContractTest(unittest.TestCase):
    def test_build_and_score_from_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = Path(tmp) / "request-built.json"
            subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "build_candidates.py"),
                    "--base",
                    str(EXAMPLES / "request-offline.json"),
                    "--menu",
                    str(EXAMPLES / "fixtures" / "menu.json"),
                    "--details-dir",
                    str(EXAMPLES / "fixtures" / "meal-details"),
                    "-o",
                    str(built),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            validate(load(built), "recommend-request.schema.json")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "score_lunch.py"),
                    "-i",
                    str(built),
                    "--seed",
                    "1",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            response = json.loads(result.stdout)

        validate(response, "recommend-response.schema.json")
        self.assertTrue(response["ok"])
        self.assertLessEqual(response["priceFen"], 3500)
        self.assertTrue(response["items"])
        self.assertFalse(response["priceVerified"])
        self.assertFalse(response["fallbackUsed"])
        with self.assertRaises(jsonschema.ValidationError):
            validate({**response, "items": []}, "recommend-response.schema.json")

    def test_candidate_sample_is_repeatable_except_timestamp(self):
        request = load(EXAMPLES / "candidates-sample.json")
        first, _ = score_lunch.run(request, seed=7)
        second, _ = score_lunch.run(request, seed=7)
        validate(first, "recommend-response.schema.json")
        self.assertEqual(first["rotationKey"], second["rotationKey"])
        self.assertEqual(first["score"], second["score"])
        self.assertLessEqual(first["priceFen"], request["budgetFen"])
        self.assertFalse(first["priceVerified"])
        self.assertTrue(any("fresh calculate-price" in warning for warning in first["warnings"]))

    def test_no_candidate_response_has_no_fake_order_item(self):
        request = load(EXAMPLES / "candidates-sample.json")
        request["budgetFen"] = 1
        response, ranked = score_lunch.run(request, seed=1)
        validate(response, "recommend-response.schema.json")
        self.assertFalse(response["ok"])
        self.assertEqual(response["items"], [])
        self.assertEqual(ranked, [])
        with self.assertRaises(jsonschema.ValidationError):
            validate(
                {**response, "items": response["items"] + [{"productCode": "0"}]},
                "recommend-response.schema.json",
            )

    def test_explicit_empty_candidates_do_not_use_historical_fallback(self):
        request = load(EXAMPLES / "request-offline.json")
        request["candidates"] = []
        response, _ = score_lunch.run(request, seed=1)
        self.assertFalse(response["ok"])
        self.assertEqual(response["items"], [])
        self.assertFalse(response["fallbackUsed"])
        self.assertFalse(any("loaded templates" in warning for warning in response["warnings"]))

        request["candidates"] = [{"rotationKey": "missing-items"}]
        response, _ = score_lunch.run(request, seed=1)
        self.assertFalse(response["ok"])
        self.assertFalse(response["fallbackUsed"])
        self.assertFalse(any("loaded templates" in warning for warning in response["warnings"]))

    def test_mismatched_fallback_is_not_retried_or_marked_used(self):
        request = load(EXAMPLES / "request-offline.json")
        # Force a store the bundled fallback template does not cover.
        request["storeCode"] = "111111"
        response, _ = score_lunch.run(request, seed=1)
        self.assertFalse(response["ok"])
        self.assertFalse(response["fallbackUsed"])
        mismatches = [warning for warning in response["warnings"] if "does not match" in warning]
        self.assertEqual(len(mismatches), 1)

    def test_detail_contract_reports_field_path(self):
        detail = load(EXAMPLES / "fixtures" / "meal-details" / "9900016655.json")
        del detail["rounds"][0]["choices"][0]["code"]
        with self.assertRaisesRegex(ContractError, r"rounds\[0\]\.choices\[0\]\.code"):
            rounds_of(detail)

    def test_menu_contract_rejects_invalid_price(self):
        menu = load(EXAMPLES / "fixtures" / "menu.json")
        menu["mealIndex"]["9900016655"]["currentPrice"] = "unknown"
        with self.assertRaisesRegex(ContractError, r"mealIndex\.9900016655\.currentPrice"):
            normalize_menu(menu)

    def test_detail_contract_allows_new_optional_fields(self):
        detail = load(EXAMPLES / "fixtures" / "meal-details" / "9900016655.json")
        detail["futureField"] = {"ignored": True}
        detail["rounds"][0]["choices"][0]["futureChoiceField"] = "accepted"
        self.assertEqual(len(rounds_of(detail)), 2)

    def test_missing_snapshot_has_cli_error_without_traceback(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ENGINE / "build_candidates.py"),
                "--base",
                str(EXAMPLES / "request-offline.json"),
                "--menu",
                str(EXAMPLES / "fixtures" / "missing.json"),
                "--details-dir",
                str(EXAMPLES / "fixtures" / "meal-details"),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing.json: file not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_profile_requires_explicit_numeric_store(self):
        profile = load(ENGINE / "profiles" / "templates" / "lunch.example.json")
        with self.assertRaisesRegex(ValueError, "storeCode"):
            profile_to_request(profile)
        self.assertEqual(
            profile_to_request(profile, {"storeCode": "000000"})["storeCode"],
            "000000",
        )
        profile["storeCode"] = "000000"
        self.assertEqual(profile_to_request(profile)["storeCode"], "000000")
        with self.assertRaisesRegex(ValueError, "storeCode"):
            profile_to_request(profile, {"storeCode": "YOUR_STORE_CODE"})

    def test_fixed_recommendation_evaluation(self):
        result = subprocess.run(
            [sys.executable, str(ENGINE / "evaluate_recommendations.py")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["scenarioCount"], 10)
        self.assertEqual(report["failedExpectations"], 0)
        self.assertEqual(report["hardConstraintViolations"], 0)
        self.assertEqual(report["unexpectedVerifiedPrices"], 0)

    def test_missing_candidate_price_has_cli_error_without_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            menu_path = Path(tmp) / "menu.json"
            menu = load(EXAMPLES / "fixtures" / "menu.json")
            del menu["mealIndex"]["9900016655"]["currentPrice"]
            menu_path.write_text(json.dumps(menu), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "build_candidates.py"),
                    "--base",
                    str(EXAMPLES / "request-offline.json"),
                    "--menu",
                    str(menu_path),
                    "--details-dir",
                    str(EXAMPLES / "fixtures" / "meal-details"),
                ],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mealIndex.9900016655.currentPrice", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_delivery_budget_requires_quote(self):
        from budget import apply_quote, budget_check

        cand = score_lunch.ScoredCandidate(
            rotation_key="delivery-1690",
            human_summary="two piece",
            price_fen=1690,
            items=[],
        )
        request = {"budgetFen": 2000, "dining": "delivery"}
        # Unquoted delivery totals must not be reported as confirmed or in budget.
        self.assertEqual(budget_check(cand, request), "unverified_delivery_total")
        apply_quote(cand, {"productPrice": 1690, "price": 2290})
        self.assertEqual(cand.payable_fen, 2290)
        self.assertTrue(cand.price_verified)
        self.assertEqual(budget_check(cand, request), "over_budget_payable:2290>2000")

    def test_pickup_budget_uses_goods_price(self):
        from budget import budget_check

        cand = score_lunch.ScoredCandidate(
            rotation_key="pickup-2780", human_summary="lunch", price_fen=2780, items=[]
        )
        self.assertIsNone(budget_check(cand, {"budgetFen": 3500, "dining": "eat-in"}))

    def test_rank_only_returns_highest_score(self):
        import random as _random

        high = score_lunch.ScoredCandidate(
            rotation_key="high",
            human_summary="h",
            price_fen=2800,
            items=[],
            tags=["dual11"],
            score=90.0,
            meta={"primaryMainCode": "1120"},
        )
        low = score_lunch.ScoredCandidate(
            rotation_key="low",
            human_summary="l",
            price_fen=2800,
            items=[],
            tags=["mcard_four"],
            score=60.0,
            meta={"primaryMainCode": "1450"},
        )
        request = {
            "selection": {
                "mode": "rank_only",
                "topK": 1,
                "diversifyByFamily": True,
                "diversifyByMain": True,
            },
            "prefs": {"priority": ["mcard_four", "dual_1plus1"]},
        }
        pick, _, _ = score_lunch.select_from_ranked([high, low], request, _random.Random(1))
        self.assertEqual(pick.rotation_key, "high")

    def test_history_window_zero_disables_window(self):
        request = {
            "historyWindowPicks": 0,
            "historyWindowDays": 0,
            "history": [],
            "historyTracks": {
                "mains": [{"code": "1120", "at": "2026-11-03T12:00:00+08:00"}],
                "sides": [],
            },
        }
        self.assertEqual(score_lunch.recent_track_codes(request, "mains"), [])

    def test_real_menu_meals_field_is_accepted(self):
        from mcp_contract import normalize_menu

        menu = {
            "meals": {"9900016670": {"name": "三件套", "currentPrice": "29.9"}},
            "categories": [],
        }
        normalized = normalize_menu(menu)
        self.assertIn("mealIndex", normalized)
        self.assertEqual(normalized["mealIndex"]["9900016670"]["currentPrice"], "29.9")

    def test_family_rotation_key_mapping(self):
        cases = {
            "mk0884_522032_4810": "mcard_four",
            "sj1120+zl505416_w4105": "dual_1plus1",
            "db6670_1100_4810": "dabao",
            "ct4064_1440_4810": "classic_three",
            "bf3360_521315_900479": "coffee_combo",
            "unknown": "other",
        }
        for key, family in cases.items():
            with self.subTest(key=key):
                self.assertEqual(score_lunch.family_of_rotation_key(key), family)

    def test_recent_meal_families_follows_insertion_order(self):
        # Several picks can share one timestamp; append order is the truth.
        request = {
            "history": [
                {"date": "2026-01-01", "at": "2026-01-01T12:00:00+08:00", "rotationKey": "mk0001_a"},
                {"date": "2026-01-01", "at": "2026-01-01T12:00:00+08:00", "rotationKey": "sj0002_b"},
            ]
        }
        self.assertEqual(
            score_lunch.recent_meal_families(request, limit=2),
            ["dual_1plus1", "mcard_four"],
        )

    def test_family_rotation_lets_another_family_win(self):
        def cand(key, score, tag, main):
            return score_lunch.ScoredCandidate(
                rotation_key=key,
                human_summary=key,
                price_fen=2800,
                items=[],
                tags=[tag],
                score=score,
                meta={"primaryMainCode": main},
            )

        strong = cand("mk0001_a", 60.0, "mcard_four", "522032")
        weaker = cand("db0002_b", 40.0, "dabao", "1100")
        cands = [strong, weaker]
        request = {
            "prefs": {"familyRepeatPenalty": 0.55},
            "history": [{"date": "2026-01-01", "rotationKey": "mk0001_a"}],
        }
        score_lunch.apply_family_rotation(cands, request)
        ranked = sorted(cands, key=lambda c: -c.score)
        self.assertEqual(ranked[0].rotation_key, "db0002_b")
        self.assertEqual(strong.meta.get("familyRotationFactor"), 0.55)

    def test_shipped_profiles_use_rank_only(self):
        # Uniform random over a family-interleaved pool can surface a far
        # lower-scoring candidate, so the shipped profiles pick by rank.
        for name in ("lunch.example.json", "breakfast.example.json"):
            with self.subTest(name=name):
                profile = load(ENGINE / "profiles" / "templates" / name)
                self.assertEqual(profile["selection"]["mode"], "rank_only")

    def test_rank_only_beats_family_pool_decoy(self):
        import random as _random

        def cand(key, score, main, tag):
            return score_lunch.ScoredCandidate(
                rotation_key=key,
                human_summary=key,
                price_fen=2800,
                items=[],
                tags=[tag],
                score=score,
                meta={"primaryMainCode": main},
            )

        strong = cand("strong", 65.0, "522032", "dual11")
        decoy = cand("decoy", 33.5, "1440", "classic_three")
        ranked = sorted([strong, decoy], key=lambda c: -c.score)
        request = {
            "selection": {
                "mode": "rank_only",
                "topK": 5,
                "diversifyByFamily": True,
                "diversifyByMain": True,
            },
            "candidateFamilies": ["classic_three", "dual_1plus1"],
            "prefs": {"priority": ["classic_three", "dual_1plus1"]},
        }
        pick, _, _ = score_lunch.select_from_ranked(ranked, request, _random.Random(3))
        self.assertEqual(pick.rotation_key, "strong")

    def test_mcard_four_enumerates_menu_side_pairs_and_drinks(self):
        from build_candidates import expand_mcard_four

        def choice(code, name, diff="+¥0.00"):
            return {"code": code, "name": name, "diffPrice": diff}

        detail = {
            "name": "酥酥多笋卷四件套",
            "rounds": [
                {
                    "id": "1",
                    "name": "主食",
                    "choices": [
                        choice("1100", "巨无霸"),
                        choice("1440", "麦辣鸡腿堡"),
                        choice("1401", "麦乐鸡5块"),
                    ],
                },
                {
                    "id": "2",
                    "name": "小食",
                    "choices": [
                        choice("4810", "中薯条"),
                        choice("4437", "玉米杯"),
                    ],
                },
                {
                    "id": "3",
                    "name": "小食",
                    "choices": [
                        choice("515106", "麦乐鸡4块"),
                        choice("4105", "菠萝派"),
                    ],
                },
                {
                    "id": "4",
                    "name": "饮料",
                    "choices": [
                        choice("522021", "无糖可口可乐大杯"),
                        choice("3050", "雪碧中杯"),
                        choice("3060", "芬达中杯"),
                        choice("3110", "橙汁"),
                        choice("3071", "无糖可乐中杯"),
                        choice("522020", "可口可乐"),
                    ],
                },
            ],
        }
        menu = {
            "mealIndex": {
                "9900000884": {"name": "酥酥多笋卷四件套", "currentPrice": 28},
            }
        }
        cands = expand_mcard_four(
            {"9900000884": detail},
            menu,
            ["522021"],
            "0-0",
            False,
            budget_fen=3500,
            prefer_sides=["4810"],
        )
        self.assertTrue(cands)
        mains = []
        for cand in cands:
            main = cand["primaryMainCode"]
            if not mains or mains[-1] != main:
                mains.append(main)
        self.assertEqual(mains, ["1100", "1440"])
        self.assertNotIn("1401", {c["primaryMainCode"] for c in cands})

        for_main = [c for c in cands if c["primaryMainCode"] == "1100"]
        pairs = {tuple(c["sideCodes"]) for c in for_main}
        drinks = {c["drinkCodes"][0] for c in for_main}
        self.assertIn(("4810", "515106"), pairs)
        self.assertIn(("4437", "4105"), pairs)
        self.assertGreater(len(pairs), 1)
        self.assertIn("522021", drinks)
        self.assertGreater(len(drinks), 1)
        self.assertTrue(drinks - {"522021"})
        menu_drinks = {"522021", "3050", "3060", "3110", "3071", "522020"}
        self.assertTrue(drinks <= menu_drinks)
        sample = for_main[0]
        self.assertEqual(
            [rnd["round"] for rnd in sample["items"][0]["roundList"]],
            ["1", "2", "3", "4"],
        )

    def test_mcard_four_budget_drops_expensive_side_and_drink(self):
        from build_candidates import expand_mcard_four

        def choice(code, name, diff="+¥0.00"):
            return {"code": code, "name": name, "diffPrice": diff}

        detail = {
            "name": "人气超值四件套随心选",
            "rounds": [
                {"id": "a", "name": "主食", "choices": [choice("1100", "巨无霸")]},
                {"id": "b", "name": "小食", "choices": [choice("4810", "中薯条")]},
                {
                    "id": "c",
                    "name": "小食",
                    "choices": [
                        choice("515106", "麦乐鸡4块"),
                        choice("4438", "大玉米杯", "+¥5.00"),
                    ],
                },
                {
                    "id": "d",
                    "name": "饮料",
                    "choices": [
                        choice("3050", "雪碧中杯"),
                        choice("520688", "大杯鲜萃咖啡", "+¥6.00"),
                    ],
                },
            ],
        }
        menu = {"mealIndex": {"9900004236": {"name": detail["name"], "currentPrice": 28}}}
        cands = expand_mcard_four(
            {"9900004236": detail},
            menu,
            ["520688"],
            "0-0",
            False,
            budget_fen=3000,
        )
        self.assertTrue(cands)
        self.assertTrue(all(c["listPriceFen"] <= 3000 for c in cands))
        self.assertTrue(all("4438" not in c["sideCodes"] for c in cands))
        self.assertTrue(all(c["drinkCodes"] == ["3050"] for c in cands))
        self.assertIn("515106", cands[0]["sideCodes"])

    def test_recent_second_side_and_drink_downweight(self):
        from build_candidates import expand_mcard_four

        def choice(code, name, diff="+¥0.00"):
            return {"code": code, "name": name, "diffPrice": diff}

        detail = {
            "name": "酥酥多笋卷四件套",
            "rounds": [
                {"id": "1", "name": "主食", "choices": [choice("1100", "巨无霸")]},
                {"id": "2", "name": "小食", "choices": [choice("4810", "中薯条"), choice("4437", "玉米杯")]},
                {"id": "3", "name": "小食", "choices": [choice("515106", "麦乐鸡4块"), choice("4105", "菠萝派")]},
                {"id": "4", "name": "饮料", "choices": [choice("522021", "无糖可乐"), choice("3050", "雪碧")]},
            ],
        }
        menu = {"mealIndex": {"9900000884": {"name": detail["name"], "currentPrice": 28}}}
        raws = expand_mcard_four(
            {"9900000884": detail},
            menu,
            ["522021"],
            "0-0",
            False,
            budget_fen=3500,
        )
        target = next(
            c
            for c in raws
            if c["sideCodes"] == ["4810", "515106"] and c["drinkCodes"] == ["3050"]
        )

        def score_with(history_tracks=None, history=None, drink_codes=None):
            meta = {
                "primaryMainCode": target["primaryMainCode"],
                "sideCodes": list(target["sideCodes"]),
                "drinkCodes": list(drink_codes or target["drinkCodes"]),
                "mcardFour": True,
            }
            cand = score_lunch.ScoredCandidate(
                rotation_key=target["rotationKey"] + "-".join(meta["drinkCodes"]),
                human_summary=target["humanSummary"],
                price_fen=target["listPriceFen"],
                items=target["items"],
                tags=list(target["tags"]),
                meta=meta,
            )
            req = {
                "budgetFen": 3500,
                "mealPeriod": "lunch",
                "dining": "eat-in",
                "history": history or [],
                "historyWindowDays": 5,
                "historyWindowPicks": 5,
                "prefs": {
                    "priority": ["low_repeat", "satiety", "deal", "drink"],
                    "satiety": "high",
                    "lowRecentRepeat": True,
                    "drink": {"ice": "no_ice", "preferCodes": ["522021"]},
                    "unseenSideBoost": 1.12,
                    "unseenDrinkBoost": 1.12,
                    "unseenMainBoost": 1.0,
                    "unseenComboCap": 1.6,
                },
            }
            if history_tracks is not None:
                req["historyTracks"] = history_tracks
            score_lunch.score_candidate(cand, req)
            return cand

        at = "2026-11-04T12:00:00+08:00"
        base = score_with(history_tracks={"mains": [], "sides": [], "drinks": []})
        second_side = score_with(
            history_tracks={
                "mains": [],
                "sides": [{"code": "515106", "at": at, "rotationKey": "prev-side"}],
                "drinks": [],
            }
        )
        self.assertEqual(second_side.meta["primarySideCode"], "4810")
        self.assertLess(second_side.meta["repeatSideMult"], 1)
        self.assertLess(second_side.breakdown["low_repeat"], base.breakdown["low_repeat"])

        both_sides_seen = score_with(
            history_tracks={
                "mains": [],
                "sides": [
                    {"code": "4810", "at": at, "rotationKey": "prev-side"},
                    {"code": "515106", "at": at, "rotationKey": "prev-side"},
                ],
                "drinks": [],
            }
        )
        self.assertFalse(both_sides_seen.meta["unseenSide"])
        fresh_sides = score_with(
            history_tracks={
                "mains": [],
                "sides": [{"code": "4105", "at": at, "rotationKey": "prev-other"}],
                "drinks": [],
            }
        )
        self.assertTrue(fresh_sides.meta["unseenSide"])
        self.assertIn("515106", fresh_sides.meta["unseenSideCodes"])

        repeated_drink = score_with(
            history_tracks={
                "mains": [],
                "sides": [],
                "drinks": [{"code": "3050", "at": at, "rotationKey": "prev-drink"}],
            }
        )
        self.assertLess(repeated_drink.meta["repeatDrinkMult"], 1)
        self.assertFalse(repeated_drink.meta["unseenDrink"])
        self.assertLess(repeated_drink.breakdown["low_repeat"], base.breakdown["low_repeat"])

        other_drink = score_with(
            history_tracks={
                "mains": [],
                "sides": [],
                "drinks": [{"code": "522021", "at": at, "rotationKey": "prev-cola"}],
            }
        )
        self.assertTrue(other_drink.meta["unseenDrink"])
        self.assertEqual(other_drink.meta["repeatDrinkMult"], 1)

        second_drink = score_with(
            history_tracks={
                "mains": [],
                "sides": [],
                "drinks": [{"code": "3050", "at": at, "rotationKey": "prev-sprite"}],
            },
            drink_codes=["522021", "3050"],
        )
        self.assertEqual(second_drink.meta["primaryDrinkCode"], "522021")
        self.assertLess(second_drink.meta["repeatDrinkMult"], 1)

        derived = score_lunch.recent_track_codes(
            {
                "historyWindowDays": 5,
                "historyWindowPicks": 5,
                "history": [
                    {
                        "date": "2026-11-04",
                        "at": at,
                        "rotationKey": "prev",
                        "drinkCode": "522021",
                        "extraDrinkCodes": ["3050"],
                        "sideCode": "4810",
                        "extraSideCodes": ["515106"],
                    }
                ],
            },
            "drinks",
        )
        self.assertEqual({code for code, _age in derived}, {"522021", "3050"})

    def test_select_drinks_never_uses_pies_or_fries(self):
        from build_candidates import select_drinks

        def ch(code, name):
            return {"code": code, "name": name, "diffFen": 0}

        pies = select_drinks(
            [ch("4105", "菠萝派"), ch("4810", "中薯条")],
            ["4105", "4810"],
            False,
            0,
            [],
            None,
        )
        self.assertEqual(pies, [])

        mixed = select_drinks(
            [ch("522021", "无糖可乐"), ch("4105", "菠萝派"), ch("900001", "定制杯装")],
            ["900001"],
            False,
            0,
            [],
            None,
        )
        self.assertEqual({c["code"] for c in mixed}, {"522021", "900001"})

        dropped = select_drinks(
            [ch("522021", "无糖可乐"), ch("900001", "定制杯装")],
            [],
            False,
            0,
            [],
            None,
        )
        self.assertEqual([c["code"] for c in dropped], ["522021"])

        unknown_only = select_drinks(
            [ch("900001", "定制杯装"), ch("4810", "中薯条")],
            [],
            False,
            0,
            [],
            None,
        )
        self.assertEqual([c["code"] for c in unknown_only], ["900001"])

    def test_list_history_keeps_extra_side_and_drink_codes(self):
        from profile_lib import normalize_history_doc, profile_to_request

        raw = {
            "date": "2026-11-04",
            "at": "2026-11-04T12:00:00+08:00",
            "rotationKey": "mk0884_1100_4810_515106_3050",
            "mainCode": "1100",
            "sideCode": "4810",
            "extraSideCodes": ["515106", "515106"],
            "drinkCode": "3050",
            "extraDrinkCodes": ["522021"],
        }
        doc = normalize_history_doc([raw])
        self.assertEqual([row["code"] for row in doc["sides"]], ["4810", "515106"])
        self.assertEqual([row["code"] for row in doc["drinks"]], ["3050", "522021"])
        self.assertEqual(doc["entries"][0]["extraSideCodes"], ["515106"])
        self.assertEqual(doc["entries"][0]["extraDrinkCodes"], ["522021"])

        loaded = profile_to_request({"storeCode": "000000", "history": [raw]})
        self.assertEqual(
            [row["code"] for row in loaded["historyTracks"]["sides"]],
            ["4810", "515106"],
        )
        self.assertEqual(
            [row["code"] for row in loaded["historyTracks"]["drinks"]],
            ["3050", "522021"],
        )

        cand = score_lunch.ScoredCandidate(
            rotation_key="mk0884_1100_4810_515106_522021",
            human_summary="four",
            price_fen=2800,
            items=[
                {
                    "productCode": "9900000884",
                    "quantity": 1,
                    "roundList": [
                        {"round": "1", "comboItemList": [{"code": "1100", "quantity": 1}]},
                        {"round": "2", "comboItemList": [{"code": "4810", "quantity": 1}]},
                        {"round": "3", "comboItemList": [{"code": "515106", "quantity": 1}]},
                        {"round": "4", "comboItemList": [{"code": "522021", "quantity": 1}]},
                    ],
                }
            ],
            tags=["mcard_four"],
            meta={"sideCodes": ["4810", "515106"], "drinkCodes": ["522021"], "primaryMainCode": "1100"},
        )
        req = {
            "budgetFen": 3500,
            "mealPeriod": "lunch",
            "dining": "eat-in",
            "history": loaded["history"],
            "historyTracks": loaded["historyTracks"],
            "historyWindowDays": 5,
            "historyWindowPicks": 5,
            "prefs": {
                "priority": ["low_repeat"],
                "satiety": "off",
                "lowRecentRepeat": True,
                "drink": {"ice": "no_ice"},
                "unseenMainBoost": 1.0,
                "unseenSideBoost": 1.0,
                "unseenDrinkBoost": 1.0,
                "unseenComboCap": 1.0,
            },
        }
        score_lunch.score_candidate(cand, req)
        self.assertLess(cand.meta["repeatSideMult"], 1)
        self.assertEqual(cand.meta["primarySideCode"], "4810")
        neutral = score_lunch.ScoredCandidate(
            rotation_key="mk0884_1100_4437_4105_522021",
            human_summary="other sides",
            price_fen=2800,
            items=cand.items,
            tags=["mcard_four"],
            meta={"sideCodes": ["4437", "4105"], "drinkCodes": ["522021"], "primaryMainCode": "1100"},
        )
        score_lunch.score_candidate(neutral, req)
        self.assertEqual(neutral.meta["repeatSideMult"], 1)
        self.assertLess(cand.breakdown["low_repeat"], neutral.breakdown["low_repeat"])

    def test_example_profiles_document_soft_prefs_without_personal_data(self):
        gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("engine/profiles/lunch.json", gi)
        self.assertEqual(list((ENGINE / "profiles").glob("*.json")), [])
        for name in ("lunch.example.json", "breakfast.example.json"):
            profile = load(ENGINE / "profiles" / "templates" / name)
            self.assertEqual(profile["storeCode"], "YOUR_STORE_CODE")
            self.assertEqual(profile["prefs"]["preferSideCodes"], [])
            self.assertEqual(profile["prefs"]["unseenDrinkBoost"], 1.12)
            request = load(EXAMPLES / "request-offline.json")
            request["prefs"] = profile["prefs"]
            validate(request, "recommend-request.schema.json")

    def test_documented_examples_match_schemas(self):
        for name in (
            "request-default.json",
            "request-breakfast-default.json",
            "candidates-sample.json",
            "request-offline.json",
        ):
            with self.subTest(name=name):
                validate(load(EXAMPLES / name), "recommend-request.schema.json")
        for name in ("response-verified-2880.json", "score-run-sample.json"):
            with self.subTest(name=name):
                validate(load(EXAMPLES / name), "recommend-response.schema.json")


if __name__ == "__main__":
    unittest.main()
