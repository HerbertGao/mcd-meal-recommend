#!/usr/bin/env python3
"""Expand dual-1+1 / 大堡口福 / 麦金卡 candidates from meal-details (no MCP / no order).

Deal SKUs come from the slim menu (category, mealIndex, name). Hardcoded codes are
only a fallback when that signal is missing. ``--max`` then round-robins across
families and across dual-left / mcard product codes so a new leg is not trimmed away.

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
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from mcp_contract import ContractError, load_snapshot, normalize_menu, rounds_of
from profile_lib import SCRIPT_DIR, apply_prefs_file, load_profile, resolve_profile_path

DRINK_RE = re.compile(r"(可乐|雪碧|芬达|汽水|橙汁|豆浆|咖啡|奶茶|柠檬水|饮|茶(?!叶)|麦炫酷)", re.I)
MAIN_RE = re.compile(r"(堡|汉堡|吉士|麦香鸡|板烧|巨无霸|双层|卷|鱼堡|安格斯|麦香鱼|鸡排堡|麦满分|炒双蛋|营养卷|火腿扒|猪柳)", re.I)
SIDE_RE = re.compile(r"(薯条|派|鸡翅|鸡腿|琵琶|麦乐鸡|鸡块|小食|玉米|沙拉|脆汁鸡|鸡排(?!堡)|新地|薯饼|油条|香肠)", re.I)

DRINK_CODES = {"522021", "3071", "522020", "3050", "3060", "3110"}
NON_DRINK_WHITE = {"4105", "4106", "4437", "4420", "4422"}
FORBIDDEN_AS_MAIN_CODES = {"1401"}  # 麦乐鸡 — never left-leg / primary main

# 菜单没有分类/名称信号时的退路。能从当日菜单认出家族时，不以这些码为准。
FALLBACK_MCARD_FOUR_CODES = [
    "9900004236",  # 人气超值四件套随心选 ¥28
    "9900000884",  # 酥酥多笋卷四件套 ¥28
    "9900016629",  # 海陆双全堡四件套 ¥30.9
    "9900010110",  # 鱼牛汉堡四件套随心选 ¥32
    "9900006957",  # 安格斯厚牛堡四件套随心选 ¥37（预算外由 scorer 淘汰）
]
FALLBACK_DUAL_LEFT = ["9900016655", "9900016651"]  # 双吉 / 三件套左腿
FALLBACK_DUAL_RIGHT = ["9900016668"]  # 真有料右腿
FALLBACK_DABAO_CODES = ["9900016670", "9900016671", "9900016673", "9900016674"]
# 旧名只是退路别名。发现逻辑不把它们当作唯一名单。
MCARD_FOUR_CODES = FALLBACK_MCARD_FOUR_CODES
DUAL11_LEFT = FALLBACK_DUAL_LEFT
DUAL11_RIGHT = FALLBACK_DUAL_RIGHT[0]
DABAO_IDS = FALLBACK_DABAO_CODES
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
DUAL_SIGNAL_RE = re.compile(r"(随心配|1\s*[+＋]\s*1)")
MCARD_CAT_RE = re.compile(r"(麦金卡|金卡专享)")
MCARD_FOUR_NAME_RE = re.compile(r"四件套")
DABAO_SIGNAL_RE = re.compile(r"大堡口福")
SHARE_MEAL_RE = re.compile(r"(双人|三人|多人|分享|拼盘|八件|亲子|BFF|周边|派对)")
OTHER_DEAL_RE = re.compile(r"(四件套|大堡口福|麦金卡|金卡专享|开心乐园|早餐|麦满分)")


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


# 每个主食下：小食轮各取若干、组合数与饮品数有上限，避免菜单笛卡尔积撑爆候选池。
# 组合上限由 iter_side_combos 按轮转填充，不用深度优先把名额留给前几种第一小食。
MAX_CHOICES_PER_SIDE_ROUND = 4
MAX_SIDE_COMBOS_PER_MAIN = 8
MAX_DRINKS_PER_SELECTION = 4


def _choice_sort_key(ch: Dict[str, Any], prefer: Sequence[str]) -> Tuple[int, int, str]:
    code = str(ch.get("code") or "")
    try:
        pref_i = list(prefer).index(code)
    except ValueError:
        pref_i = 1000
    return (pref_i, int(ch.get("diffFen") or 0), code)


def truncate_choices(
    choices: Sequence[Dict[str, Any]],
    prefer: Sequence[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """软偏好排序后截断。偏好码只影响顺序，不当成唯一可选项。

    当轮次里还有非偏好项且上限 ≥ 2 时，至少留一个非偏好席位，避免 prefer 列表把整轮写死。
    """
    ranked = sorted(list(choices or []), key=lambda c: _choice_sort_key(c, prefer))
    if limit <= 0 or len(ranked) <= limit:
        return ranked
    prefer_set = {str(x) for x in prefer}
    preferred = [c for c in ranked if str(c.get("code") or "") in prefer_set]
    others = [c for c in ranked if str(c.get("code") or "") not in prefer_set]
    if preferred and others and limit >= 2:
        keep_pref = min(len(preferred), limit - 1)
        picked = preferred[:keep_pref] + others[: limit - keep_pref]
        picked.sort(key=lambda c: _choice_sort_key(c, prefer))
        return picked
    return ranked[:limit]


def _within_budget(base_fen: int, parts: Sequence[Dict[str, Any]], budget_fen: Optional[int]) -> bool:
    if budget_fen is None or int(budget_fen) <= 0:
        return True
    total = int(base_fen) + sum(int(p.get("diffFen") or 0) for p in parts)
    return total <= int(budget_fen)


def _looks_drink_round(rnd: Dict[str, Any]) -> bool:
    name = str(rnd.get("name") or "")
    if any(k in name for k in ("饮料", "饮品")):
        return True
    chs = list(rnd.get("choices") or [])
    if not chs:
        return False
    n_drink = sum(1 for c in chs if is_drink(c) or is_coffee_choice(c))
    n_main = sum(1 for c in chs if is_mainish(c))
    return n_drink >= max(1, (len(chs) + 1) // 2) and n_drink > n_main


def _looks_main_round(rnd: Dict[str, Any]) -> bool:
    name = str(rnd.get("name") or "")
    if any(k in name for k in ("饮料", "饮品", "小食", "配菜", "薯条", "甜品")):
        return False
    if any(k in name for k in ("主食", "汉堡", "红区")) or ("堡" in name and "小食" not in name):
        return True
    chs = list(rnd.get("choices") or [])
    if not chs:
        return False
    n_main = sum(1 for c in chs if is_mainish(c))
    return n_main >= max(1, (len(chs) + 1) // 2)


def split_meal_rounds(
    rounds: Sequence[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """主食轮、小食轮（按菜单顺序）、饮品轮。

    先看轮次名和选项角色；认不出时退回「第一轮主食、最后一轮饮品、中间小食」。
    """
    seq = list(rounds or [])
    drink = next((r for r in seq if _looks_drink_round(r)), None)
    main = next((r for r in seq if r is not drink and _looks_main_round(r)), None)
    if main is None:
        main = next((r for r in seq if r is not drink), None)
    if drink is None:
        drink = next((r for r in reversed(seq) if r is not main), None)
    sides = [r for r in seq if r is not main and r is not drink]
    return main, sides, drink


def mains_from_round(rnd: Dict[str, Any]) -> List[Dict[str, Any]]:
    """主食轮里的汉堡/卷。麦乐鸡等鸡块只做小食，不进入主食候选。"""
    chs = list(rnd.get("choices") or [])
    mains = [c for c in chs if is_mainish(c)]
    if mains:
        return mains
    return [
        c
        for c in chs
        if c.get("code") not in FORBIDDEN_AS_MAIN_CODES
        and "麦乐鸡" not in (c.get("name") or "")
        and "鸡块" not in (c.get("name") or "")
        and not is_drink(c)
        and not is_sideish(c)
    ]


def side_pools(
    side_rounds: Sequence[Dict[str, Any]],
    prefer_sides: Sequence[str],
) -> List[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
    pools: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []
    for rnd in side_rounds:
        chs = [c for c in (rnd.get("choices") or []) if not is_drink(c)]
        if not chs:
            chs = list(rnd.get("choices") or [])
        kept = truncate_choices(chs, prefer_sides, MAX_CHOICES_PER_SIDE_ROUND)
        if kept:
            pools.append((rnd, kept))
    return pools


def _iter_diverse_indexes(sizes: Sequence[int]):
    """笛卡尔积的轮转顺序：每一轮的选项都尽早出现，而不是先耗尽第一项。

    两轮时先走对角线 (0,0)、(1,1)、(2,2)…，再错一位。
    更多轮时对后续轮次递归使用同一顺序，并相对第一轮下标错开。
    每个下标组合恰好出现一次。
    """
    if not sizes:
        yield ()
        return
    head = sizes[0]
    if head <= 0:
        return
    if len(sizes) == 1:
        for i in range(head):
            yield (i,)
        return
    tail = list(_iter_diverse_indexes(sizes[1:]))
    span = len(tail)
    if span == 0:
        return
    for shift in range(span):
        for i in range(head):
            yield (i,) + tail[(shift + i) % span]


def iter_side_combos(
    pools: Sequence[Tuple[Dict[str, Any], List[Dict[str, Any]]]],
    max_combos: int,
    accept,
) -> List[List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
    """按轮次取笛卡尔积，名额在各轮选项之间轮转；accept 为假的组合不占名额。

    深度优先会让第一轮靠前的选项把上限吃光（8 个名额全落在麦乐鸡一类上），
    菜单里更靠后的玉米、派、鸡翅进不了候选。这里按对角线轮转取样，
    顺序只由各轮已经排好的选项决定。
    """
    if not pools:
        return [[]]
    if max_combos <= 0:
        return []
    sizes = [len(chs) for _, chs in pools]
    if any(size <= 0 for size in sizes):
        return []
    combos: List[List[Tuple[Dict[str, Any], Dict[str, Any]]]] = []
    for indexes in _iter_diverse_indexes(sizes):
        acc = [(pools[i][0], pools[i][1][idx]) for i, idx in enumerate(indexes)]
        if accept([ch for _, ch in acc]):
            combos.append(acc)
            if len(combos) >= max_combos:
                break
    return combos


def _blocked_as_drink(ch: Dict[str, Any]) -> bool:
    """派、薯条、主食不能占饮品槽。"""
    return is_sideish(ch) or is_mainish(ch)


def select_drinks(
    choices: Sequence[Dict[str, Any]],
    prefer: Sequence[str],
    prefer_coffee: bool,
    base_fen: int,
    extra: Sequence[Dict[str, Any]],
    budget_fen: Optional[int],
    limit: int = MAX_DRINKS_PER_SELECTION,
) -> List[Dict[str, Any]]:
    """从饮品轮枚举。派、薯条和主食不能占饮品槽。

    已识别的饮品/咖啡始终保留。偏好码里不是小食、主食的未知 SKU 也保留。
    已经识别出饮品时，不再把其余未知项当成饮品。
    一个都认不出时，丢掉小食和主食，只留不像派/薯条的项。
    """
    chs = list(choices or [])
    prefer_set = {str(x) for x in prefer if x}
    if prefer_coffee:
        chs = [c for c in chs if is_coffee_choice(c) and not _blocked_as_drink(c)]
        if not chs:
            return []
    else:
        recognized = [
            c
            for c in chs
            if (is_drink(c) or is_coffee_choice(c)) and not _blocked_as_drink(c)
        ]
        recognized_ids = {id(c) for c in recognized}
        preferred = [
            c
            for c in chs
            if str(c.get("code") or "") in prefer_set
            and not _blocked_as_drink(c)
            and id(c) not in recognized_ids
        ]
        if recognized:
            # 已有饮品时只追加偏好里的未知码，避免把派/薯条或杂项一并收进饮品槽
            chs = recognized + preferred
        else:
            # 一个都认不出：丢掉小食和主食，只留不像派/薯条的项（新品饮品码）
            chs = [c for c in chs if not _blocked_as_drink(c)]
        if not chs:
            return []
    fitting = [
        ch
        for ch in chs
        if _within_budget(base_fen, list(extra) + [ch], budget_fen)
    ]
    return truncate_choices(fitting, prefer, limit)


def build_round_combo(
    code: str,
    det: Dict[str, Any],
    picks: List[Tuple[Dict[str, Any], Dict[str, Any]]],
    price_fen: int,
    ice_key: str,
    *,
    key_prefix: str,
    tags: List[str],
    primary_main: str,
    side_codes: Sequence[str],
    drink_codes: Sequence[str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """按菜单轮次顺序写入 roundList。去冰只打在饮品码上。"""
    order = {r["id"]: i for i, r in enumerate(rounds_of(det))}
    ordered = sorted(picks, key=lambda p: order.get(p[0]["id"], 99))
    drink_set = {str(c) for c in drink_codes if c}
    round_list = []
    name_bits = []
    for rnd, ch in ordered:
        iced = str(ch.get("code") or "") in drink_set
        round_list.append(
            {
                "round": rnd["id"],
                "comboItemList": [combo_item(ch["code"], ice_mod(ice_key) if iced else None)],
            }
        )
        bit = str(ch.get("name") or ch.get("code") or "")
        if iced:
            bit += "去冰"
        name_bits.append(bit)
    code_bits = [str(ch.get("code") or "") for _, ch in ordered]
    rk = f"{key_prefix}{str(code)[-4:]}_{'_'.join(code_bits)}"
    product_name = str(det.get("name") or det.get("productName") or code)
    summary = f"{product_name}（{'+'.join(name_bits)}）≈¥{price_fen / 100:.1f}"
    cand: Dict[str, Any] = {
        "id": rk,
        "rotationKey": rk,
        "humanSummary": summary,
        "listPriceFen": price_fen,
        "priceVerified": False,
        "tags": list(tags),
        "primaryMainCode": primary_main,
        "sideCodes": [str(c) for c in side_codes if c],
        "drinkCodes": [str(c) for c in drink_codes if c],
        "items": [
            {
                "productCode": code,
                "productName": product_name,
                "quantity": 1,
                "roundList": round_list,
            }
        ],
    }
    if extra:
        cand.update(extra)
    return cand


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
    rk = f"{tag_left}{main_a['code']}+zl{main_b['code']}_w{side['code']}_{drink['code']}"
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
        "sideCodes": [side["code"]] if side else [],
        "drinkCodes": [drink["code"]] if drink else [],
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
        "sideCodes": [side["code"]],
        "drinkCodes": [drink["code"]],
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
        "sideCodes": [side["code"]],
        "drinkCodes": [drink["code"]],
        "items": items,
    }


def price_for(code: str, menu: Dict[str, Any], diff_extra: int = 0) -> int:
    idx = (menu or {}).get("mealIndex") or {}
    meta = idx.get(code) or {}
    if "currentPrice" not in meta:
        raise ContractError(f"menu.mealIndex.{code}.currentPrice: required for candidate pricing")
    base = yuan_to_fen(meta["currentPrice"])
    return base + diff_extra



def is_coffee_choice(ch: Dict[str, Any]) -> bool:
    return ch["code"] in set(COFFEE_CODES_PREF) or bool(COFFEE_NAME_RE.search(ch.get("name") or ""))


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


class DualLegs(NamedTuple):
    left: List[str]
    right: List[str]
    source: str  # "menu" when category/name matched, else "fallback"
    roles: Dict[str, str]  # productCode -> left_three | left_burger | right


def _as_codes(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value if v]


def iter_menu_products(menu: Dict[str, Any]) -> List[Dict[str, Any]]:
    """分类餐品优先，mealIndex 补没有出现在分类里的码。"""
    idx = (menu or {}).get("mealIndex") or {}
    if not isinstance(idx, dict):
        idx = {}
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(code: Any, name: Any, category: Any, tags: Any, meta: Any) -> None:
        key = str(code or "")
        if not key or key in seen:
            return
        seen.add(key)
        index_meta = idx.get(key) if isinstance(idx.get(key), dict) else {}
        merged = dict(index_meta or {})
        if isinstance(meta, dict):
            for field, val in meta.items():
                if field not in merged or merged[field] in (None, "", [], {}):
                    merged[field] = val
        resolved_tags = [str(t) for t in (tags or merged.get("tags") or []) if t]
        out.append(
            {
                "code": key,
                "name": str(name or merged.get("name") or merged.get("productName") or ""),
                "category": str(category or merged.get("category") or merged.get("categoryName") or ""),
                "tags": resolved_tags,
                "meta": merged,
            }
        )

    for cat in (menu or {}).get("categories") or []:
        if not isinstance(cat, dict):
            continue
        cname = str(cat.get("name") or cat.get("categoryName") or "")
        meals = cat.get("meals") or cat.get("products") or []
        if not isinstance(meals, list):
            continue
        for meal in meals:
            if not isinstance(meal, dict):
                continue
            add(
                meal.get("code") or meal.get("productCode"),
                meal.get("name") or meal.get("productName"),
                cname,
                meal.get("tags"),
                meal,
            )
    for code, meta in idx.items():
        if not isinstance(meta, dict):
            meta = {}
        add(
            code,
            meta.get("name") or meta.get("productName"),
            meta.get("category") or meta.get("categoryName") or "",
            meta.get("tags"),
            meta,
        )
    return out


def _present_codes(menu: Dict[str, Any], details: Optional[Dict[str, Any]]) -> set:
    present = {str(c) for c in ((menu or {}).get("mealIndex") or {})}
    present |= {str(c) for c in (details or {})}
    return present


def _menu_product_name(menu: Dict[str, Any], code: str, detail: Optional[Dict[str, Any]] = None) -> str:
    for product in iter_menu_products(menu):
        if product["code"] == str(code) and product.get("name"):
            return str(product["name"])
    if detail:
        return str(detail.get("name") or detail.get("productName") or "")
    return ""


def classify_dual_role(name: str, detail: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """双 1+1 角色：left_three / left_burger / right。

    名称里的「真有料 / 三件套 / 双吉」优先；没有这些词时看 detail 轮次形状
    （主食+饮品，或 3 轮三件套，对小食蛋白+配菜）。
    """
    label = str(name or "")
    if "真有料" in label:
        return "right"
    if "三件套" in label:
        return "left_three"
    if "双吉" in label:
        return "left_burger"
    if not detail:
        return None
    try:
        rounds = rounds_of(detail)
    except ContractError:
        return None
    if not rounds:
        return None
    red, white = red_white(rounds)
    red_choices = list(red.get("choices") or [])
    white_choices = list(white.get("choices") or [])
    n_main = sum(1 for choice in red_choices if is_mainish(choice))
    n_side_red = sum(1 for choice in red_choices if is_sideish(choice) and not is_mainish(choice))
    n_drink = sum(1 for choice in white_choices if is_drink(choice))
    n_side_white = sum(1 for choice in white_choices if not is_drink(choice))
    if n_main > 0 and n_drink > 0 and n_main >= n_side_red:
        return "left_three" if len(rounds) >= 3 else "left_burger"
    if n_main == 0 and (n_side_white > 0 or n_side_red > 0) and n_side_white >= n_drink:
        return "right"
    return None


def _dual_signaled(product: Dict[str, Any]) -> bool:
    if DUAL_SIGNAL_RE.search(product.get("category") or ""):
        return True
    if DUAL_SIGNAL_RE.search(product.get("name") or ""):
        return True
    return any(DUAL_SIGNAL_RE.search(tag) for tag in product.get("tags") or [])


def _dual_rejected(name: str) -> bool:
    return bool(SHARE_MEAL_RE.search(name) or OTHER_DEAL_RE.search(name))


def _fallback_dual_legs(menu: Dict[str, Any], details: Dict[str, Any]) -> DualLegs:
    present = _present_codes(menu, details)
    right: List[str] = []
    for code in _as_codes(FALLBACK_DUAL_RIGHT):
        if code in present and code not in right:
            right.append(code)
    left: List[str] = []
    for code in _as_codes(FALLBACK_DUAL_LEFT):
        if code in present and code not in right and code not in left:
            left.append(code)
    roles: Dict[str, str] = {}
    for code in right:
        detail = details.get(code)
        role = classify_dual_role(_menu_product_name(menu, code, detail), detail)
        roles[code] = role or "right"
    for code in left:
        detail = details.get(code)
        role = classify_dual_role(_menu_product_name(menu, code, detail), detail)
        roles[code] = role or "left_burger"
    return DualLegs(left, right, "fallback", roles)


def discover_dual_legs(
    menu: Dict[str, Any],
    details: Optional[Dict[str, Dict[str, Any]]] = None,
) -> DualLegs:
    """从「随心配 / 1+1」分类、名称或标签发现左/右腿。没有信号时才用退路码。"""
    details = details or {}
    signaled = [product for product in iter_menu_products(menu) if _dual_signaled(product)]
    if not signaled:
        return _fallback_dual_legs(menu, details)
    left: List[str] = []
    right: List[str] = []
    roles: Dict[str, str] = {}
    for product in signaled:
        if _dual_rejected(product.get("name") or ""):
            continue
        code = product["code"]
        detail = details.get(code)
        name = product.get("name") or str((detail or {}).get("name") or (detail or {}).get("productName") or "")
        role = classify_dual_role(name, detail)
        if role == "right":
            right.append(code)
        elif role in ("left_three", "left_burger"):
            left.append(code)
        else:
            continue
        roles[code] = role
    return DualLegs(left, right, "menu", roles)


def discover_mcard_four_codes(
    menu: Dict[str, Any],
    details: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """麦金卡分类（或名称/标签）里的四件套。分享餐排除。没有该信号时才退回已知码。"""
    explicit = _as_codes((menu or {}).get("mcardFourProductCodes"))
    explicit_set = set(explicit)
    found: List[str] = []
    seen = set()
    saw_signal = bool(explicit)

    def add(code: str) -> None:
        if code and code not in seen:
            seen.add(code)
            found.append(code)

    for product in iter_menu_products(menu):
        category_hit = bool(
            MCARD_CAT_RE.search(product.get("category") or "")
            or any(MCARD_CAT_RE.search(tag) for tag in product.get("tags") or [])
        )
        name = str(product.get("name") or "")
        name_mcard = bool(MCARD_CAT_RE.search(name))
        if product["code"] in explicit_set:
            add(product["code"])
            continue
        if not category_hit and not name_mcard:
            continue
        saw_signal = True
        if SHARE_MEAL_RE.search(name) or DABAO_SIGNAL_RE.search(name):
            continue
        if DUAL_SIGNAL_RE.search(name) and not MCARD_FOUR_NAME_RE.search(name):
            continue
        if not MCARD_FOUR_NAME_RE.search(name):
            continue
        add(product["code"])
    for code in explicit:
        add(code)
    if saw_signal:
        return found
    present = _present_codes(menu, details)
    return [code for code in _as_codes(FALLBACK_MCARD_FOUR_CODES) if code in present]


def discover_dabao_codes(
    menu: Dict[str, Any],
    details: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """大堡口福分类或名称。双人/分享排除。没有该信号时才退回已知码。"""
    found: List[str] = []
    seen = set()
    saw_signal = False
    for product in iter_menu_products(menu):
        name = str(product.get("name") or "")
        hit = bool(
            DABAO_SIGNAL_RE.search(product.get("category") or "")
            or DABAO_SIGNAL_RE.search(name)
            or any(DABAO_SIGNAL_RE.search(tag) for tag in product.get("tags") or [])
        )
        if not hit:
            continue
        saw_signal = True
        if SHARE_MEAL_RE.search(name):
            continue
        if MCARD_FOUR_NAME_RE.search(name) or MCARD_CAT_RE.search(name) or DUAL_SIGNAL_RE.search(name):
            continue
        if product["code"] not in seen:
            seen.add(product["code"])
            found.append(product["code"])
    if saw_signal:
        return found
    present = _present_codes(menu, details)
    return [code for code in _as_codes(FALLBACK_DABAO_CODES) if code in present]


def mcard_four_codes(menu: Dict[str, Any], details: Dict[str, Dict[str, Any]]) -> List[str]:
    """当日可展开的麦金卡四件套，便宜的在前。名单来自菜单发现，不是写死的并集。"""
    discovered = discover_mcard_four_codes(menu, details)
    known = list(FALLBACK_MCARD_FOUR_CODES)
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

    seen = set()
    ordered: List[str] = []
    for code in discovered:
        if code in seen:
            continue
        seen.add(code)
        ordered.append(code)
    return sorted([code for code in ordered if code in details], key=price_key)


def expand_mcard_four(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer_drinks: List[str],
    ice_key: str,
    prefer_coffee: bool,
    budget_fen: Optional[int] = None,
    prefer_sides: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """麦金卡四件套：先定主食，再按小食轮配组合，最后从饮品轮枚举。

    小食对和饮品都来自该套餐 detail 的可选项，不用写死的单一小食对或默认饮品。
    preferSideCodes / drink.preferCodes 只做软排序；超预算的组合丢掉。
    小食组合达到上限时按轮转保留，不把名额留给第一种小食。
    """
    out: List[Dict[str, Any]] = []
    prefer_side_codes = [str(x) for x in (prefer_sides or [])]
    prefer_drink_codes = [str(x) for x in (prefer_drinks or [])]
    ordered = mcard_four_codes(menu, details)
    for code in ordered:
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 3:
            continue
        main_r, side_rs, drink_r = split_meal_rounds(rounds)
        if not main_r or not drink_r or not side_rs:
            continue
        mains = mains_from_round(main_r)
        pools = side_pools(side_rs, prefer_side_codes)
        if not mains or not pools:
            continue
        base = price_for(code, menu)
        drink_source = list(drink_r.get("choices") or [])
        if prefer_coffee:
            coffees = [c for c in drink_source if is_coffee_choice(c)]
            if coffees:
                drink_source = coffees
        min_drink_diff = min((int(c.get("diffFen") or 0) for c in drink_source), default=0)
        # 先主食，再小食组合，再饮品
        for burger in mains:
            def _side_ok(side_chs: Sequence[Dict[str, Any]], _burger=burger) -> bool:
                return _within_budget(
                    base,
                    [_burger] + list(side_chs) + [{"diffFen": min_drink_diff}],
                    budget_fen,
                )

            for combo in iter_side_combos(pools, MAX_SIDE_COMBOS_PER_MAIN, _side_ok):
                side_chs = [ch for _, ch in combo]
                drinks = select_drinks(
                    drink_r.get("choices") or [],
                    prefer_drink_codes,
                    prefer_coffee,
                    base,
                    [burger] + side_chs,
                    budget_fen,
                )
                for drink in drinks:
                    price = (
                        base
                        + int(burger.get("diffFen") or 0)
                        + sum(int(c.get("diffFen") or 0) for c in side_chs)
                        + int(drink.get("diffFen") or 0)
                    )
                    if not _within_budget(base, [burger] + side_chs + [drink], budget_fen):
                        continue
                    tags = ["mcard_four", "mcard", "four_piece", "deal", "satiety", "built"]
                    if is_coffee_choice(drink) and "coffee" not in tags:
                        tags.append("coffee")
                    out.append(
                        build_round_combo(
                            code,
                            det,
                            [(main_r, burger)] + combo + [(drink_r, drink)],
                            price,
                            ice_key,
                            key_prefix="mk",
                            tags=tags,
                            primary_main=str(burger.get("code") or ""),
                            side_codes=[str(c.get("code") or "") for c in side_chs],
                            drink_codes=[str(drink.get("code") or "")],
                            extra={"mcardFour": True},
                        )
                    )
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
        "sideCodes": [side["code"]] if side else [],
        "drinkCodes": [drink["code"]],
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
        # 先主食，再小食，再从饮品轮里的咖啡枚举
        for main in mains[:6]:
            for side in sides:
                extra_parts = [main] + ([side] if side else [])
                drinks = select_drinks(
                    r_drink["choices"],
                    prefer_drinks,
                    True,
                    base,
                    extra_parts,
                    budget_fen,
                )
                for drink in drinks:
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
    prefer_sides: Optional[Sequence[str]] = None,
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
    out.extend(
        expand_mcard_four(
            details,
            menu,
            prefer_drinks,
            ice_key,
            prefer_coffee=True,
            budget_fen=budget_fen,
            prefer_sides=prefer_sides,
        )
    )
    # dabao with coffee：主食 → 小食轮 → 饮品轮里的咖啡
    side_pref = [str(x) for x in (prefer_sides or [])]
    for code in discover_dabao_codes(menu, details):
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 3:
            continue
        burgers = [c for c in rounds[0]["choices"] if is_mainish(c)] or [
            c
            for c in rounds[0]["choices"]
            if c["code"] not in FORBIDDEN_AS_MAIN_CODES and "麦乐鸡" not in (c.get("name") or "")
        ]
        sides = truncate_choices(
            [c for c in rounds[1]["choices"] if not is_drink(c)] or list(rounds[1]["choices"]),
            side_pref,
            MAX_CHOICES_PER_SIDE_ROUND,
        )
        if not burgers or not sides:
            continue
        base = price_for(code, menu)
        for burger in burgers:
            for side in sides:
                drinks = select_drinks(
                    rounds[2]["choices"],
                    prefer_drinks,
                    True,
                    base,
                    [burger, side],
                    budget_fen,
                )
                for drink in drinks:
                    price = base + burger["diffFen"] + side["diffFen"] + drink["diffFen"]
                    cand = build_dabao(code, det, burger, side, drink, price, ice_key)
                    cand["tags"] = list(dict.fromkeys((cand.get("tags") or []) + ["coffee", "coffee_combo"]))
                    cand["rotationKey"] = f"cf{cand['rotationKey']}"
                    cand["id"] = cand["rotationKey"]
                    out.append(cand)
    # dual 1+1 with coffee on the drink leg, using discovered left/right roles
    legs = discover_dual_legs(menu, details)
    seen_dual: set = set()
    for right_code in legs.right:
        zl = details.get(right_code)
        if not zl:
            continue
        for left_code in legs.left:
            det = details.get(left_code)
            if not det:
                continue
            red, white = red_white(rounds_of(det))
            zl_red, zl_white = red_white(rounds_of(zl))
            sides = [c for c in zl_white["choices"] if not is_drink(c)]
            sides = sorted(sides, key=lambda c: (0 if c["code"] in NON_DRINK_WHITE else 1, c["code"]))[:3]
            right_mains = [c for c in zl_red["choices"] if is_sideish(c) or not is_mainish(c)][:4] or list(zl_red["choices"])[:4]
            left_mains = [c for c in red["choices"] if is_mainish(c)] or list(red["choices"])
            base = price_for(left_code, menu) + price_for(right_code, menu)
            for main_a in left_mains[:4]:
                for main_b in right_mains:
                    if main_a["code"] == main_b["code"]:
                        continue
                    for side in sides[:2]:
                        coffees = select_drinks(
                            white["choices"],
                            prefer_drinks,
                            True,
                            base,
                            [main_a, main_b, side],
                            budget_fen,
                        )
                        for coffee in coffees:
                            price = base + main_a["diffFen"] + main_b["diffFen"] + side["diffFen"] + coffee["diffFen"]
                            cand = build_dual(
                                left_code, det, main_a, coffee, right_code, zl, main_b, side, price, ice_key
                            )
                            cand["tags"] = list(dict.fromkeys((cand.get("tags") or []) + ["coffee", "coffee_combo"]))
                            cand["rotationKey"] = f"cf{cand['rotationKey']}"
                            cand["id"] = cand["rotationKey"]
                            _keep_dual(cand, seen_dual, out, left_code)
    return out



def _keep_dual(
    cand: Dict[str, Any],
    seen: set,
    out: List[Dict[str, Any]],
    left_code: str,
) -> None:
    """保留一条双 1+1。两条左腿撞车时在 rotationKey 前加上左腿码，避免后写的整组被丢掉。"""
    rk = str(cand.get("rotationKey") or "")
    if rk in seen:
        rk = f"{str(left_code)[-4:]}_{rk}"
        cand["rotationKey"] = rk
        cand["id"] = rk
    if not rk or rk in seen:
        return
    seen.add(rk)
    out.append(cand)


def _dual_right_pools(
    detail: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """真有料一类右腿：蛋白排序后最多 6 个，非饮配菜最多 5 个。"""
    zl_red, zl_white = red_white(rounds_of(detail))
    zl_sides = [c for c in zl_white["choices"] if not is_drink(c)]
    zl_sides_sorted = sorted(
        zl_sides,
        key=lambda c: (0 if c["code"] in NON_DRINK_WHITE else 1, c["code"]),
    )[:5]
    zl_mains_sideish = [c for c in zl_red["choices"] if is_sideish(c) or not is_mainish(c)]
    zl_mains_all = list(zl_red["choices"])
    right_mains: List[Dict[str, Any]] = []
    for pool in (zl_mains_sideish, zl_mains_all):
        for choice in pool:
            if choice["code"] not in {item["code"] for item in right_mains}:
                right_mains.append(choice)
    # 优先琵琶腿/鸡排；麦乐鸡垫底（可作配，不当首选蛋白）
    right_mains.sort(
        key=lambda c: (
            2 if c["code"] in FORBIDDEN_AS_MAIN_CODES or "麦乐鸡" in (c.get("name") or "") else 0,
            0 if c["code"] in {"505416", "521156"} else 1,
            c.get("diffFen") or 0,
            c["code"],
        )
    )
    return zl_red, zl_white, right_mains[:6], zl_sides_sorted


def _dual_left_mains(red: Dict[str, Any]) -> List[Dict[str, Any]]:
    """左腿红区只要汉堡/卷。麦乐鸡、琵琶腿、鸡排不能当主堡。"""
    left_mains = [c for c in red["choices"] if is_mainish(c)]
    if left_mains:
        return left_mains
    return [
        c
        for c in red["choices"]
        if c["code"] not in FORBIDDEN_AS_MAIN_CODES and not is_sideish(c)
    ]


def expand_dual_1plus1(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer: List[str],
    ice_key: str,
    budget_fen: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """左腿（三件套/双吉）× 右腿（真有料）。腿来自菜单发现，不是写死的商品码。"""
    out: List[Dict[str, Any]] = []
    seen = set()
    legs = discover_dual_legs(menu, details)
    if not legs.left or not legs.right:
        return out

    def remember(cand: Dict[str, Any], left_code: str) -> None:
        _keep_dual(cand, seen, out, left_code)

    for right_code in legs.right:
        zl = details.get(right_code)
        if not zl:
            continue
        _zl_red, _zl_white, right_mains, zl_sides_sorted = _dual_right_pools(zl)
        for left_code in legs.left:
            det = details.get(left_code)
            if not det:
                continue
            red, white = red_white(rounds_of(det))
            left_mains = _dual_left_mains(red)
            if not left_mains:
                continue
            base = price_for(left_code, menu) + price_for(right_code, menu)
            for main_a in left_mains:
                for main_b in right_mains:
                    if main_a["code"] == main_b["code"]:
                        continue
                    if not (is_mainish(main_a) or is_mainish(main_b)):
                        continue
                    for side in zl_sides_sorted[:3]:
                        drinks = select_drinks(
                            white["choices"],
                            prefer,
                            False,
                            base,
                            [main_a, main_b, side],
                            budget_fen,
                        )
                        for drink in drinks:
                            price = (
                                base
                                + main_a["diffFen"]
                                + main_b["diffFen"]
                                + side["diffFen"]
                                + drink["diffFen"]
                            )
                            remember(
                                build_dual(
                                    left_code,
                                    det,
                                    main_a,
                                    drink,
                                    right_code,
                                    zl,
                                    main_b,
                                    side,
                                    price,
                                    ice_key,
                                ),
                                left_code,
                            )

    # 右腿红区里若有真汉堡，可与双吉左腿对调（三件套左腿不走这条，避免把薯条轮当配菜腿）。
    for right_code in legs.right:
        zl = details.get(right_code)
        if not zl:
            continue
        zl_red, zl_white = red_white(rounds_of(zl))
        protein_mains = [c for c in zl_red["choices"] if is_mainish(c)]
        if not protein_mains:
            continue
        for left_code in legs.left:
            if legs.roles.get(left_code) == "left_three":
                continue
            sj = details.get(left_code)
            if not sj:
                continue
            sj_red, sj_white = red_white(rounds_of(sj))
            sj_sides = [c for c in sj_white["choices"] if not is_drink(c)]
            sj_sides = sorted(
                sj_sides,
                key=lambda c: (0 if c["code"] in NON_DRINK_WHITE else 1, c["code"]),
            )[:3]
            side_mains = [c for c in sj_red["choices"] if is_sideish(c)] or [
                c for c in sj_red["choices"] if c["code"] != "1120"
            ]
            if not side_mains:
                continue
            base = price_for(right_code, menu) + price_for(left_code, menu)
            for main_a in protein_mains[:5]:
                for main_b in side_mains[:5]:
                    if main_a["code"] == main_b["code"]:
                        continue
                    for side in sj_sides[:2]:
                        drinks = select_drinks(
                            zl_white["choices"],
                            prefer,
                            False,
                            base,
                            [main_a, main_b, side],
                            budget_fen,
                        )
                        for zl_drink in drinks:
                            price = base + main_a["diffFen"] + main_b["diffFen"] + zl_drink["diffFen"]
                            cand = build_dual(
                                right_code,
                                zl,
                                main_a,
                                zl_drink,
                                left_code,
                                sj,
                                main_b,
                                side,
                                price,
                                ice_key,
                            )
                            cand["rotationKey"] = (
                                f"zl{main_a['code']}+sj{main_b['code']}_w{side['code']}_{zl_drink['code']}"
                            )
                            cand["id"] = cand["rotationKey"]
                            cand["primaryMainCode"] = main_a["code"]
                            remember(cand, left_code)
    return out


def expand_dabao(
    details: Dict[str, Dict[str, Any]],
    menu: Dict[str, Any],
    prefer: List[str],
    ice_key: str,
    budget_fen: Optional[int] = None,
    prefer_sides: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen = set()
    side_pref = [str(x) for x in (prefer_sides or [])]
    for code in discover_dabao_codes(menu, details):
        det = details.get(code)
        if not det:
            continue
        rounds = rounds_of(det)
        if len(rounds) < 3:
            continue
        burgers = [c for c in rounds[0]["choices"] if is_mainish(c)]
        if not burgers:
            burgers = [
                c
                for c in rounds[0]["choices"]
                if c["code"] not in FORBIDDEN_AS_MAIN_CODES
                and "麦乐鸡" not in (c.get("name") or "")
                and not is_drink(c)
            ]
        sides = truncate_choices(
            [c for c in rounds[1]["choices"] if not is_drink(c)] or list(rounds[1]["choices"]),
            side_pref,
            MAX_CHOICES_PER_SIDE_ROUND,
        )
        if not burgers or not sides:
            continue
        base = price_for(code, menu)
        for burger in burgers:
            for side in sides:
                drinks = select_drinks(
                    rounds[2]["choices"],
                    prefer,
                    False,
                    base,
                    [burger, side],
                    budget_fen,
                )
                for drink in drinks:
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

    legs = discover_dual_legs(menu, details)
    known_other = set(_as_codes(FALLBACK_MCARD_FOUR_CODES))
    known_other |= set(_as_codes(FALLBACK_DUAL_LEFT)) | set(_as_codes(FALLBACK_DUAL_RIGHT))
    known_other |= set(_as_codes(FALLBACK_DABAO_CODES))
    known_other |= set(discover_mcard_four_codes(menu, details))
    known_other |= set(legs.left) | set(legs.right)
    known_other |= set(discover_dabao_codes(menu, details))
    known_other |= set(_as_codes((menu or {}).get("mcardFourProductCodes")))

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
    prefer_sides: Optional[Sequence[str]] = None,
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
        side_pref = [str(x) for x in (prefer_sides or [])]
        sides = truncate_choices(
            [c for c in (r_side["choices"] or []) if not is_drink(c)] or list(r_side["choices"] or []),
            side_pref,
            MAX_CHOICES_PER_SIDE_ROUND,
        )
        if not sides:
            continue
        base = price_for(code, menu)
        for burger in burgers:
            for side in sides:
                drinks = select_drinks(
                    r_drink["choices"] or [],
                    prefer,
                    False,
                    base,
                    [burger, side],
                    budget_fen,
                )
                for drink in drinks:
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
    prefer_sides = [str(x) for x in ((prefs or {}).get("preferSideCodes") or [])]

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
        add_all(expand_coffee_combo(
            details, menu, coffee_prefer, ice_key, budget_fen=budget_fen, prefer_sides=prefer_sides
        ))
    if "mcard_four" in fams:
        prefer_coffee = "coffee" in ((prefs or {}).get("priority") or []) or bool((prefs or {}).get("requireCoffee"))
        add_all(expand_mcard_four(
            details,
            menu,
            coffee_prefer if prefer_coffee else prefer,
            ice_key,
            prefer_coffee,
            budget_fen=budget_fen,
            prefer_sides=prefer_sides,
        ))
    if "dual_1plus1" in fams:
        add_all(expand_dual_1plus1(details, menu, prefer, ice_key, budget_fen=budget_fen))
    if "dabao" in fams:
        add_all(expand_dabao(
            details, menu, prefer, ice_key, budget_fen=budget_fen, prefer_sides=prefer_sides
        ))
    if "classic_three" in fams:
        add_all(expand_classic_three(
            details, menu, prefer, ice_key, int(budget_fen or 0), prefer_sides=prefer_sides
        ))

    # 新品 tags：MCP 无 isNew，用菜单 categories[].meals[].tags（上新/全新…）+ prefs
    tag_new_candidates(out, menu, prefs or {})
    return out


def _matches_family(family: str, tags: set) -> bool:
    if family == "mcard_four":
        return bool(tags & {"mcard_four", "mcard"})
    if family == "dual_1plus1":
        return "dual11" in tags or "dual_1plus1" in tags
    if family == "dabao":
        return "dabao" in tags
    if family == "classic_three":
        return bool(tags & {"classic_three", "three_piece"})
    if family == "coffee_combo":
        return bool(tags & {"coffee", "coffee_combo"})
    return False


def _family_of(cand: Dict[str, Any], families: Sequence[str]) -> str:
    tags = set(cand.get("tags") or [])
    for family in families:
        if _matches_family(family, tags):
            return family
    for family in ("mcard_four", "dual_1plus1", "dabao", "classic_three", "coffee_combo"):
        if _matches_family(family, tags):
            return family
    return "other"


def _deal_anchor(cand: Dict[str, Any]) -> str:
    """分层裁剪用的套餐码。双 1+1 取左腿（items[0]），麦金卡/大堡/三件套取套餐本身。"""
    for item in cand.get("items") or []:
        if isinstance(item, dict) and item.get("productCode"):
            return str(item["productCode"])
    return "main:" + str(cand.get("primaryMainCode") or "")


def _trim_within_key(cand: Dict[str, Any]) -> Tuple[int, int, str]:
    tags = cand.get("tags") or []
    if "classic_three" in tags:
        main = str(cand.get("primaryMainCode") or "")
        bias = 0 if main == "1600" else 1 if main == "1010" else 2
    else:
        bias = 3
    return (bias, int(cand.get("listPriceFen") or 0), str(cand.get("rotationKey") or ""))


def trim_candidates(
    cands: Sequence[Dict[str, Any]],
    max_n: int,
    families: Sequence[str],
) -> List[Dict[str, Any]]:
    """Cap the pool without letting one family, or one dual/mcard SKU, take every seat.

    Classic three still reserves a few unique mains first. The remaining seats
    round-robin across families, and within a family across deal product codes
    (dual left leg, mcard four, dabao, classic). Per-main caps stay as before.
    """
    if max_n <= 0 or not cands:
        return []
    fams = [str(f) for f in families]
    trimmed: List[Dict[str, Any]] = []
    seen = set()
    per_main: Dict[str, int] = {}

    def ident(cand: Dict[str, Any]) -> str:
        return str(cand.get("rotationKey") or cand.get("id") or id(cand))

    def push(cand: Dict[str, Any]) -> None:
        trimmed.append(cand)
        seen.add(ident(cand))
        main = str(cand.get("primaryMainCode") or "")
        per_main[main] = per_main.get(main, 0) + 1

    def over_cap(cand: Dict[str, Any]) -> bool:
        main = str(cand.get("primaryMainCode") or "")
        count = per_main.get(main, 0)
        if count >= 6:
            return True
        if count >= 4 and len(trimmed) >= min(40, max_n):
            return True
        return False

    classics = sorted(
        [c for c in cands if "classic_three" in set(c.get("tags") or [])],
        key=_trim_within_key,
    )
    reserved_mains = set()
    classic_reserved: List[Dict[str, Any]] = []
    for cand in classics:
        if len(classic_reserved) >= 10 or len(trimmed) + len(classic_reserved) >= max_n:
            break
        main = str(cand.get("primaryMainCode") or "")
        if main in reserved_mains and len(classic_reserved) >= 8:
            continue
        if main not in reserved_mains or main in {"1600", "1010"}:
            classic_reserved.append(cand)
            reserved_mains.add(main)
    for cand in classic_reserved:
        if ident(cand) in seen or len(trimmed) >= max_n:
            continue
        push(cand)

    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for cand in cands:
        if ident(cand) in seen:
            continue
        family = _family_of(cand, fams)
        buckets.setdefault(family, {}).setdefault(_deal_anchor(cand), []).append(cand)

    ordered_fams = [family for family in fams if family in buckets]
    for family in buckets:
        if family not in ordered_fams:
            ordered_fams.append(family)
    rows: Dict[str, List[Tuple[str, List[Dict[str, Any]]]]] = {}
    indexes: Dict[str, List[int]] = {}
    pointers: Dict[str, int] = {}
    for family in ordered_fams:
        family_rows = []
        for anchor in sorted(buckets[family]):
            family_rows.append((anchor, sorted(buckets[family][anchor], key=_trim_within_key)))
        rows[family] = family_rows
        indexes[family] = [0] * len(family_rows)
        pointers[family] = 0

    def take_one(family: str) -> bool:
        family_rows = rows[family]
        count = len(family_rows)
        if count == 0:
            return False
        for step in range(count):
            anchor_i = (pointers[family] + step) % count
            bucket = family_rows[anchor_i][1]
            idx = indexes[family][anchor_i]
            while idx < len(bucket):
                cand = bucket[idx]
                idx += 1
                if ident(cand) in seen or over_cap(cand):
                    continue
                indexes[family][anchor_i] = idx
                pointers[family] = (anchor_i + 1) % count
                push(cand)
                return True
            indexes[family][anchor_i] = idx
        return False

    progressed = True
    while len(trimmed) < max_n and progressed:
        progressed = False
        for family in ordered_fams:
            if len(trimmed) >= max_n:
                break
            if take_one(family):
                progressed = True
    return trimmed


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

    trimmed = trim_candidates(cands, int(args.max), families)

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
