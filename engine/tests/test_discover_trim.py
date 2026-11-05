"""Menu discovery and fair trim for lunch deal families."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine"
EXAMPLES = ENGINE / "examples"
sys.path.insert(0, str(ENGINE))

import build_candidates as bc  # noqa: E402
from mcp_contract import load_snapshot  # noqa: E402


def choice(code, name, diff="+¥0.00"):
    return {"code": code, "name": name, "diffPrice": diff}


def product_codes(cand):
    return [str(item.get("productCode") or "") for item in cand.get("items") or []]


def dual_detail_6655():
    return {
        "name": "双吉随心配1+1",
        "rounds": [
            {
                "id": "1",
                "name": "红区",
                "choices": [choice("1120", "双层吉士汉堡"), choice("1450", "麦香鸡")],
            },
            {
                "id": "2",
                "name": "饮料",
                "choices": [choice("522021", "无糖可乐大杯"), choice("3050", "雪碧中杯")],
            },
        ],
    }


def dual_detail_6651():
    return {
        "name": "随心配1+1三件套!!!",
        "rounds": [
            {
                "id": "1",
                "name": "红区",
                "choices": [
                    choice("1120", "双层吉士汉堡"),
                    choice("1450", "麦香鸡"),
                    choice("1100", "巨无霸"),
                ],
            },
            {
                "id": "2",
                "name": "小薯条",
                "choices": [choice("4810", "中薯条"), choice("4800", "小薯条")],
            },
            {
                "id": "3",
                "name": "白区",
                "choices": [choice("522021", "无糖可乐大杯"), choice("3050", "雪碧中杯")],
            },
        ],
    }


def dual_detail_6668():
    return {
        "name": "真有料随心配1+1",
        "rounds": [
            {
                "id": "1",
                "name": "红区",
                "choices": [choice("505416", "脆汁鸡琵琶腿"), choice("1401", "麦乐鸡5块")],
            },
            {
                "id": "2",
                "name": "白区",
                "choices": [choice("4105", "菠萝派"), choice("4106", "香芋派")],
            },
        ],
    }


def indexed(code, name, price):
    return {"code": code, "name": name, "currentPrice": price}


class DealDiscoveryTest(unittest.TestCase):
    def test_classify_by_name_and_by_round_shape(self):
        self.assertEqual(bc.classify_dual_role("随心配1+1三件套!!!", dual_detail_6651()), "left_three")
        self.assertEqual(bc.classify_dual_role("双吉随心配1+1", dual_detail_6655()), "left_burger")
        self.assertEqual(bc.classify_dual_role("真有料随心配1+1", dual_detail_6668()), "right")

        unnamed_left = {
            "name": "随心配1+1新品桶",
            "rounds": [
                {"id": "1", "name": "红区", "choices": [choice("1100", "巨无霸")]},
                {"id": "2", "name": "饮料", "choices": [choice("3050", "雪碧中杯")]},
            ],
        }
        unnamed_three = {
            "name": "随心配1+1加薯条",
            "rounds": [
                {"id": "1", "name": "红区", "choices": [choice("1100", "巨无霸")]},
                {"id": "2", "name": "小薯条", "choices": [choice("4810", "中薯条")]},
                {"id": "3", "name": "白区", "choices": [choice("3050", "雪碧中杯")]},
            ],
        }
        unnamed_right = {
            "name": "随心配1+1小食桶",
            "rounds": [
                {"id": "1", "name": "红区", "choices": [choice("505416", "脆汁鸡琵琶腿")]},
                {"id": "2", "name": "白区", "choices": [choice("4105", "菠萝派")]},
            ],
        }
        self.assertEqual(bc.classify_dual_role(unnamed_left["name"], unnamed_left), "left_burger")
        self.assertEqual(bc.classify_dual_role(unnamed_three["name"], unnamed_three), "left_three")
        self.assertEqual(bc.classify_dual_role(unnamed_right["name"], unnamed_right), "right")
        self.assertIsNone(bc.classify_dual_role("巨无霸四件套", None))

    def test_category_discovers_dual_legs_when_fallback_lists_are_empty(self):
        menu = {
            "categories": [
                {
                    "name": "随心配1+1",
                    "meals": [
                        indexed("9900016651", "随心配1+1三件套!!!", "21.90"),
                        indexed("9900016655", "双吉随心配1+1", "13.90"),
                        indexed("9900016668", "真有料随心配1+1", "14.90"),
                        indexed("9900016999", "开心乐园随心配", "20.00"),
                    ],
                }
            ],
            "mealIndex": {
                "9900016651": {"name": "随心配1+1三件套!!!", "currentPrice": "21.90"},
                "9900016655": {"name": "双吉随心配1+1", "currentPrice": "13.90"},
                "9900016668": {"name": "真有料随心配1+1", "currentPrice": "14.90"},
                "9900010000": {"name": "旧退路左腿", "currentPrice": "9.00"},
                "9900010001": {"name": "旧退路右腿", "currentPrice": "9.00"},
            },
        }
        details = {
            "9900016651": dual_detail_6651(),
            "9900016655": dual_detail_6655(),
            "9900016668": dual_detail_6668(),
        }
        saved = (bc.FALLBACK_DUAL_LEFT, bc.FALLBACK_DUAL_RIGHT, bc.DUAL11_LEFT, bc.DUAL11_RIGHT)
        bc.FALLBACK_DUAL_LEFT = ["9900010000"]
        bc.FALLBACK_DUAL_RIGHT = ["9900010001"]
        bc.DUAL11_LEFT = bc.FALLBACK_DUAL_LEFT
        bc.DUAL11_RIGHT = "9900010001"
        try:
            legs = bc.discover_dual_legs(menu, details)
            self.assertEqual(legs.source, "menu")
            self.assertEqual(legs.left, ["9900016651", "9900016655"])
            self.assertEqual(legs.right, ["9900016668"])
            self.assertEqual(legs.roles["9900016651"], "left_three")
            self.assertEqual(legs.roles["9900016655"], "left_burger")
            self.assertEqual(legs.roles["9900016668"], "right")
            self.assertNotIn("9900016999", legs.left + legs.right)
            self.assertNotIn("9900010000", legs.left + legs.right)
            cands = bc.expand_dual_1plus1(details, menu, ["522021"], "0-0", budget_fen=5000)
        finally:
            bc.FALLBACK_DUAL_LEFT, bc.FALLBACK_DUAL_RIGHT, bc.DUAL11_LEFT, bc.DUAL11_RIGHT = saved
        codes_6651 = [c for c in cands if "9900016651" in product_codes(c)]
        codes_6655 = [c for c in cands if "9900016655" in product_codes(c)]
        self.assertGreaterEqual(len(codes_6651), 4)
        self.assertGreaterEqual(len(codes_6655), 1)
        self.assertTrue(all("9900016668" in product_codes(c) for c in codes_6651))

    def test_fallback_used_only_without_category_or_name_signal(self):
        menu = {
            "mealIndex": {
                "9900016655": {"currentPrice": "13.90"},
                "9900016651": {"currentPrice": "21.90"},
                "9900016668": {"currentPrice": "14.90"},
            }
        }
        legs = bc.discover_dual_legs(menu, {})
        self.assertEqual(legs.source, "fallback")
        self.assertEqual(legs.left, ["9900016655", "9900016651"])
        self.assertEqual(legs.right, ["9900016668"])

        named = {
            "mealIndex": {
                "9900091001": {"name": "随心配1+1新品桶", "currentPrice": "16.00"},
                "9900091002": {"name": "随心配1+1小食桶", "currentPrice": "15.00"},
            }
        }
        details = {
            "9900091001": {
                "name": "随心配1+1新品桶",
                "rounds": [
                    {"id": "1", "name": "红区", "choices": [choice("1100", "巨无霸")]},
                    {"id": "2", "name": "饮料", "choices": [choice("3050", "雪碧中杯")]},
                ],
            },
            "9900091002": {
                "name": "随心配1+1小食桶",
                "rounds": [
                    {"id": "1", "name": "红区", "choices": [choice("505416", "脆汁鸡琵琶腿")]},
                    {"id": "2", "name": "白区", "choices": [choice("4105", "菠萝派")]},
                ],
            },
        }
        legs = bc.discover_dual_legs(named, details)
        self.assertEqual(legs.source, "menu")
        self.assertEqual(legs.left, ["9900091001"])
        self.assertEqual(legs.right, ["9900091002"])
        paired = bc.expand_dual_1plus1(details, named, ["3050"], "0-0", budget_fen=5000)
        self.assertTrue(paired)
        self.assertTrue(all(product_codes(c) == ["9900091001", "9900091002"] for c in paired))

    def test_mcard_and_dabao_follow_category_and_keep_share_meals_out(self):
        menu = {
            "categories": [
                {
                    "name": "麦金卡优惠",
                    "meals": [
                        indexed("9900090001", "新主堡四件套", "29"),
                        indexed("9900090002", "麦金卡双人分享桶", "60"),
                        indexed("9900090003", "麦金卡早餐板烧", "16"),
                    ],
                },
                {
                    "name": "大堡口福",
                    "meals": [
                        indexed("9900080001", "巨无霸大堡口福", "32"),
                        indexed("9900080002", "大堡口福双人餐", "55"),
                    ],
                },
            ],
            "mealIndex": {
                "9900090001": {"name": "新主堡四件套", "currentPrice": "29"},
                "9900000884": {"name": "酥酥多笋卷四件套", "currentPrice": "28"},
                "9900080001": {"name": "巨无霸大堡口福", "currentPrice": "32"},
                "9900016670": {"name": "套餐A", "currentPrice": "30"},
            },
        }
        self.assertEqual(bc.discover_mcard_four_codes(menu, {}), ["9900090001"])
        self.assertEqual(bc.discover_dabao_codes(menu, {}), ["9900080001"])

        bare = {"mealIndex": {"9900000884": {"name": "酥酥多笋卷四件套", "currentPrice": "28"}}}
        self.assertEqual(bc.discover_mcard_four_codes(bare, {"9900000884": {}}), ["9900000884"])
        dabao_bare = {"mealIndex": {"9900016670": {"name": "套餐A", "currentPrice": "30"}}}
        self.assertEqual(bc.discover_dabao_codes(dabao_bare, {}), ["9900016670"])

    def test_classic_three_does_not_absorb_sui_xin_pei(self):
        menu = {
            "categories": [
                {
                    "name": "随心配1+1",
                    "meals": [indexed("9900016651", "随心配1+1三件套!!!", "21.90")],
                },
                {
                    "name": "精选单人餐",
                    "meals": [indexed("9900030001", "麦香鱼三件套", "27.00")],
                },
            ],
            "mealIndex": {
                "9900016651": {"name": "随心配1+1三件套!!!", "currentPrice": "21.90"},
                "9900030001": {"name": "麦香鱼三件套", "currentPrice": "27.00"},
            },
        }
        codes = bc.discover_classic_three_codes(menu, 5000, {})
        self.assertIn("9900030001", codes)
        self.assertNotIn("9900016651", codes)

    def test_existing_fixture_dual_set_is_unchanged(self):
        base = load_snapshot(EXAMPLES / "request-offline.json")
        menu = load_snapshot(EXAMPLES / "fixtures" / "menu.json")
        details = {
            path.stem: load_snapshot(path)
            for path in (EXAMPLES / "fixtures" / "meal-details").glob("*.json")
        }
        cands = bc.expand(
            details,
            menu,
            base["prefs"],
            families=base["candidateFamilies"],
            budget_fen=base["budgetFen"],
        )
        keys = {c["rotationKey"] for c in cands}
        self.assertEqual(
            keys,
            {
                "sj1120+zl505416_w4105_522021",
                "sj1120+zl505416_w4105_3050",
                "sj1120+zl505416_w4106_522021",
                "sj1120+zl505416_w4106_3050",
                "sj1120+zl1401_w4105_522021",
                "sj1120+zl1401_w4105_3050",
                "sj1120+zl1401_w4106_522021",
                "sj1120+zl1401_w4106_3050",
                "sj1450+zl505416_w4105_522021",
                "sj1450+zl505416_w4105_3050",
                "sj1450+zl505416_w4106_522021",
                "sj1450+zl505416_w4106_3050",
                "sj1450+zl1401_w4105_522021",
                "sj1450+zl1401_w4105_3050",
                "sj1450+zl1401_w4106_522021",
                "sj1450+zl1401_w4106_3050",
            },
        )
        self.assertTrue(all(product_codes(c) == ["9900016655", "9900016668"] for c in cands))


def _mcard_flood_detail(mains):
    return {
        "name": "麦金卡四件套",
        "rounds": [
            {"id": "1", "name": "主食", "choices": [choice(code, f"汉堡{code}") for code in mains]},
            {
                "id": "2",
                "name": "小食",
                "choices": [choice("4810", "中薯条"), choice("4437", "玉米杯")],
            },
            {
                "id": "3",
                "name": "饮料",
                "choices": [
                    choice("522021", "无糖可乐大杯"),
                    choice("3050", "雪碧中杯"),
                    choice("3060", "芬达中杯"),
                    choice("3110", "橙汁"),
                ],
            },
        ],
    }


class TrimDiversityTest(unittest.TestCase):
    def test_round_robin_keeps_each_dual_left_and_each_mcard_code(self):
        cands = []
        for i in range(30):
            cands.append(
                {
                    "rotationKey": f"mkA_{i}",
                    "listPriceFen": 2800,
                    "primaryMainCode": f"A{i}",
                    "tags": ["mcard_four", "mcard", "four_piece"],
                    "items": [{"productCode": "9900000884"}],
                }
            )
        for i in range(8):
            cands.append(
                {
                    "rotationKey": f"mkB_{i}",
                    "listPriceFen": 3200,
                    "primaryMainCode": f"B{i}",
                    "tags": ["mcard_four", "mcard", "four_piece"],
                    "items": [{"productCode": "9900090001"}],
                }
            )
        for i, main in enumerate(["1120", "1450"] * 10):
            cands.append(
                {
                    "rotationKey": f"sj_{i}",
                    "listPriceFen": 2800,
                    "primaryMainCode": main,
                    "tags": ["dual11"],
                    "items": [
                        {"productCode": "9900016655"},
                        {"productCode": "9900016668"},
                    ],
                }
            )
        for i, main in enumerate(["1120", "1450", "1100"] * 8):
            cands.append(
                {
                    "rotationKey": f"t_{i}",
                    "listPriceFen": 3690,
                    "primaryMainCode": main,
                    "tags": ["dual11"],
                    "items": [
                        {"productCode": "9900016651"},
                        {"productCode": "9900016668"},
                    ],
                }
            )
        trimmed = bc.trim_candidates(cands, 20, ["mcard_four", "dual_1plus1"])
        self.assertLessEqual(len(trimmed), 20)
        anchors = [product_codes(c)[0] for c in trimmed]

        def count(code):
            return sum(1 for anchor in anchors if anchor == code)

        self.assertGreaterEqual(count("9900016651"), 1)
        self.assertGreaterEqual(count("9900016655"), 1)
        self.assertGreaterEqual(count("9900000884"), 1)
        self.assertGreaterEqual(count("9900090001"), 1)

    def test_classic_unique_mains_stay_reserved(self):
        classics = []
        for main in ("1600", "1440", "1010"):
            classics.append(
                {
                    "rotationKey": f"ct_{main}",
                    "listPriceFen": 3000,
                    "primaryMainCode": main,
                    "tags": ["classic_three", "three_piece"],
                    "items": [{"productCode": f"ct-{main}"}],
                }
            )
        flood = [
            {
                "rotationKey": f"mk_{i}",
                "listPriceFen": 2800,
                "primaryMainCode": f"M{i}",
                "tags": ["mcard_four", "four_piece"],
                "items": [{"productCode": "9900000884"}],
            }
            for i in range(30)
        ]
        trimmed = bc.trim_candidates(classics + flood, 100, ["mcard_four", "classic_three"])
        kept = {c["primaryMainCode"] for c in trimmed if "classic_three" in c["tags"]}
        self.assertTrue({"1600", "1440", "1010"} <= kept)

    def test_cli_max_100_keeps_new_dual_left_beside_cheaper_sibling(self):
        mains_a = [f"A{i:02d}" for i in range(12)]
        mains_b = [f"B{i:02d}" for i in range(12)]
        menu = {
            "categories": [
                {
                    "name": "麦金卡",
                    "meals": [
                        indexed("9900000884", "酥酥多笋卷四件套", "28"),
                        indexed("9900090001", "新主堡四件套", "30"),
                    ],
                },
                {
                    "name": "随心配1+1",
                    "meals": [
                        indexed("9900016655", "双吉随心配1+1", "13.90"),
                        indexed("9900016668", "真有料随心配1+1", "14.90"),
                        indexed("9900016651", "随心配1+1三件套!!!", "21.90"),
                    ],
                },
            ],
            "mealIndex": {
                "9900000884": {"name": "酥酥多笋卷四件套", "currentPrice": "28"},
                "9900090001": {"name": "新主堡四件套", "currentPrice": "30"},
                "9900016655": {"name": "双吉随心配1+1", "currentPrice": "13.90"},
                "9900016668": {"name": "真有料随心配1+1", "currentPrice": "14.90"},
                "9900016651": {"name": "随心配1+1三件套!!!", "currentPrice": "21.90"},
            },
        }
        details = {
            "9900000884": _mcard_flood_detail(mains_a),
            "9900090001": _mcard_flood_detail(mains_b),
            "9900016655": dual_detail_6655(),
            "9900016668": dual_detail_6668(),
            "9900016651": dual_detail_6651(),
        }
        base = {
            "storeCode": "000000",
            "beType": 1,
            "orderType": 1,
            "budgetFen": 5000,
            "dining": "eat-in",
            "mealPeriod": "lunch",
            "prefs": {
                "priority": ["mcard_four", "dual_1plus1", "satiety"],
                "drink": {"preferCodes": ["522021"], "iceKey": "0-0"},
            },
            "candidateFamilies": ["mcard_four", "dual_1plus1"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            menu_path = root / "menu.json"
            details_dir = root / "details"
            details_dir.mkdir()
            built = root / "built.json"
            menu_path.write_text(json.dumps(menu, ensure_ascii=False), encoding="utf-8")
            (root / "base.json").write_text(json.dumps(base, ensure_ascii=False), encoding="utf-8")
            for code, detail in details.items():
                (details_dir / f"{code}.json").write_text(
                    json.dumps(detail, ensure_ascii=False),
                    encoding="utf-8",
                )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ENGINE / "build_candidates.py"),
                    "--base",
                    str(root / "base.json"),
                    "--menu",
                    str(menu_path),
                    "--details-dir",
                    str(details_dir),
                    "--max",
                    "100",
                    "-o",
                    str(built),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(built.read_text(encoding="utf-8"))
        self.assertIn("candidates", result.stdout)
        cands = payload["candidates"]
        self.assertEqual(len(cands), 100)
        self.assertTrue(any("9900016651" in product_codes(c) for c in cands))
        self.assertTrue(any("9900016655" in product_codes(c) for c in cands))
        self.assertGreaterEqual(sum(1 for c in cands if "9900016651" in product_codes(c)), 2)
        mcard_codes = {product_codes(c)[0] for c in cands if "mcard_four" in (c.get("tags") or [])}
        self.assertTrue({"9900000884", "9900090001"} <= mcard_codes)

    def test_swap_path_anchor_stays_on_left_leg_when_right_leg_leads(self):
        """右腿红区有真汉堡时，burger-swap 把真有料放在 items[0]，锚点仍是左腿。"""
        left_burger = "9900016655"
        left_plain = "9900091001"
        left_three = "9900016651"
        right = "9900016668"
        right_plain = "9900091002"
        menu = {
            "categories": [
                {
                    "name": "随心配1+1",
                    "meals": [
                        indexed(left_burger, "双吉随心配1+1", "13.90"),
                        indexed(left_plain, "双吉随心配1+1新品", "13.90"),
                        indexed(left_three, "随心配1+1三件套!!!", "21.90"),
                        indexed(right, "真有料随心配1+1", "14.90"),
                        indexed(right_plain, "真有料随心配1+1新品", "14.90"),
                    ],
                }
            ],
            "mealIndex": {
                left_burger: {"name": "双吉随心配1+1", "currentPrice": "13.90"},
                left_plain: {"name": "双吉随心配1+1新品", "currentPrice": "13.90"},
                left_three: {"name": "随心配1+1三件套!!!", "currentPrice": "21.90"},
                right: {"name": "真有料随心配1+1", "currentPrice": "14.90"},
                right_plain: {"name": "真有料随心配1+1新品", "currentPrice": "14.90"},
            },
        }
        burger_right = {
            "name": "真有料随心配1+1",
            "rounds": [
                {
                    "id": "1",
                    "name": "红区",
                    "choices": [choice("1100", "巨无霸"), choice("505416", "脆汁鸡琵琶腿")],
                },
                {
                    "id": "2",
                    "name": "白区",
                    "choices": [choice("522021", "无糖可乐大杯"), choice("4105", "菠萝派")],
                },
            ],
        }
        burger_left = {
            "name": "双吉随心配1+1",
            "rounds": [
                {
                    "id": "1",
                    "name": "红区",
                    "choices": [choice("1120", "双层吉士汉堡"), choice("1401", "麦乐鸡5块")],
                },
                {
                    "id": "2",
                    "name": "饮料",
                    "choices": [choice("522021", "无糖可乐大杯"), choice("4105", "菠萝派")],
                },
            ],
        }
        # 菜单名能认出左右腿，detail 不再带「双吉 / 真有料」，商品名不能当锚点。
        plain_right = {
            "rounds": [
                {
                    "id": "1",
                    "name": "红区",
                    "choices": [choice("1100", "巨无霸"), choice("505416", "脆汁鸡琵琶腿")],
                },
                {
                    "id": "2",
                    "name": "白区",
                    "choices": [choice("3050", "雪碧中杯"), choice("4106", "香芋派")],
                },
            ],
        }
        plain_left = {
            "rounds": [
                {
                    "id": "1",
                    "name": "红区",
                    "choices": [choice("1450", "麦香鸡"), choice("1401", "麦乐鸡5块")],
                },
                {
                    "id": "2",
                    "name": "饮料",
                    "choices": [choice("3050", "雪碧中杯"), choice("4106", "香芋派")],
                },
            ],
        }
        details = {
            left_burger: burger_left,
            left_plain: plain_left,
            left_three: dual_detail_6651(),
            right: burger_right,
            right_plain: plain_right,
        }
        cands = bc.expand_dual_1plus1(details, menu, ["522021", "3050"], "0-0", budget_fen=5000)

        def swapped(pool, right_code):
            return [c for c in pool if product_codes(c)[:1] == [right_code]]

        named_swap = swapped(
            [c for c in cands if left_burger in product_codes(c) and right in product_codes(c)],
            right,
        )
        plain_swap = swapped(
            [c for c in cands if left_plain in product_codes(c) and right_plain in product_codes(c)],
            right_plain,
        )
        self.assertTrue(named_swap)
        self.assertTrue(plain_swap)
        for cand in named_swap:
            self.assertEqual(product_codes(cand), [right, left_burger])
            self.assertEqual(bc._deal_anchor(cand), left_burger)
        for cand in plain_swap:
            self.assertEqual(product_codes(cand), [right_plain, left_plain])
            self.assertEqual(cand.get("dualLeftCode"), left_plain)
            self.assertEqual(bc._deal_anchor(cand), left_plain)
        lefts = {left_burger, left_plain, left_three}
        rights = {right, right_plain}
        for cand in cands:
            codes = product_codes(cand)
            anchor = bc._deal_anchor(cand)
            self.assertIn(anchor, lefts)
            self.assertIn(anchor, codes)
            self.assertNotIn(anchor, rights)
            if left_three in codes:
                self.assertEqual(codes[0], left_three)
                self.assertEqual(anchor, left_three)

    def test_max_splits_seats_by_left_leg_when_items_lead_with_right(self):
        """items 把真有料放前面时，--max 仍按两条左腿轮转，不合成一个右腿桶。"""
        right = "9900016668"
        left_a = "9900016655"
        left_b = "9900016651"
        left_c = "9900091001"
        cands = []
        for i in range(8):
            cands.append(
                {
                    "rotationKey": f"a{i:02d}",
                    "listPriceFen": 2800,
                    "primaryMainCode": f"A{i}",
                    "tags": ["dual11"],
                    "items": [
                        {"productCode": right, "productName": "真有料随心配1+1"},
                        {"productCode": left_a, "productName": "双吉随心配1+1"},
                    ],
                }
            )
            cands.append(
                {
                    "rotationKey": f"b{i:02d}",
                    "listPriceFen": 3690,
                    "primaryMainCode": f"B{i}",
                    "tags": ["dual11"],
                    "items": [
                        {"productCode": right, "productName": "真有料随心配1+1"},
                        {"productCode": left_b, "productName": "随心配1+1三件套!!!"},
                    ],
                }
            )
            cands.append(
                {
                    "rotationKey": f"c{i:02d}",
                    "listPriceFen": 3000,
                    "primaryMainCode": f"C{i}",
                    "tags": ["dual11"],
                    "dualLeftCode": left_c,
                    "items": [
                        {"productCode": right, "productName": right},
                        {"productCode": left_c, "productName": left_c},
                    ],
                }
            )
        for cand in cands:
            self.assertEqual(bc._deal_anchor(cand), product_codes(cand)[1])
            self.assertNotEqual(bc._deal_anchor(cand), right)

        trimmed = bc.trim_candidates(cands, 9, ["dual_1plus1"])
        anchors = [bc._deal_anchor(c) for c in trimmed]
        self.assertEqual(anchors.count(left_a), 3)
        self.assertEqual(anchors.count(left_b), 3)
        self.assertEqual(anchors.count(left_c), 3)
        self.assertNotIn(right, anchors)


if __name__ == "__main__":
    unittest.main()
