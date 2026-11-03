#!/usr/bin/env python3
"""Expand dual-1+1 / 大堡口福 candidates from meal-details (no MCP / no order).

Usage:
  python3 build_candidates.py \\
    --base runs/fullmenu-request.json \\
    --details-dir runs/meal-details \\
    --menu runs/query-meals-<store>-slim.json \\
    -o runs/fullmenu-request-diverse.json
"""
from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from mcp_contract import ContractError, load_snapshot, normalize_menu, rounds_of
from profile_lib import SCRIPT_DIR, apply_prefs_file, load_profile, resolve_profile_path

DRINK_RE = re.compile(r"(可乐|雪碧|芬达|汽水|橙汁|豆浆|咖啡|奶茶|柠檬水|饮|茶(?!叶)|麦炫酷)", re.I)
MAIN_RE = re.compile(r"(堡|汉堡|吉士|麦香鸡|板烧|巨无霸|双层|卷|鱼堡|安格斯|麦香鱼|鸡排堡|麦满分|炒双蛋|营养卷|火腿扒|猪柳)", re.I)
SIDE_RE = re.compile(r"(薯条|派|鸡翅|鸡腿|琵琶|麦乐鸡|鸡块|小食|玉米|沙拉|脆汁鸡|鸡排(?!堡)|新地|薯饼|油条|香肠)", re.I)

DRINK_CODES = {"522021", "3071", "522020", "3050", "3060", "3110"}
NON_DRINK_WHITE = {"4105", "4106", "4437", "4420", "4422"}
FORBIDDEN_AS_MAIN_CODES = {"1401"}  # 麦乐鸡 — never left-leg / primary main

MCARD_FOUR_CODES = [
    "9900004236",  # 人气超值四件套随心选 ¥28
    "9900000884",  # 酥酥多笋卷四件套 ¥28
    "9900016629",  # 海陆双全堡四件套 ¥30.9
    "9900010110",  # 鱼牛汉堡四件套随心选 ¥32
    "9900006957",  # 安格斯厚牛堡四件套随心选 ¥37（预算外由 scorer 淘汰）
]
DUAL11_LEFT = ["9900016655", "9900016651"]
DUAL11_RIGHT = "9900016668"
DABAO_IDS = ["9900016670", "9900016671", "9900016673", "9900016674"]
COFFEE_CODES_PREF = ["520688", "521327", "507411", "507368", "507409", "507328"]  # cheaper 鲜萃 first
COFFEE_NAME_RE = re.compile(r"(咖啡|拿铁|美式|卡布|奶铁|鲜萃冰咖|鲜萃咖啡)", re.I)

# classic burger/wrap/fish 三件套 / 精选单人餐（动态发现，勿在 Skill 写死 SKU）
CLASSIC_THREE_NAME_RE = re.compile(
    r"(三件套|精选单人|(?:汉堡|鸡腿堡|板烧|麦香鸡|麦香鱼|吉士|巨无霸|鳕鱼|安格斯|笋卷).{0,8}套餐|(?:套餐).{0,6}(?:汉堡|鸡腿堡|板烧|麦香鸡|麦香鱼))",
    re.I,
)
CLASSIC_THREE_EXCLUDE_RE = re.compile(
    r"(大堡口福|随心配|1\+1|四件套|麦金卡|开心乐园|麦乐鸡|鸡翅|双人|拼盘|八件|分享|BFF|周边|炸鸡架|亲子)",
    re.I,
)


def load(p: Path) -> Any:
    return json.loads(p.read_text(encoding="utf-8"))


def ice_mod(ice_key: str = "0-0", ice_code: str = "200002") -> Dict[str, Any]:
    return {"values": [{"code": ice_code, "key": ice_key, "quantity": 1}]}


def yuan_to_fen(v: Any) -> int:
    if v is None:
        return 0
    try:
        return int(round(float(v) * 100))
    except Exception:
        return 0


def red_white(rounds: List[Dict[str, Any]]):
    red, white = rounds[0], rounds[-1]
    for r in rounds:
        if "红" in r["name"] or "汉堡" in r["name"] or "主食" in r["name"]:
            red = r
        if "白" in r["name"] or "饮料" in r["name"]:
            white = r
    return red, white


def is_drink(ch: Dict[str, Any]) -> bool:
    return ch["code"] in DRINK_CODES or bool(DRINK_RE.search(ch["name"]))


def is_mainish(ch: Dict[str, Any]) -> bool:
    """True burger/wrap/fish only — 麦乐鸡/鸡块/纯炸鸡小食不算主食。"""
    code = ch["code"]
    name = ch.get("name") or ""
    if code in FORBIDDEN_AS_MAIN_CODES:
        return False
    if "麦乐鸡" in name or ("鸡块" in name and "堡" not in name):
        return False
    # pure fried-chicken sides in 真有料红区 — OK as right-leg protein, not left-leg main
    if code in {"505416", "521156"}:  # 琵琶腿 / 那么大鸡排
        return False
    if MAIN_RE.search(name):
        return True
    return code in {
        "1010",
        "1120",
        "1450",
        "1100",
        "1106",
        "522032",
        "521147",
        "515295",
        "522016",
        "1440",
        "1406",
        "1600",
        "4648",
        "520312",
        "8317",
        "511781",
        "511782",
    }


def is_sideish(ch: Dict[str, Any]) -> bool:
    if ch["code"] in FORBIDDEN_AS_MAIN_CODES:
        return True
    if ch["code"] in NON_DRINK_WHITE or SIDE_RE.search(ch["name"]):
        return True
    return ch["code"] in {"505416", "521156", "1401", "4800", "4810"}


def pick_drink(choices: List[Dict[str, Any]], prefer: List[str]) -> Optional[Dict[str, Any]]:
    for code in prefer:
        for ch in choices:
            if ch["code"] == code:
                return ch
    for ch in choices:
        if is_drink(ch) and "无糖" in ch["name"] and "可乐" in ch["name"]:
            return ch
    for ch in choices:
        if is_drink(ch):
            return ch
    return None


def combo_item(code: str, mod: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {"code": code, "quantity": 1}
    if mod:
        d["modification"] = mod
    return d


def build_dual(
    code_a: str,
    det_a: Dict[str, Any],
    main_a: Dict[str, Any],
    drink: Dict[str, Any],
    code_b: str,
    det_b: Dict[str, Any],
    main_b: Dict[str, Any],
    side: Dict[str, Any],
    price_fen: int,
    ice_key: str,
) -> Dict[str, Any]:
    ra, wa = red_white(rounds_of(det_a))
    rb, wb = red_white(rounds_of(det_b))
    rounds_a = rounds_of(det_a)
    rounds_b = rounds_of(det_b)
    mid_a = [r for r in rounds_a if r["id"] not in (ra["id"], wa["id"])]
    mid_b = [r for r in rounds_b if r["id"] not in (rb["id"], wb["id"])]

    def item(pcode, det, red, white, mid, red_ch, white_ch, with_ice: bool):
        rl = [{"round": red["id"], "comboItemList": [combo_item(red_ch["code"])]}]
        for m in mid:
            if m["choices"]:
                rl.append(
                    {
                        "round": m["id"],
                        "comboItemList": [combo_item(m["choices"][0]["code"])],
                    }
                )
        mod = ice_mod(ice_key) if with_ice else None
        rl.append(
            {"round": white["id"], "comboItemList": [combo_item(white_ch["code"], mod)]}
        )
        return {
            "productCode": pcode,
            "productName": str(det.get("name") or det.get("productName") or pcode),
            "quantity": 1,
            "roundList": rl,
        }

    item_a = item(code_a, det_a, ra, wa, mid_a, main_a, drink, True)
    item_b = item(code_b, det_b, rb, wb, mid_b, main_b, side, False)
    tag_left = "sj" if "双吉" in str(det_a.get("name")) else (
        "t" if "三件套" in str(det_a.get("name")) else "x"
    )
    rk = f"{tag_left}{main_a['code']}+zl{main_b['code']}_w{side['code']}"
    summary = (
        f"{item_a['productName']}（{main_a['name']}+{drink['name']}去冰）+ "
        f"{item_b['productName']}（{main_b['name']}+{side['name']}）≈¥{price_fen/100:.1f}"
    )
    return {
        "id": rk,
        "rotationKey": rk,
        "humanSummary": summary,
        "listPriceFen": price_fen,
        "priceVerified": False,
        "tags": ["dual11", "deal", "satiety", "built"],
        "primaryMainCode": main_a["code"],
        "items": [item_a, item_b],
    }


def build_dabao(
    code: str,
    det: Dict[str, Any],
    burger: Dict[str, Any],
    side: Dict[str, Any],
    drink: Dict[str, Any],
    price_fen: int,
    ice_key: str,
) -> Dict[str, Any]:
    rounds = rounds_of(det)
    r_burger, r_side, r_drink = rounds[0], rounds[1], rounds[2]
    items = [
        {
            "productCode": code,
            "productName": str(det.get("name") or code),
            "quantity": 1,
            "roundList": [
                {"round": r_burger["id"], "comboItemList": [combo_item(burger["code"])]},
                {"round": r_side["id"], "comboItemList": [combo_item(side["code"])]},
                {
                    "round": r_drink["id"],
                    "comboItemList": [combo_item(drink["code"], ice_mod(ice_key))],
                },
            ],
        }
    ]
    rk = f"db{code[-4:]}_{burger['code']}_{side['code']}_{drink['code']}"
    summary = (
        f"{det.get('name')}（{burger['name']}+{side['name']}+{drink['name']}去冰）≈¥{price_fen/100:.1f}"
    )
    return {
        "id": rk,
        "rotationKey": rk,
        "humanSummary": summary,
        "listPriceFen": price_fen,
        "priceVerified": False,
        "tags": ["dabao", "deal", "satiety", "built"],
        "primaryMainCode": burger["code"],
        "items": items,
    }



def build_classic_three(
    code: str,
    det: Dict[str, Any],
    burger: Dict[str, Any],
    side: Dict[str, Any],
    drink: Dict[str, Any],
    price_fen: int,
    ice_key: str,
) -> Dict[str, Any]:
    rounds = rounds_of(det)
    if len(rounds) < 3:
        raise ValueError(f"classic_three needs >=3 rounds: {code}")
    # map by role rather than assuming fixed order
    r_burger, r_side, r_drink = rounds[0], rounds[1], rounds[2]
    for r in rounds:
        n = r.get("name") or ""
        if any(k in n for k in ("饮料", "饮品")) or (r["choices"] and all(is_drink(c) for c in r["choices"][:3])):
            r_drink = r
        elif any(k in n for k in ("小食", "薯条", "配菜")) or (
            r["choices"] and any(is_sideish(c) for c in r["choices"][:3])
        ):
            if r is not r_burger:
                r_side = r
        elif any(k in n for k in ("汉堡", "主食", "堡", "卷", "鱼")) or (
            r["choices"] and any(is_mainish(c) for c in r["choices"][:3])
        ):
            r_burger = r
    items = [
        {
            "productCode": code,
            "productName": str(det.get("name") or det.get("productName") or code),
            "quantity": 1,
            "roundList": [
                {"round": r_burger["id"], "comboItemList": [combo_item(burger["code"])]},
                {"round": r_side["id"], "comboItemList": [combo_item(side["code"])]},
                {
                    "round": r_drink["id"],
                    "comboItemList": [combo_item(drink["code"], ice_mod(ice_key))],
                },
            ],
        }
    ]
    rk = f"ct{code[-4:]}_{burger['code']}_{side['code']}_{drink['code']}"
    summary = (
        f"{det.get('name')}（{burger['name']}+{side['name']}+{drink['name']}去冰）≈¥{price_fen/100:.1f}"
    )
    return {
        "id": rk,
        "rotationKey": rk,
        "humanSummary": summary,
        "listPriceFen": price_fen,
        "priceVerified": False,
        "tags": ["classic_three", "three_piece", "deal", "satiety", "built"],
        "primaryMainCode": burger["code"],
        "items": items,
    }


def price_for(code: str, menu: Dict[str, Any], diff_extra: int = 0) -> int:
    idx = (menu or {}).get("mealIndex") or {}
    meta = idx.get(code) or {}
    if "currentPrice" not in meta:
        raise ContractError(f"menu.mealIndex.{code}.currentPrice: required for candidate pricing")
    base = yuan_to_fen(meta["currentPrice"])
    return base + diff_extra



def build_mcard_four(
    code: str,
    det: Dict[str, Any],
    burger: Dict[str, Any],
    side_a: Dict[str, Any],
    side_b: Dict[str, Any],
    drink: Dict[str, Any],
    price_fen: int,
    ice_key: str,
) -> Dict[str, Any]:
    rounds = rounds_of(det)
    if len(rounds) < 4:
        raise ValueError(f"mcard four needs 4 rounds: {code}")
    r0, r1, r2, r3 = rounds[0], rounds[1], rounds[2], rounds[3]
    items = [
        {
            "productCode": code,
            "productName": str(det.get("name") or det.get("productName") or code),
            "quantity": 1,
            "roundList": [
                {"round": r0["id"], "comboItemList": [combo_item(burger["code"])]},
                {"round": r1["id"], "comboItemList": [combo_item(side_a["code"])]},
                {"round": r2["id"], "comboItemList": [combo_item(side_b["code"])]},
                {
                    "round": r3["id"],
                    "comboItemList": [combo_item(drink["code"], ice_mod(ice_key))],
                },
            ],
        }
    ]
    rk = f"mk{code[-4:]}_{burger['code']}_{side_a['code']}_{side_b['code']}_{drink['code']}"
    summary = (
        f"{det.get('name')}（{burger['name']}+{side_a['name']}+{side_b['name']}+"
        f"{drink['name']}去冰）≈¥{price_fen/100:.1f}"
    )
    return {
        "id": rk,
        "rotationKey": rk,
        "humanSummary": summary,
        "listPriceFen": price_fen,
        "priceVerified": False,
        "tags": ["mcard_four", "mcard", "four_piece", "deal", "satiety", "built"],
        "primaryMainCode": burger["code"],
        "mcardFour": True,
        "items": items,
    }


def is_coffee_choice(ch: Dict[str, Any]) -> bool:
    return ch["code"] in set(COFFEE_CODES_PREF) or bool(COFFEE_NAME_RE.search(ch.get("name") or ""))


def pick_coffee(choices: List[Dict[str, Any]], prefer: List[str]) -> Optional[Dict[str, Any]]:
    coffees = [ch for ch in choices if is_coffee_choice(ch)]
    if not coffees:
        return None
    # prefer listed codes, then lowest diffFen (鲜萃 often +¥1.5 vs 美式 +¥9)
    def rank(ch: Dict[str, Any]):
        try:
            pref_i = prefer.index(ch["code"])
        except ValueError:
            pref_i = 999
        return (pref_i, ch.get("diffFen") or 0, ch["code"])
    coffees.sort(key=rank)
    return coffees[0]


def pick_coffee_budgeted(
    choices: List[Dict[str, Any]],
    prefer: List[str],
    base_fen: int,
    extra_fen: int = 0,
    budget_fen: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Coffee that fits remaining budget; prefer list order, then lowest diff."""
    coffees = [ch for ch in choices if is_coffee_choice(ch)]
    if not coffees:
        return None
    if budget_fen is not None:
        coffees = [
            ch
            for ch in coffees
            if int(base_fen) + int(extra_fen) + int(ch.get("diffFen") or 0) <= int(budget_fen)
        ]
        if not coffees:
            return None
    def rank(ch: Dict[str, Any]):
        try:
            pref_i = prefer.index(ch["code"])
        except ValueError:
            pref_i = 999
        return (int(ch.get("diffFen") or 0), pref_i, ch["code"])
    coffees.sort(key=rank)
    # among zero-diff, honor prefer order
    zero = [c for c in coffees if int(c.get("diffFen") or 0) == 0]
    pool = zero or coffees
    for code in prefer:
        for ch in pool:
            if ch["code"] == code:
                return ch
    return pool[0]


def resolve_families(req: Dict[str, Any]) -> List[str]:
    """Families from explicit candidateFamilies, else infer from prefs.priority."""
    explicit = req.get("candidateFamilies")
    if isinstance(explicit, list) and explicit:
        return [str(x) for x in explicit]
    pri = [str(x) for x in ((req.get("prefs") or {}).get("priority") or [])]
    mapping = {
        "mcard_four": "mcard_four",
        "dual_1plus1": "dual_1plus1",
        "classic_three": "classic_three",
        "coffee": "coffee_combo",
        "deal": None,  # deal alone does not force a family
    }
    out: List[str] = []
    for p in pri:
        fam = mapping.get(p)
        if fam and fam not in out:
            out.append(fam)
    # sensible defaults if still empty
    if not out:
        out = ["dual_1plus1", "mcard_four", "dabao"]
    # dabao as filler when dual/mcard present and not excluded
    if "dual_1plus1" in out and "dabao" not in out:
        out.append("dabao")
    return out


def mcard_four_codes(menu: Dict[str, Any], details: Dict[str, Dict[str, Any]]) -> List[str]:
    """Union of slim-menu mcard codes + known defaults; cheapest first, then known order."""
    from_menu = [str(c) for c in ((menu or {}).get("mcardFourProductCodes") or [])]
    known = list(MCARD_FOUR_CODES)
    seen = set()
    merged: List[str] = []
    for c in known + from_menu:
        if c in seen:
            continue
        if c not in details and c not in set(from_menu) and c not in set(known):
            continue
        # keep if we have detail OR it is listed (detail may arrive later)
        if c in details or c in from_menu or c in set(known):
            seen.add(c)
            merged.append(c)
    idx = (menu or {}).get("mealIndex") or {}

    def price_key(code: str) -> Tuple[float, int, str]:
        try:
            p = float((idx.get(code) or {}).get("currentPrice") or 999)
        except Exception:
            p = 999.0
        try:
            known_i = known.index(code)
        except ValueError:
            known_i = 99
        return (p, known_i, code)

    # only emit codes we can expand now
    return sorted([c for c in merged if c in details], key=price_key)


def expand_mcard_four(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer_drinks: List[str],
    ice_key: str,
    prefer_coffee: bool,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ordered = mcard_four_codes(menu, details) or [c for c in MCARD_FOUR_CODES if c in details]
    for code in ordered:
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 4:
            continue
        burgers = rounds[0]["choices"]
        sides_a = rounds[1]["choices"]
        sides_b = rounds[2]["choices"]
        drinks = rounds[3]["choices"]
        if prefer_coffee:
            drink = pick_coffee(drinks, prefer_drinks) or pick_drink(drinks, prefer_drinks)
        else:
            drink = pick_drink(drinks, prefer_drinks)
        side_a = next((c for c in sides_a if c["code"] in {"4810", "4800", "4437"}), sides_a[0] if sides_a else None)
        side_b = next((c for c in sides_b if c["code"] in {"515106", "1401", "505416", "1700"}), sides_b[0] if sides_b else None)
        if not drink or not side_a or not side_b:
            continue
        base = price_for(code, menu)
        for burger in burgers:
            price = base + burger["diffFen"] + side_a["diffFen"] + side_b["diffFen"] + drink["diffFen"]
            tags_drink = "coffee" if is_coffee_choice(drink) else "drink"
            cand = build_mcard_four(code, det, burger, side_a, side_b, drink, price, ice_key)
            if tags_drink == "coffee" and "coffee" not in cand["tags"]:
                cand["tags"].append("coffee")
            out.append(cand)
    return out



BREAKFAST_COMBO_NAME_RE = re.compile(
    r"(麦满分|早安|炒双蛋|营养卷|一早现磨|鲜萃有堡|火腿扒堡|二件套|单人餐|三件套|美式组合)",
    re.I,
)
BREAKFAST_COMBO_EXCLUDE_RE = re.compile(r"(双人|三人|分享|周边|量贩(?!)|派对)", re.I)


def discover_breakfast_combo_codes(menu: Dict[str, Any], budget_fen: Optional[int] = None) -> List[str]:
    """Product codes for real breakfast combos on a reserved-delivery menu."""
    idx = (menu or {}).get("mealIndex") or {}
    out: List[str] = []
    seen = set()
    for code, meta in idx.items():
        name = str(meta.get("name") or "")
        if not BREAKFAST_COMBO_NAME_RE.search(name):
            continue
        if BREAKFAST_COMBO_EXCLUDE_RE.search(name):
            continue
        pt = meta.get("productType")
        if pt not in (2, 3, 7, None):
            # still allow unknown
            if pt == 1 and "套" not in name and "组合" not in name:
                continue
        price = yuan_to_fen(meta.get("currentPrice"))
        if budget_fen is not None and price > int(budget_fen):
            continue
        if code in seen:
            continue
        seen.add(code)
        out.append(str(code))
    # stable: cheaper first
    out.sort(key=lambda c: (yuan_to_fen((idx.get(c) or {}).get("currentPrice")), c))
    return out


def _role_rounds(rounds: List[Dict[str, Any]]) -> Dict[str, Optional[Dict[str, Any]]]:
    """Best-effort main/side/drink round mapping for breakfast dynamic combos."""
    roles: Dict[str, Optional[Dict[str, Any]]] = {"main": None, "side": None, "drink": None}
    if not rounds:
        return roles
    for r in rounds:
        n = r.get("name") or ""
        ch = r.get("choices") or []
        if any(k in n for k in ("咖啡", "饮料", "饮品")) or (ch and all(is_drink(c) or is_coffee_choice(c) for c in ch[: min(3, len(ch))])):
            roles["drink"] = r
        elif any(k in n for k in ("小食", "薯条", "配菜", "薯饼", "油条")) or (
            ch and any(is_sideish(c) for c in ch[: min(3, len(ch))]) and not any(is_mainish(c) for c in ch[: min(3, len(ch))])
        ):
            roles["side"] = r
        elif any(k in n for k in ("主食", "麦满分", "堡", "卷", "早餐")) or (
            ch and any(is_mainish(c) for c in ch[: min(3, len(ch))])
        ):
            roles["main"] = r
    # fallbacks by position
    if roles["main"] is None:
        roles["main"] = rounds[0]
    if roles["drink"] is None:
        roles["drink"] = rounds[-1] if len(rounds) >= 2 else None
    if roles["side"] is None and len(rounds) >= 3:
        for r in rounds[1:-1]:
            if r is not roles["main"] and r is not roles["drink"]:
                roles["side"] = r
                break
    return roles


def build_breakfast_coffee(
    code: str,
    det: Dict[str, Any],
    main: Dict[str, Any],
    drink: Dict[str, Any],
    price_fen: int,
    ice_key: str,
    side: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rounds = rounds_of(det)
    roles = _role_rounds(rounds)
    r_main = roles["main"] or rounds[0]
    r_drink = roles["drink"] or rounds[-1]
    r_side = roles["side"]
    round_list: List[Dict[str, Any]] = [
        {"round": r_main["id"], "comboItemList": [combo_item(main["code"])]},
    ]
    if side and r_side:
        round_list.append({"round": r_side["id"], "comboItemList": [combo_item(side["code"])]})
    # ice only when drink looks cold / has 冰 in name; still pass ice_key (去冰) for iced coffee
    drink_name = drink.get("name") or ""
    use_ice = ("冰" in drink_name) or ("冰" in (det.get("name") or ""))
    mod = ice_mod(ice_key) if use_ice else None
    # many McCafé choices still expose 去冰 keys even on 热 — prefer applying when ice_key set and iced
    if use_ice:
        round_list.append({"round": r_drink["id"], "comboItemList": [combo_item(drink["code"], mod)]})
    else:
        round_list.append({"round": r_drink["id"], "comboItemList": [combo_item(drink["code"])]})
    side_bit = f"+{side['name']}" if side else ""
    ice_bit = "去冰" if use_ice else ""
    rk = f"bf{code[-4:]}_{main['code']}_{side['code'] if side else 'noside'}_{drink['code']}"
    summary = (
        f"{det.get('name')}（{main['name']}{side_bit}+{drink['name']}{ice_bit}）≈¥{price_fen/100:.1f}"
    )
    tags = ["coffee", "coffee_combo", "breakfast", "deal", "satiety", "built"]
    if side:
        tags.append("three_piece")
    else:
        tags.append("two_piece")
    return {
        "id": rk,
        "rotationKey": rk,
        "humanSummary": summary,
        "listPriceFen": price_fen,
        "priceVerified": False,
        "tags": tags,
        "primaryMainCode": main["code"],
        "primarySideCode": side["code"] if side else "",
        "items": [
            {
                "productCode": code,
                "productName": str(det.get("name") or det.get("productName") or code),
                "quantity": 1,
                "roundList": round_list,
            }
        ],
    }


def expand_breakfast_coffee_combos(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer_drinks: List[str],
    ice_key: str,
    budget_fen: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Enumerate reserved-breakfast combos (麦满分/二件套/三件套) with coffee drink."""
    out: List[Dict[str, Any]] = []
    codes = discover_breakfast_combo_codes(menu, budget_fen=budget_fen)
    # also include any breakfast-looking details already pulled even if over menu index miss
    for code, det in details.items():
        name = str(det.get("name") or "")
        if code in codes:
            continue
        if BREAKFAST_COMBO_NAME_RE.search(name) and not BREAKFAST_COMBO_EXCLUDE_RE.search(name):
            price = price_for(code, menu)
            if budget_fen is not None and price > int(budget_fen):
                continue
            codes.append(code)
    for code in codes:
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 2:
            continue
        roles = _role_rounds(rounds)
        r_main = roles["main"]
        r_drink = roles["drink"]
        r_side = roles["side"]
        if not r_main or not r_drink:
            continue
        if not any(is_coffee_choice(c) for c in r_drink["choices"]):
            continue
        mains = [c for c in r_main["choices"] if is_mainish(c)] or list(r_main["choices"])
        sides: List[Optional[Dict[str, Any]]]
        if r_side and r_side["choices"]:
            sides = [c for c in r_side["choices"] if is_sideish(c) or not is_drink(c)] or list(r_side["choices"])
            sides = sides[:3]
        else:
            sides = [None]
        base = price_for(code, menu)
        for main in mains[:6]:
            for side in sides:
                extra = int(main["diffFen"]) + (int(side["diffFen"]) if side else 0)
                drink = pick_coffee_budgeted(
                    r_drink["choices"], prefer_drinks, base, extra_fen=extra, budget_fen=budget_fen
                )
                if not drink:
                    continue
                price = base + main["diffFen"] + drink["diffFen"] + (side["diffFen"] if side else 0)
                if budget_fen is not None and price > int(budget_fen):
                    continue
                out.append(
                    build_breakfast_coffee(code, det, main, drink, price, ice_key, side=side)
                )
    return out


def expand_coffee_combo(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer_drinks: List[str],
    ice_key: str,
    budget_fen: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Build filling combos whose drink slot is coffee.

    Sources: (1) reserved breakfast 麦满分/二件套/三件套 on the live menu;
    (2) lunch-style mcard / dabao / dual white if those details are present.
    """
    out: List[Dict[str, Any]] = []
    # Real breakfast menu (预约麦乐送 07:00) — primary for breakfast profile
    out.extend(
        expand_breakfast_coffee_combos(details, menu, prefer_drinks, ice_key, budget_fen=budget_fen)
    )
    # Prefer mcard fours with coffee drink (often empty on breakfast menu)
    out.extend(expand_mcard_four(details, menu, prefer_drinks, ice_key, prefer_coffee=True))
    # dabao with coffee
    for code in DABAO_IDS:
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 3:
            continue
        burgers = rounds[0]["choices"]
        sides = rounds[1]["choices"]
        drinks = rounds[2]["choices"]
        drink = pick_coffee(drinks, prefer_drinks)
        side = next((c for c in sides if c["code"] in {"4810", "4800"}), sides[0] if sides else None)
        if not drink or not side:
            continue
        base = price_for(code, menu)
        for burger in burgers:
            price = base + burger["diffFen"] + side["diffFen"] + drink["diffFen"]
            cand = build_dabao(code, det, burger, side, drink, price, ice_key)
            cand["tags"] = list(dict.fromkeys((cand.get("tags") or []) + ["coffee", "coffee_combo"]))
            cand["rotationKey"] = f"cf{cand['rotationKey']}"
            cand["id"] = cand["rotationKey"]
            out.append(cand)
    # dual 1+1 with coffee on drink leg (if available in white zone)
    zl = details.get(DUAL11_RIGHT)
    if zl:
        for left_code in DUAL11_LEFT:
            det = details.get(left_code)
            if not det:
                continue
            red, white = red_white(rounds_of(det))
            coffee = pick_coffee(white["choices"], prefer_drinks)
            if not coffee:
                continue
            zl_red, zl_white = red_white(rounds_of(zl))
            sides = [c for c in zl_white["choices"] if not is_drink(c)]
            sides = sorted(sides, key=lambda c: (0 if c["code"] in NON_DRINK_WHITE else 1, c["code"]))[:3]
            right_mains = [c for c in zl_red["choices"] if is_sideish(c) or not is_mainish(c)][:4] or list(zl_red["choices"])[:4]
            left_mains = [c for c in red["choices"] if is_mainish(c)] or list(red["choices"])
            base = price_for(left_code, menu) + price_for(DUAL11_RIGHT, menu)
            for main_a in left_mains[:4]:
                for main_b in right_mains:
                    if main_a["code"] == main_b["code"]:
                        continue
                    for side in sides[:2]:
                        price = base + main_a["diffFen"] + main_b["diffFen"] + side["diffFen"]
                        cand = build_dual(
                            left_code, det, main_a, coffee, DUAL11_RIGHT, zl, main_b, side, price, ice_key
                        )
                        cand["tags"] = list(dict.fromkeys((cand.get("tags") or []) + ["coffee", "coffee_combo"]))
                        cand["rotationKey"] = f"cf{cand['rotationKey']}"
                        cand["id"] = cand["rotationKey"]
                        out.append(cand)
    return out



def expand_dual_1plus1(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer: List[str],
    ice_key: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    zl = details.get(DUAL11_RIGHT)
    if not zl:
        return out
    zl_red, zl_white = red_white(rounds_of(zl))
    zl_sides = [c for c in zl_white["choices"] if not is_drink(c)]
    zl_sides_sorted = sorted(
        zl_sides,
        key=lambda c: (0 if c["code"] in NON_DRINK_WHITE else 1, c["code"]),
    )[:5]
    zl_mains_sideish = [c for c in zl_red["choices"] if is_sideish(c) or not is_mainish(c)]
    zl_mains_all = list(zl_red["choices"])
    right_mains = []
    for pool in (zl_mains_sideish, zl_mains_all):
        for c in pool:
            if c["code"] not in {x["code"] for x in right_mains}:
                right_mains.append(c)
    # 真有料右腿：优先琵琶腿/鸡排等；麦乐鸡垫底（可作配，不当首选蛋白）
    right_mains.sort(
        key=lambda c: (
            2 if c["code"] in FORBIDDEN_AS_MAIN_CODES or "麦乐鸡" in (c.get("name") or "") else 0,
            0 if c["code"] in {"505416", "521156"} else 1,
            c.get("diffFen") or 0,
            c["code"],
        )
    )
    right_mains = right_mains[:6]

    for left_code in DUAL11_LEFT:
        det = details.get(left_code)
        if not det:
            continue
        red, white = red_white(rounds_of(det))
        drink = pick_drink(white["choices"], prefer)
        if not drink:
            continue
        # 左腿红区只要真汉堡/卷；禁止麦乐鸡/琵琶腿/鸡排当主堡
        left_mains = [c for c in red["choices"] if is_mainish(c)]
        if not left_mains:
            # last resort: still skip nuggets
            left_mains = [
                c for c in red["choices"]
                if c["code"] not in FORBIDDEN_AS_MAIN_CODES
                and not is_sideish(c)
            ]
        if not left_mains:
            continue
        base = price_for(left_code, menu) + price_for(DUAL11_RIGHT, menu)
        for main_a in left_mains:
            for main_b in right_mains:
                if main_a["code"] == main_b["code"]:
                    continue
                if not (is_mainish(main_a) or is_mainish(main_b)):
                    continue
                for side in zl_sides_sorted[:3]:
                    price = base + main_a["diffFen"] + main_b["diffFen"] + side["diffFen"]
                    cand = build_dual(
                        left_code, det, main_a, drink, DUAL11_RIGHT, zl, main_b, side, price, ice_key
                    )
                    if cand["rotationKey"] in seen:
                        continue
                    seen.add(cand["rotationKey"])
                    out.append(cand)

    sj = details.get("9900016655")
    if zl and sj:
        zl_drink = pick_drink(zl_white["choices"], prefer)
        sj_red, sj_white = red_white(rounds_of(sj))
        sj_sides = [c for c in sj_white["choices"] if not is_drink(c)]
        sj_sides = sorted(sj_sides, key=lambda c: (0 if c["code"] in NON_DRINK_WHITE else 1, c["code"]))[:3]
        left_mains = [c for c in zl_red["choices"] if is_mainish(c)]
        right_mains = [c for c in sj_red["choices"] if is_sideish(c)] or [
            c for c in sj_red["choices"] if c["code"] != "1120"
        ]
        base = price_for(DUAL11_RIGHT, menu) + price_for("9900016655", menu)
        for main_a in left_mains[:5]:
            for main_b in right_mains[:5]:
                if main_a["code"] == main_b["code"]:
                    continue
                for side in sj_sides[:2]:
                    if not zl_drink:
                        continue
                    price = base + main_a["diffFen"] + main_b["diffFen"]
                    cand = build_dual(
                        DUAL11_RIGHT, zl, main_a, zl_drink, "9900016655", sj, main_b, side, price, ice_key
                    )
                    cand["rotationKey"] = f"zl{main_a['code']}+sj{main_b['code']}_w{side['code']}"
                    cand["id"] = cand["rotationKey"]
                    cand["primaryMainCode"] = main_a["code"]
                    if cand["rotationKey"] in seen:
                        continue
                    seen.add(cand["rotationKey"])
                    out.append(cand)
    return out


def expand_dabao(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer: List[str],
    ice_key: str,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    for code in DABAO_IDS:
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 3:
            continue
        burgers = rounds[0]["choices"]
        sides = rounds[1]["choices"]
        drinks = rounds[2]["choices"]
        drink = pick_drink(drinks, prefer)
        side = next((c for c in sides if c["code"] in {"4810", "4800"}), sides[0] if sides else None)
        if not drink or not side:
            continue
        base = price_for(code, menu)
        for burger in burgers:
            price = base + burger["diffFen"] + side["diffFen"] + drink["diffFen"]
            cand = build_dabao(code, det, burger, side, drink, price, ice_key)
            if cand["rotationKey"] in seen:
                continue
            seen.add(cand["rotationKey"])
            out.append(cand)
    return out


def discover_classic_three_codes(
    menu: Dict[str, Any],
    budget_fen: int,
    details: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """Dynamically find ≤budget classic 三件套 / 精选单人餐 / burger套餐 from slim menu.

    Excludes 大堡口福 / 双1+1 / 麦金卡四件套 / nugget-wing / multi-share meals.
    Prefers meals whose name covers underrepresented mains (e.g. 麦香鱼) and
    products that already have cached meal-details.
    """
    idx = (menu or {}).get("mealIndex") or {}
    # also harvest from categories (精选单人餐 etc.)
    cat_codes: List[str] = []
    for cat in (menu or {}).get("categories") or []:
        cname = str(cat.get("name") or cat.get("categoryName") or "")
        for m in cat.get("meals") or cat.get("products") or []:
            code = str(m.get("code") or m.get("productCode") or "")
            if not code:
                continue
            if "精选单人" in cname or CLASSIC_THREE_NAME_RE.search(str(m.get("name") or "")):
                cat_codes.append(code)

    known_other = set(MCARD_FOUR_CODES) | set(DUAL11_LEFT) | {DUAL11_RIGHT} | set(DABAO_IDS)
    known_other |= set((menu or {}).get("mcardFourProductCodes") or [])

    scored: List[Tuple[int, float, int, str]] = []  # prefer_fish, price, has_detail, code
    seen = set()
    for code in list(idx.keys()) + cat_codes:
        code = str(code)
        if code in seen or code in known_other:
            continue
        meta = idx.get(code) or {}
        name = str(meta.get("name") or meta.get("productName") or "")
        # category-only hits may lack mealIndex name — scan categories
        if not name:
            for cat in (menu or {}).get("categories") or []:
                for m in cat.get("meals") or cat.get("products") or []:
                    if str(m.get("code") or m.get("productCode") or "") == code:
                        name = str(m.get("name") or m.get("productName") or "")
                        if not meta:
                            meta = m
                        break
        if not name:
            continue
        if CLASSIC_THREE_EXCLUDE_RE.search(name):
            continue
        if not (CLASSIC_THREE_NAME_RE.search(name) or "精选单人" in name):
            continue
        try:
            price = float(meta.get("currentPrice") or 999)
        except Exception:
            price = 999.0
        price_fen = int(round(price * 100))
        if budget_fen > 0 and price_fen > budget_fen:
            continue
        # skip pure side/drink combos by name
        if re.search(r"^(麦乐鸡|麦辣鸡翅)", name):
            continue
        prefer = 0
        if "麦香鱼" in name or "鳕鱼" in name:
            prefer -= 2  # higher priority (lower sort key)
        if "精选单人" in name:
            prefer -= 1
        has_det = 0 if (details and code in details) else 1
        seen.add(code)
        scored.append((prefer, price, has_det, code))

    scored.sort()
    return [c for _, _, _, c in scored]


def expand_classic_three(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer: List[str],
    ice_key: str,
    budget_fen: int,
) -> List[Dict[str, Any]]:
    """Expand discovered classic three-piece meals; only keep listPrice ≤ budget."""
    out: List[Dict[str, Any]] = []
    seen = set()
    codes = discover_classic_three_codes(menu, budget_fen, details)
    # also include any already-cached detail that looks classic even if menu filter missed
    for code, det in details.items():
        name = str(det.get("name") or "")
        if code in codes:
            continue
        if CLASSIC_THREE_EXCLUDE_RE.search(name):
            continue
        if CLASSIC_THREE_NAME_RE.search(name) or "精选单人" in name:
            base = price_for(code, menu)
            if budget_fen > 0 and base > budget_fen:
                continue
            codes.append(code)

    for code in codes:
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 3:
            continue
        # classify rounds
        r_burger, r_side, r_drink = rounds[0], rounds[1], rounds[2]
        for r in rounds:
            n = r.get("name") or ""
            chs = r.get("choices") or []
            if any(k in n for k in ("饮料", "饮品")) or (chs and sum(1 for c in chs if is_drink(c)) >= max(1, len(chs) // 2)):
                r_drink = r
            elif any(k in n for k in ("小食", "薯条", "配菜", "玉米")) or (
                chs and any(is_sideish(c) for c in chs)
            ):
                if r["id"] != r_burger["id"]:
                    r_side = r
            elif any(k in n for k in ("汉堡", "主食", "堡", "卷", "鱼", "吉士")) or (
                chs and any(is_mainish(c) for c in chs)
            ):
                r_burger = r

        burgers = [c for c in r_burger["choices"] if is_mainish(c)]
        if not burgers:
            burgers = [
                c
                for c in r_burger["choices"]
                if c["code"] not in FORBIDDEN_AS_MAIN_CODES and not is_drink(c)
            ]
        # drop nugget-only
        burgers = [
            c
            for c in burgers
            if c["code"] not in FORBIDDEN_AS_MAIN_CODES
            and "麦乐鸡" not in (c.get("name") or "")
        ]
        if not burgers:
            continue
        sides = list(r_side["choices"] or [])
        drinks = list(r_drink["choices"] or [])
        drink = pick_drink(drinks, prefer)
        side = next(
            (c for c in sides if c["code"] in {"4810", "4800", "4437", "514782"}),
            sides[0] if sides else None,
        )
        if not drink or not side:
            continue
        base = price_for(code, menu)
        for burger in burgers:
            price = base + burger["diffFen"] + side["diffFen"] + drink["diffFen"]
            if budget_fen > 0 and price > budget_fen:
                continue
            cand = build_classic_three(code, det, burger, side, drink, price, ice_key)
            if cand["rotationKey"] in seen:
                continue
            seen.add(cand["rotationKey"])
            out.append(cand)
    return out



def expand(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefs: Dict[str, Any],
    families: Optional[Sequence[str]] = None,
    budget_fen: int = 3500,
) -> List[Dict[str, Any]]:
    normalize_menu(menu)
    drink_prefs = (prefs or {}).get("drink") or {}
    prefer = [str(x) for x in (drink_prefs.get("preferCodes") or ["522021", "3071"])]
    ice_key = str(drink_prefs.get("iceKey") or "0-0")
    coffee_prefer = [str(x) for x in ((prefs or {}).get("coffeePreferCodes") or prefer)]

    fams = [str(x) for x in (families or [])]
    if not fams:
        fams = resolve_families({"prefs": prefs or {}, "candidateFamilies": None})

    out: List[Dict[str, Any]] = []
    seen = set()

    def add_all(cands: List[Dict[str, Any]]):
        for c in cands:
            rk = c.get("rotationKey")
            if not rk or rk in seen:
                continue
            seen.add(rk)
            out.append(c)

    if "coffee_combo" in fams:
        add_all(expand_coffee_combo(details, menu, coffee_prefer, ice_key, budget_fen=budget_fen))
    if "mcard_four" in fams:
        prefer_coffee = "coffee" in ((prefs or {}).get("priority") or []) or bool((prefs or {}).get("requireCoffee"))
        add_all(expand_mcard_four(details, menu, coffee_prefer if prefer_coffee else prefer, ice_key, prefer_coffee))
    if "dual_1plus1" in fams:
        add_all(expand_dual_1plus1(details, menu, prefer, ice_key))
    if "dabao" in fams:
        add_all(expand_dabao(details, menu, prefer, ice_key))
    if "classic_three" in fams:
        add_all(expand_classic_three(details, menu, prefer, ice_key, int(budget_fen or 0)))

    # 新品 tags：MCP 无 isNew，用菜单 categories[].meals[].tags（上新/全新…）+ prefs
    tag_new_candidates(out, menu, prefs or {})
    return out


def tag_new_candidates(cands: List[Dict[str, Any]], menu: Dict[str, Any], prefs: Dict[str, Any]) -> None:
    import re
    rx_raw = prefs.get("newTagRegex") or r"(上新|全新|新升级|新品|首发)"
    try:
        rx = re.compile(str(rx_raw), re.I)
    except re.error:
        rx = re.compile(r"(上新|全新|新升级|新品|首发)", re.I)
    new_codes = set(str(x) for x in (prefs.get("newProductCodes") or []) if x)
    new_codes |= set(str(x) for x in (prefs.get("newMainCodes") or []) if x)
    for cat in (menu or {}).get("categories") or []:
        for m in cat.get("meals") or []:
            tags = [str(t) for t in (m.get("tags") or [])]
            if rx.search(" ".join(tags)):
                code = str(m.get("code") or "")
                if code:
                    new_codes.add(code)
    if not new_codes:
        return
    for c in cands:
        hit = False
        pcodes = [str(it.get("productCode") or "") for it in (c.get("items") or [])]
        pmc = str(c.get("primaryMainCode") or "")
        flat = []
        for it in c.get("items") or []:
            for rnd in it.get("roundList") or []:
                for ci in rnd.get("comboItemList") or []:
                    flat.append(str(ci.get("code") or ""))
        if pmc in new_codes or any(p in new_codes for p in pcodes) or any(x in new_codes for x in flat):
            hit = True
        if hit:
            tags = list(c.get("tags") or [])
            if "new" not in tags:
                tags.append("new")
            c["tags"] = tags



def main() -> int:
    ap = argparse.ArgumentParser(description="Expand McD candidates (profile-aware, no MCP/order)")
    ap.add_argument("--profile", help="profiles/<name>.json or path")
    ap.add_argument("--prefs-file", help="overlay prefs/request JSON onto profile/base")
    ap.add_argument("--base", help="existing RecommendRequest to overlay (legacy; optional with --profile)")
    ap.add_argument("--details-dir", help="meal-details dir (default from profile.paths or runs/meal-details)")
    ap.add_argument("--menu", help="query-meals slim JSON (default from profile.paths)")
    ap.add_argument("-o", "--output", help="output request JSON with candidates[]")
    ap.add_argument("--max", type=int, default=100, help="cap candidates for scorer")
    ap.add_argument(
        "--families",
        help="comma list override: mcard_four,dual_1plus1,dabao,classic_three,coffee_combo",
    )
    args = ap.parse_args()

    if not args.profile and not args.base:
        ap.error("need --profile and/or --base")

    try:
        if args.profile:
            base = load_profile(args.profile)
        else:
            base = load(Path(args.base))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"input error: {exc}") from exc

    if args.prefs_file:
        base = apply_prefs_file(base, args.prefs_file)
    if args.base and args.profile:
        # explicit base overlays profile-materialized request (except we already loaded profile)
        base = {**base, **{k: v for k, v in load(Path(args.base)).items() if k != "candidates"}}

    paths = base.get("_paths") or {}
    details_dir = Path(args.details_dir or paths.get("detailsDir") or (SCRIPT_DIR / "runs" / "meal-details"))
    if not args.details_dir and not details_dir.is_absolute():
        details_dir = SCRIPT_DIR / details_dir
    menu_fallback = paths.get("menuSlim")
    if not args.menu and not menu_fallback:
        raise SystemExit(
            "menu path missing: pass --menu or set profile.paths.menuSlim "
            "(do not rely on a hard-coded store slim file)"
        )
    menu_path = Path(str(args.menu or menu_fallback))
    if not args.menu and not menu_path.is_absolute():
        menu_path = SCRIPT_DIR / menu_path
    out_path = Path(args.output or paths.get("defaultOutput") or (SCRIPT_DIR / "runs" / "request-built.json"))
    if not args.output and not out_path.is_absolute():
        out_path = SCRIPT_DIR / out_path

    try:
        if not details_dir.is_dir():
            raise ContractError(f"{details_dir}: directory not found")
        menu = normalize_menu(load_snapshot(menu_path), path=str(menu_path))
        details = {}
        for p in details_dir.glob("*.json"):
            detail = load_snapshot(p)
            rounds_of(detail, path=str(p))
            details[p.stem] = detail
    except ContractError as exc:
        raise SystemExit(f"input contract error: {exc}") from exc

    if args.families:
        families = [x.strip() for x in args.families.split(",") if x.strip()]
    else:
        families = resolve_families(base)

    try:
        cands = expand(
            details,
            menu,
            base.get("prefs") or {},
            families=families,
            budget_fen=int(base.get("budgetFen") or 3500),
        )
    except ContractError as exc:
        raise SystemExit(f"input contract error: {exc}") from exc

    # trim: prefer families that appear earlier in prefs.priority / families list
    pri = [str(x) for x in ((base.get("prefs") or {}).get("priority") or [])]
    def fam_rank(c: Dict[str, Any]) -> int:
        tags = set(c.get("tags") or [])
        order = []
        for i, f in enumerate(families):
            if f == "mcard_four" and tags & {"mcard_four", "mcard"}:
                order.append(i)
            if f == "dual_1plus1" and "dual11" in tags:
                order.append(i)
            if f == "dabao" and "dabao" in tags:
                order.append(i)
            if f == "classic_three" and tags & {"classic_three", "three_piece"}:
                order.append(i)
            if f == "coffee_combo" and tags & {"coffee", "coffee_combo"}:
                order.append(i)
        return min(order) if order else 99

    per_main: Dict[str, int] = {}
    trimmed: List[Dict[str, Any]] = []
    cands_sorted = sorted(
        cands,
        key=lambda c: (
            fam_rank(c),
            0 if "mcard_four" in (c.get("tags") or []) else 1,
            0 if "dual11" in (c.get("tags") or []) else 1,
            # within classic_three, prefer underrepresented mains (鱼/吉士包)
            (0 if str(c.get("primaryMainCode") or "") == "1600" else 1 if str(c.get("primaryMainCode") or "") == "1010" else 2)
                if "classic_three" in (c.get("tags") or []) else 3,
            c.get("listPriceFen") or 0,
            c.get("rotationKey"),
        ),
    )
    # Pass A: guarantee classic_three coverage (esp. unique mains) before per-main caps fill
    classic_reserved: List[Dict[str, Any]] = []
    seen_classic_main = set()
    for c in cands_sorted:
        tags = set(c.get("tags") or [])
        if "classic_three" not in tags:
            continue
        mc = str(c.get("primaryMainCode") or "")
        if mc in seen_classic_main and len(classic_reserved) >= 8:
            continue
        if mc not in seen_classic_main or mc in {"1600", "1010"}:
            classic_reserved.append(c)
            seen_classic_main.add(mc)
        if len(classic_reserved) >= 10:
            break
    for c in classic_reserved:
        rk = c.get("rotationKey")
        if any(x.get("rotationKey") == rk for x in trimmed):
            continue
        mc = str(c.get("primaryMainCode") or "")
        trimmed.append(c)
        per_main[mc] = per_main.get(mc, 0) + 1

    for c in cands_sorted:
        if len(trimmed) >= args.max:
            break
        rk = c.get("rotationKey")
        if any(x.get("rotationKey") == rk for x in trimmed):
            continue
        mc = str(c.get("primaryMainCode") or "")
        if per_main.get(mc, 0) >= 4 and len(trimmed) >= min(40, args.max):
            continue
        if per_main.get(mc, 0) >= 6:
            continue
        trimmed.append(c)
        per_main[mc] = per_main.get(mc, 0) + 1

    out = deepcopy(base)
    # strip internal keys
    for k in list(out.keys()):
        if k.startswith("_"):
            out.pop(k, None)
    out["candidateFamilies"] = families
    out["candidates"] = trimmed
    sel = dict(out.get("selection") or {})
    sel.setdefault("mode", "rank_then_random")
    sel.setdefault("topK", 5)
    sel.setdefault("diversifyByMain", True)
    sel.setdefault("maxPerMainInTopK", 1)
    out["selection"] = sel
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    mains = {}
    tag_counts: Dict[str, int] = {}
    for c in trimmed:
        mains[c.get("primaryMainCode")] = mains.get(c.get("primaryMainCode"), 0) + 1
        for t in c.get("tags") or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
    print(
        json.dumps(
            {
                "profileId": out.get("profileId"),
                "mealPeriod": out.get("mealPeriod"),
                "families": families,
                "candidates": len(trimmed),
                "primaryMainCodes": mains,
                "tagCounts": tag_counts,
                "output": str(out_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
