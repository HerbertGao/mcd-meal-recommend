#!/usr/bin/env python3
"""McDonald's meal recommender — Phase1 offline scorer (no network / MCP / order).

Supports mealPeriod=lunch|breakfast. Lunch prefs boost 麦金卡四件套 + dual 1+1;
breakfast strongly prefers coffee. Still never orders.

Reads RecommendRequest JSON (stdin or -i), ranks candidates, optionally
rank_then_random Top-K, prints RecommendResponse JSON to stdout.

Candidate sources (first hit wins):
  1. request.candidates[]  — pre-built combos
  2. dual-1+1 enum from mealDetails (multi red-zone mains; + optional menuSnapshot)
  3. templates/fallback-combos.json next to this script
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from budget import apply_quote, budget_check, payable_fen
from mcp_contract import ContractError, rounds_of
from profile_lib import (
    apply_prefs_file,
    append_history_pick,
    load_history_doc,
    load_profile,
    normalize_history_doc,
    resolve_history_path,
    save_history_doc,
)

SCRIPT_DIR = Path(__file__).resolve().parent
FALLBACK_PATH = SCRIPT_DIR / "templates" / "fallback-combos.json"
CST = timezone(timedelta(hours=8))

ICE_KEY_MAP = {
    "no_ice": "0-0",
    "standard": "0-1",
    "extra_ice": "0-2",
    "less_ice": "1-1",
}

# Heuristic code sets (extendable; names also used)
KNOWN_DRINK_CODES = {
    "522021",  # 无糖可乐大
    "3071",  # 无糖可乐中
    "522020",
    "3050",
    "3060",
    "3110",
    "3047",
    "3048",
    "3051",
    "3065",
    "3066",
    "521",
    "522",
    # McCafé / coffee (must win over startswith("50")→side heuristic)
    "507411",
    "507368",
    "507409",
    "507328",
    "507387",
    "507389",
    "510479",
    "510478",
    "520689",
    "520688",
    "521327",
    "514851",
}
KNOWN_MAIN_CODES = {
    "1010",  # 吉士汉堡包
    "1120",  # 双层吉士
    "1450",  # 麦香鸡
    "1100",  # 巨无霸
    "1101",
    "1106",  # 培根蔬萃双层牛堡
    "1360",
    # 1401 麦乐鸡 = side only（不得作主食/主堡）
    "1406",  # 板烧
    "1440",  # 麦辣鸡腿堡
    "1500",
    "1600",  # 麦香鱼
    "4648",  # 不素之霸
    "515295",  # 双层脆鸡堡
    "521147",  # Hold不住鸡排堡
    "522016",  # 海陆双全堡
    "522032",  # 酥酥多笋卷
    "520312",  # 其它卷/堡（菜单出现时）
    "8317",
    # breakfast 麦满分 / 卷 / 炒双蛋（预约菜单短码+长码）
    "5888",  # 吉士蛋麦满分（短）
    "5889",  # 大脆鸡扒麦满分（短）
    "1254",  # 火腿扒麦满分（短）
    "6800",  # 原味板烧鸡腿麦满分（短）
    "905888",
    "905889",
    "901254",
    "906800",
    "905024",  # 猪柳麦满分
    "905022",  # 猪柳蛋麦满分
    "520830",  # 双层猪柳蛋麦满分
    "904989",  # 双层原味板烧鸡腿麦满分
    "521315",  # 芝芝火腿扒堡
    "902322",  # 火腿扒早安营养卷
    "902323",  # 图林根香肠早安营养卷
    "902902",  # 原味板烧鸡腿炒双蛋堡
    "900621",  # 猪柳炒双蛋堡
}
KNOWN_SIDE_OR_CHICKEN = {
    "1401",  # 麦乐鸡5块 — 小食/鸡块，禁止当主食
    "505416",  # 琵琶腿（炸鸡小食，可作配，不作主堡）
    "521156",  # 那么大鸡排（同上）
    "4800",  # 小薯条
    "4810",  # 中薯条
    "4820",  # 大薯条
    "514782",  # 脆脆薯条
    "4105",  # 菠萝派
    "4106",  # 香芋派
    "4437",  # 小杯玉米杯
    "4438",  # 大杯玉米杯
    "4420",  # 迷你草莓新地
    "4422",  # 迷你朱古力新地
    "4070",
    "4071",
    "400",
    "401",
    "402",
    "403",
    "5000",
    "5050",
    # breakfast sides
    "904825",  # 脆薯饼
    "902631",  # 脆香油条
    "900479",  # 德式图林根香肠
    "904437",  # 甜香玉米杯
}

# 绝不可作为「主食/主堡」的 code（鸡块类）；硬过滤用
FORBIDDEN_AS_MAIN_CODES = {
    "1401",  # 麦乐鸡
}

# 早餐菜单短码↔长码（同款麦满分）；历史/未见抬权按 canonical 计
MAIN_CODE_ALIASES = {
    "5888": "905888",
    "905888": "905888",
    "5889": "905889",
    "905889": "905889",
    "1254": "901254",
    "901254": "901254",
    "6800": "906800",
    "906800": "906800",
}


def canonical_main_code(code: str) -> str:
    c = str(code or "")
    return MAIN_CODE_ALIASES.get(c, c)


# 鱼牛四件套少见主堡：进池但常被更便宜的人气超值挤出 Top-K；软抬 novelty
RARE_MAIN_CODES = {
    "520312",  # 原味双层深海鳕鱼堡
    "8317",  # 风味双层深海鳕鱼堡
    "4648",  # 不素之霸双层牛堡
}

DRINK_NAME_RE = re.compile(
    r"(可乐|雪碧|芬达|汽水|橙汁|豆浆|咖啡|奶茶|柠檬水|饮|茶(?!叶))",
    re.I,
)
MAIN_NAME_RE = re.compile(
    r"(堡|汉堡|吉士|麦香鸡|板烧|巨无霸|双层|卷|鱼堡|安格斯|麦香鱼|麦满分|炒双蛋|营养卷|火腿扒|猪柳)",
    re.I,
)
SIDE_NAME_RE = re.compile(
    r"(薯条|派|鸡翅|鸡腿|琵琶|麦乐鸡|鸡块|小食|玉米|沙拉|脆汁鸡|麦辣鸡翅|薯饼|油条|香肠)",
    re.I,
)
DEAL_NAME_RE = re.compile(r"(1\+1|随心配|大堡口福|超值|三件套|精选单人|套餐)", re.I)
NEW_NAME_RE = re.compile(r"(上新|全新|新品|新升级|全新升级|首发|限定)", re.I)
FOUR_NAME_RE = re.compile(r"四件套")
MCARD_NAME_RE = re.compile(r"(麦金卡|金卡专享)")
COFFEE_NAME_RE = re.compile(
    r"(咖啡|拿铁|美式|卡布|奶铁|燕麦奶铁|浓缩|鲜萃冰咖|鲜萃咖啡|阿芙佳朵)",
    re.I,
)

# 麦金卡专享四件套商品码（取自一次门店快照；下单可能需会员身份）
MCARD_FOUR_CODES = {
    "9900016629",  # 海陆双全堡四件套
    "9900010110",  # 鱼牛汉堡四件套随心选
    "9900006957",  # 安格斯厚牛堡四件套随心选
    "9900004236",  # 人气超值四件套随心选
    "9900000884",  # 酥酥多笋卷四件套
}
DUAL11_PRODUCT_CODES = {
    "9900016655",  # 双吉随心配1+1
    "9900016651",  # 三件套随心配1+1
    "9900016668",  # 真有料随心配1+1
}
COFFEE_CODES = {
    "507411",  # 冰美式
    "507368",  # 热美式
    "507409",  # 冰奶铁
    "507328",  # 热奶铁 / 任选
    "507387",  # 卡布奇诺
    "507389",  # 浓缩
    "510479",  # 燕麦奶铁
    "510478",  # 热燕麦奶铁
    "520689",  # 鲜萃咖啡
    "520688",  # 大杯鲜萃咖啡
    "521327",  # 鲜萃冰咖
    "514851",  # 咖啡阿芙佳朵
}

# Soft-score base caps (reweighted by prefs.priority)
BASE_CAPS = {
    "deal": 22.0,
    "satiety": 20.0,
    "novelty": 18.0,  # 新品/上新抬权（菜单 tags + 启发式）
    "low_repeat": 16.0,
    "drink": 8.0,
    "mcard_four": 18.0,
    "dual_1plus1": 16.0,
    "classic_three": 12.0,  # below mcard/dual; diversifyByFamily still lifts into Top-K
    "coffee": 14.0,
}

# Empty-priority fallback only — prefer profiles/*.json prefs.priority.
DEFAULT_PRIORITY_BY_PERIOD = {
    "lunch": [
        "mcard_four",
        "dual_1plus1",
        "satiety",
        "novelty",
        "low_repeat",
        "deal",
        "classic_three",
        "drink",
    ],
    "breakfast": [
        "coffee",
        "satiety",
        "deal",
        "low_repeat",
        "drink",
    ],
}


@dataclass
class ScoredCandidate:
    rotation_key: str
    human_summary: str
    price_fen: int
    items: List[Dict[str, Any]]
    tags: List[str] = field(default_factory=list)
    price_verified: bool = False
    payable_fen: Optional[int] = None
    source: str = "candidates"
    score: float = 0.0
    breakdown: Dict[str, float] = field(default_factory=dict)
    hard_fail: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def codes_flat(self) -> List[str]:
        out: List[str] = []
        for it in self.items:
            for rnd in it.get("roundList") or []:
                for ci in rnd.get("comboItemList") or []:
                    code = str(ci.get("code", ""))
                    if code:
                        out.append(code)
        return out

    def product_names(self) -> List[str]:
        return [str(it.get("productName") or "") for it in self.items]

    def product_codes(self) -> List[str]:
        return [str(it.get("productCode") or "") for it in self.items]


def now_cst_iso() -> str:
    return datetime.now(CST).replace(microsecond=0).isoformat()


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_request(args: argparse.Namespace) -> Dict[str, Any]:
    if args.input:
        data = load_json(Path(args.input))
    else:
        raw = sys.stdin.read()
        if not raw.strip():
            raise SystemExit("empty stdin; pass -i request.json or pipe JSON")
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("request root must be object")
    return data


def ice_key_from_prefs(drink: Dict[str, Any]) -> str:
    if drink.get("iceKey"):
        return str(drink["iceKey"])
    ice = str(drink.get("ice") or "standard")
    return ICE_KEY_MAP.get(ice, "0-1")


def classify_code(code: str, name_hint: str = "") -> str:
    """Return 'drink' | 'main' | 'side' | 'other'.

    Code tables / numeric heuristics win over name_hint so that a fries code
    inside a burger combo is not mislabeled main just because productName
    contains 「堡」.
    麦乐鸡等鸡块：永远是 side，不得当 main。
    """
    c = str(code)
    if c in KNOWN_DRINK_CODES or c in COFFEE_CODES:
        return "drink"
    # nuggets / 鸡块 first — never main
    if c in FORBIDDEN_AS_MAIN_CODES:
        return "side"
    if c in KNOWN_SIDE_OR_CHICKEN:
        return "side"
    if c in KNOWN_MAIN_CODES:
        return "main"
    # pies / fries / chicken buckets by code prefix
    if c.startswith("41") and len(c) <= 5:
        return "side"
    if c.startswith("48"):
        return "side"
    # 50xxxx: sides/chicken — but not coffee (handled above)
    if c.startswith("50") and c not in KNOWN_MAIN_CODES and c not in COFFEE_CODES:
        return "side"
    hint = name_hint or ""
    if hint:
        if DRINK_NAME_RE.search(hint):
            return "drink"
        if SIDE_NAME_RE.search(hint):
            return "side"
        if MAIN_NAME_RE.search(hint):
            return "main"
    return "other"


def iter_combo_items(items: Sequence[Dict[str, Any]]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    for it in items:
        for rnd in it.get("roundList") or []:
            for ci in rnd.get("comboItemList") or []:
                yield str(ci.get("code", "")), ci


def count_roles(cand: ScoredCandidate) -> Dict[str, int]:
    roles = {"main": 0, "side": 0, "drink": 0, "other": 0}
    for code, ci in iter_combo_items(cand.items):
        # Use item-level name only (never whole productName) to avoid
        # 「巨无霸大堡口福」把薯条判成 main。
        item_name = str(
            (ci or {}).get("name")
            or (ci or {}).get("productName")
            or ""
        )
        role = classify_code(code, item_name)
        roles[role] = roles.get(role, 0) + 1
    return roles


def drink_codes_in(cand: ScoredCandidate) -> List[str]:
    out = []
    for code, _ in iter_combo_items(cand.items):
        if classify_code(code) == "drink" or code in KNOWN_DRINK_CODES:
            out.append(code)
    return out


def ice_keys_in(cand: ScoredCandidate) -> List[str]:
    keys = []
    ice_code = "200002"
    for _code, ci in iter_combo_items(cand.items):
        mod = ci.get("modification") or {}
        for v in mod.get("values") or []:
            if str(v.get("code")) == ice_code or str(v.get("code")) == "200002":
                keys.append(str(v.get("key")))
    return keys



def _nugget_as_hero_main(cand: ScoredCandidate) -> Optional[str]:
    """If dual/combo uses 麦乐鸡(or forbidden) as the first-item red-zone pick, reject.

    麦乐鸡可作小食出现在白区/四件套小食槽；不可作「主堡」占红区左腿。
    """
    if not cand.items:
        return None
    first = cand.items[0]
    rounds = first.get("roundList") or []
    if not rounds:
        return None
    # first round ≈ 红区 / 主食槽
    for ci in rounds[0].get("comboItemList") or []:
        code = str(ci.get("code") or "")
        name = str(ci.get("name") or ci.get("productName") or "")
        if code in FORBIDDEN_AS_MAIN_CODES:
            return code
        if name and ("麦乐鸡" in name or "鸡块" in name) and "堡" not in name:
            return code or name
    # also: declared primaryMainCode is nugget
    pmc = str((cand.meta or {}).get("primaryMainCode") or "")
    if pmc in FORBIDDEN_AS_MAIN_CODES:
        return pmc
    return None


def hard_filter(
    cand: ScoredCandidate,
    req: Dict[str, Any],
) -> Optional[str]:
    reason = budget_check(cand, req)
    if reason == "unverified_delivery_total":
        # Fees may still change the total; require a quote before confirming.
        cand.meta["needsQuoteBeforeConfirm"] = True
    elif reason:
        return reason

    prefs = req.get("prefs") or {}
    satiety_mode = str(prefs.get("satiety") or "high")
    satiety_cfg = req.get("satiety") or {}
    require_main = satiety_cfg.get("requireMain", True)
    require_side = satiety_cfg.get("requireSideOrChicken", True)
    require_drink = satiety_cfg.get("requireDrink", True)
    forbid_thin = satiety_cfg.get("forbidDrinkOnlyCombo", True)

    if satiety_mode != "off":
        roles = count_roles(cand)
        if require_main and roles["main"] < 1:
            return "satiety_missing_main"
        if require_side and roles["side"] < 1:
            return "satiety_missing_side_or_chicken"
        if require_drink and roles["drink"] < 1:
            return "satiety_missing_drink"
        # 麦乐鸡等不得充当唯一/英雄主食（双1+1 红区左腿）
        nugget_hero = _nugget_as_hero_main(cand)
        if nugget_hero:
            return f"nugget_forbidden_as_main:{nugget_hero}"
        if forbid_thin:
            # 「仅双吉+可乐」：有主食+饮品但无小食/炸鸡
            if roles["main"] >= 1 and roles["drink"] >= 1 and roles["side"] < 1:
                return "forbid_drink_only_combo"
            # single skinny combo (only one product line and no side)
            if len(cand.items) == 1 and roles["side"] < 1:
                return "forbid_thin_single_combo"

    drinks = drink_codes_in(cand)
    if len(drinks) > 1:
        return f"white_zone_drink_dup:{drinks}"

    # breakfast: coffee required by default
    if meal_period(req) == "breakfast":
        prefs = req.get("prefs") or {}
        require_coffee = prefs.get("requireCoffee")
        if require_coffee is None:
            require_coffee = True
        if require_coffee and not has_coffee(cand):
            return "breakfast_missing_coffee"

    return None


def recent_rotation_keys(req: Dict[str, Any]) -> set:
    window = int(req.get("historyWindowDays", 5))
    hist = req.get("history") or []
    if window <= 0 or not hist:
        return set()
    today = date.today()
    keys = set()
    for h in hist:
        try:
            d = date.fromisoformat(str(h.get("date")))
        except Exception:
            keys.add(str(h.get("rotationKey")))
            continue
        if (today - d).days <= window:
            keys.add(str(h.get("rotationKey")))
    return keys


def primary_main_code(cand: ScoredCandidate) -> str:
    """Best-effort 'hero' main for diversity: first main-classified combo code."""
    for code, ci in iter_combo_items(cand.items):
        name = str((ci or {}).get("name") or (ci or {}).get("productName") or "")
        if classify_code(code, name) == "main":
            return canonical_main_code(code)
    # fallback: first round first code of first item
    for it in cand.items:
        for rnd in it.get("roundList") or []:
            for ci in rnd.get("comboItemList") or []:
                code = str(ci.get("code") or "")
                if code:
                    return code
    return ""


def codes_by_role(cand: ScoredCandidate, role: str) -> List[Tuple[str, str]]:
    """All (code, name) for a role in combo order; deduped."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for code, ci in iter_combo_items(cand.items):
        name = str((ci or {}).get("name") or (ci or {}).get("productName") or "")
        if classify_code(code, name) != role:
            continue
        c = str(code)
        if not c or c in seen:
            continue
        seen.add(c)
        out.append((c, name))
    return out


def primary_side_code(cand: ScoredCandidate) -> str:
    """First side-classified combo code (薯条/鸡块/派/新地等)."""
    for code, ci in iter_combo_items(cand.items):
        name = str((ci or {}).get("name") or (ci or {}).get("productName") or "")
        if classify_code(code, name) == "side":
            return str(code)
    return ""


def _code_display_name(code: str, cand: Optional[ScoredCandidate] = None) -> str:
    if cand is not None:
        for c, ci in iter_combo_items(cand.items):
            if str(c) != str(code):
                continue
            name = str((ci or {}).get("name") or (ci or {}).get("productName") or "")
            if name:
                return name
    return ""


def _track_from_req(req: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    tracks = req.get("historyTracks") or {}
    if isinstance(tracks, dict) and tracks.get(key):
        return [x for x in tracks[key] if isinstance(x, dict)]
    # derive from flat entries
    hist = req.get("history") or []
    out: List[Dict[str, Any]] = []
    field = "mainCode" if key == "mains" else "sideCode"
    name_field = "mainName" if key == "mains" else "sideName"
    for h in hist:
        if not isinstance(h, dict):
            continue
        code = str(h.get(field) or "")
        if not code and key == "mains":
            # legacy parse rotationKey for main only
            rk = str(h.get("rotationKey") or "")
            m = re.search(r"(?:sj|t)(\d{3,6})", rk)
            if m:
                code = m.group(1)
        if not code:
            continue
        out.append(
            {
                "code": code,
                "name": str(h.get(name_field) or ""),
                "at": str(h.get("at") or h.get("date") or ""),
                "rotationKey": str(h.get("rotationKey") or ""),
            }
        )
    return out


def _parse_at_date(raw: str) -> Optional[date]:
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def recent_track_codes(
    req: Dict[str, Any],
    track_key: str,
) -> List[Tuple[str, int]]:
    """Recent codes on one track as [(code, age_index)], age 0 = most recent meal.

    Window defaults:
      - historyWindowPicks=5 → last N *meals* (grouped by at / seed+rotationKey)
      - historyWindowDays=5 (drop older by calendar day when at/date present)
    One meal may append several codes (dual 1+1); they share the same age.
    Tracks are independent: mains never affect sides and vice versa.
    """
    picks_n = int(req.get("historyWindowPicks", 5))
    days_n = int(req.get("historyWindowDays", 5))
    track = _track_from_req(req, track_key)
    if picks_n <= 0 or not track:
        return []
    today = date.today()
    filtered: List[Dict[str, Any]] = []
    for h in track:
        d = _parse_at_date(str(h.get("at") or ""))
        if d is not None and days_n >= 0 and (today - d).days > days_n:
            continue
        filtered.append(h)

    def meal_key(h: Dict[str, Any]) -> Tuple[str, str, str]:
        return (
            str(h.get("at") or ""),
            str(h.get("seed") if h.get("seed") is not None else ""),
            str(h.get("rotationKey") or ""),
        )

    meals: List[List[Dict[str, Any]]] = []
    keys: List[Tuple[str, str, str]] = []
    for h in filtered:
        k = meal_key(h)
        if meals and k == keys[-1] and any(k):
            meals[-1].append(h)
        else:
            meals.append([h])
            keys.append(k)
    recent_meals = list(reversed(meals[-picks_n:]))
    out: List[Tuple[str, int]] = []
    for age, meal in enumerate(recent_meals):
        seen = set()
        for h in meal:
            code = str(h.get("code") or "")
            if code and code not in seen:
                seen.add(code)
                out.append((code, age))
    return out


def recent_main_codes(req: Dict[str, Any]) -> set:
    """Main codes seen in recent history (track or legacy entries)."""
    return {c for c, _ in recent_track_codes(req, "mains")}


def recent_side_codes(req: Dict[str, Any]) -> set:
    return {c for c, _ in recent_track_codes(req, "sides")}


def _recency_multiplier(code: str, recent: List[Tuple[str, int]], *, floor: float, ceiling: float) -> float:
    """Return score multiplier in [floor, 1.0]. Missing code → 1.0.

    age 0 (just picked) → floor; age at window edge → closer to ceiling.
    """
    if not code or not recent:
        return 1.0
    hits = [age for c, age in recent if c == code]
    if not hits:
        return 1.0
    age = min(hits)
    span = max(1, max(a for _, a in recent))
    # linear from floor (age0) toward ceiling (oldest in window)
    t = age / span
    return floor + (ceiling - floor) * t


def priority_weights(priority: Sequence[str]) -> Dict[str, float]:
    """Reweight BASE_CAPS so earlier prefs.priority get larger share; sum≈100."""
    dims = [
        "deal",
        "satiety",
        "novelty",
        "low_repeat",
        "drink",
        "mcard_four",
        "dual_1plus1",
        "classic_three",
        "coffee",
    ]
    # start from base
    caps = dict(BASE_CAPS)
    if not priority:
        return caps
    # rank boost: first gets *1.35, then 1.15, 1.0, 0.85, 0.7 for listed order
    boosts = [1.35, 1.15, 1.0, 0.85, 0.7]
    listed = [p for p in priority if p in caps]
    unlisted = [d for d in dims if d not in listed]
    scale = {d: 0.75 for d in unlisted}
    for i, p in enumerate(listed):
        scale[p] = boosts[min(i, len(boosts) - 1)]
    weighted = {d: caps[d] * scale.get(d, 1.0) for d in dims}
    total = sum(weighted.values()) or 1.0
    # normalize to 100
    return {d: round(100.0 * weighted[d] / total, 4) for d in dims}



def meal_period(req: Dict[str, Any]) -> str:
    mp = str(req.get("mealPeriod") or (req.get("prefs") or {}).get("mealPeriod") or "lunch").lower()
    return "breakfast" if mp in ("breakfast", "bf", "早餐") else "lunch"


def effective_priority(req: Dict[str, Any]) -> List[str]:
    prefs = req.get("prefs") or {}
    pri = list(prefs.get("priority") or [])
    if pri:
        return pri
    return list(DEFAULT_PRIORITY_BY_PERIOD.get(meal_period(req), DEFAULT_PRIORITY_BY_PERIOD["lunch"]))


def is_mcard_four(cand: ScoredCandidate) -> bool:
    tags = set(cand.tags or [])
    if tags & {"mcard_four", "mcard", "four_piece"}:
        # require four-piece-ish if only mcard tag
        if "mcard_four" in tags or "four_piece" in tags:
            return True
    pcodes = set(cand.product_codes())
    if pcodes & MCARD_FOUR_CODES:
        return True
    names = " ".join(cand.product_names() + [cand.human_summary])
    if FOUR_NAME_RE.search(names) and (MCARD_NAME_RE.search(names) or "mcard" in tags):
        return True
    # 四件套 alone + known code already handled; name-only 四件套 in 麦金卡 category via meta
    if cand.meta.get("mcardFour"):
        return True
    if FOUR_NAME_RE.search(names) and any(c in MCARD_FOUR_CODES for c in pcodes):
        return True
    return False


def is_dual_1plus1(cand: ScoredCandidate) -> bool:
    tags = set(cand.tags or [])
    if tags & {"dual11", "dual_1plus1", "1+1"}:
        return True
    pcodes = [str(x) for x in cand.product_codes()]
    if sum(1 for c in pcodes if c in DUAL11_PRODUCT_CODES) >= 2:
        return True
    names = " ".join(cand.product_names() + [cand.human_summary])
    if names.count("1+1") >= 2 or names.count("随心配") >= 2:
        return True
    return False


def coffee_hits(cand: ScoredCandidate) -> List[str]:
    hits: List[str] = []
    for code, ci in iter_combo_items(cand.items):
        name = str((ci or {}).get("name") or (ci or {}).get("productName") or "")
        if code in COFFEE_CODES or COFFEE_NAME_RE.search(name):
            hits.append(code or name)
    # also product-level coffee SKUs (standalone)
    for it in cand.items:
        code = str(it.get("productCode") or "")
        name = str(it.get("productName") or "")
        if code in COFFEE_CODES or COFFEE_NAME_RE.search(name):
            hits.append(code or name)
    # dedupe preserve order
    seen = set()
    out = []
    for h in hits:
        if h in seen:
            continue
        seen.add(h)
        out.append(h)
    return out



_FAMILY_PREFIXES = (
    ("mk", "mcard_four"),
    ("sj", "dual_1plus1"),
    ("db", "dabao"),
    ("ct", "classic_three"),
    ("bf", "coffee_combo"),
)


def family_of_rotation_key(rk: str) -> str:
    """Map a stored rotationKey to its family (history keeps no tags)."""
    key = str(rk or "")
    for prefix, family in _FAMILY_PREFIXES:
        if key.startswith(prefix):
            return family
    return "other"


def recent_meal_families(req: Dict[str, Any], limit: int = 3) -> List[str]:
    """Families of the most recent meals, newest first.

    History is appended chronologically, so insertion order is the reliable
    ordering; timestamps can tie (several picks recorded in one run).
    """
    hist = req.get("history") or []
    if not isinstance(hist, list) or not hist:
        return []
    out: List[str] = []
    seen = set()
    for h in reversed(hist):
        if not isinstance(h, dict):
            continue
        key = (str(h.get("at") or h.get("date") or ""), str(h.get("rotationKey") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(family_of_rotation_key(str(h.get("rotationKey") or "")))
        if len(out) >= max(1, limit):
            break
    return out


def apply_family_rotation(cands: List[ScoredCandidate], req: Dict[str, Any]) -> None:
    """Make the most recently eaten family yield the seat to the others.

    Scoring alone cannot rotate families: a stronger family keeps winning even
    when its repeat dimension is penalised, because that dimension is only a
    small share of the total. The factor is therefore applied to the total score
    before ranking.
    """
    prefs = req.get("prefs") or {}
    try:
        factor = float(prefs.get("familyRepeatPenalty", 0.55))
    except (TypeError, ValueError):
        factor = 0.55
    factor = max(0.05, min(1.0, factor))
    if factor >= 1.0:
        return
    past_fams = recent_meal_families(req, limit=2)
    if not past_fams:
        return
    for cand in cands:
        fam = candidate_family(cand)
        if fam == "other" or fam not in past_fams:
            continue
        age = past_fams.index(fam)
        applied = factor if age == 0 else factor ** 0.5
        cand.score *= applied
        cand.meta["familyRotationFactor"] = round(applied, 3)


def candidate_family(cand: ScoredCandidate) -> str:
    tags = set(cand.tags or [])
    if tags & {"mcard_four", "mcard", "four_piece"} or is_mcard_four(cand):
        return "mcard_four"
    if tags & {"dual11", "dual_1plus1"} or is_dual_1plus1(cand):
        return "dual_1plus1"
    if tags & {"classic_three", "three_piece"} or (cand.rotation_key or "").startswith("ct"):
        return "classic_three"
    if tags & {"coffee_combo"}:
        return "coffee_combo"
    if tags & {"dabao"} or (cand.rotation_key or "").startswith("db"):
        return "dabao"
    if tags & {"coffee"} or has_coffee(cand):
        return "coffee_combo"
    return "other"

def has_coffee(cand: ScoredCandidate) -> bool:
    if coffee_hits(cand):
        return True
    tags = set(cand.tags or [])
    return bool(tags & {"coffee", "coffee_combo"})



DEFAULT_NEW_TAG_RE = re.compile(r"(上新|全新|新升级|新品|首发)", re.I)


def _menu_path_from_req(req: Dict[str, Any]) -> Optional[Path]:
    paths = req.get("_paths") or {}
    raw = paths.get("menuSlim")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_absolute() else SCRIPT_DIR / p


def build_menu_new_index(req: Dict[str, Any]) -> Dict[str, Any]:
    """Collect product/meal codes marked new via menu tags or profile prefs.

    MCP query-meals has no isNew boolean — only categories[].meals[].tags
    (e.g. 上新 / 全新升级). Detail API likewise has no new field.
    """
    cached = req.get("_newIndex")
    if isinstance(cached, dict) and cached.get("_ok"):
        return cached
    prefs = req.get("prefs") or {}
    tag_rx_raw = prefs.get("newTagRegex") or ""
    try:
        tag_rx = re.compile(str(tag_rx_raw), re.I) if tag_rx_raw else DEFAULT_NEW_TAG_RE
    except re.error:
        tag_rx = DEFAULT_NEW_TAG_RE
    # Profile aliases: newItemCodes == newProductCodes (config in profile, not Skill)
    codes: set = set(str(x) for x in (prefs.get("newProductCodes") or []) if x)
    codes |= set(str(x) for x in (prefs.get("newItemCodes") or []) if x)
    codes |= set(str(x) for x in (prefs.get("newMainCodes") or []) if x)
    code_tags: Dict[str, List[str]] = {}

    def note(code: str, tags: List[str]):
        c = str(code or "")
        if not c:
            return
        code_tags.setdefault(c, [])
        for t in tags:
            if t not in code_tags[c]:
                code_tags[c].append(t)
        blob = " ".join(str(t) for t in tags)
        if tag_rx.search(blob):
            codes.add(c)

    menu = req.get("menuSnapshot")
    if not isinstance(menu, dict):
        mp = _menu_path_from_req(req)
        if mp and mp.is_file():
            try:
                menu = load_json(mp)
            except Exception:
                menu = None
    if isinstance(menu, dict):
        for cat in menu.get("categories") or []:
            for m in cat.get("meals") or []:
                note(str(m.get("code") or ""), list(m.get("tags") or []))
        # mealIndex may lack tags; still ok
    idx = {"_ok": True, "codes": codes, "codeTags": code_tags, "tagRegex": tag_rx.pattern}
    req["_newIndex"] = idx
    return idx


def candidate_new_signals(cand: ScoredCandidate, req: Dict[str, Any]) -> List[str]:
    """Why this combo counts as 新品; empty = not new."""
    prefs = req.get("prefs") or {}
    idx = build_menu_new_index(req)
    new_codes = idx.get("codes") or set()
    hits: List[str] = []
    names = " ".join(cand.product_names() + [cand.human_summary])
    tags = set(cand.tags or [])
    if "new" in tags or "上新" in tags:
        hits.append("cand_tag:new")
    # name heuristics: built-in NEW_NAME_RE + profile newItemNamePatterns
    if NEW_NAME_RE.search(names):
        hits.append("name")
    for pat in prefs.get("newItemNamePatterns") or []:
        try:
            if re.search(str(pat), names, re.I):
                hits.append(f"namePat:{pat}")
        except re.error:
            continue
    pmc = str(cand.meta.get("primaryMainCode") or primary_main_code(cand) or "")
    if pmc and pmc in new_codes:
        hits.append(f"main:{pmc}")
    for pc in cand.product_codes():
        if pc in new_codes:
            hits.append(f"product:{pc}")
    for code in cand.codes_flat():
        if code in new_codes:
            hits.append(f"item:{code}")
            break
    # profile explicit lists already folded into new_codes
    # dedupe preserve order
    seen = set()
    out = []
    for h in hits:
        if h in seen:
            continue
        seen.add(h)
        out.append(h)
    return out


def score_candidate(cand: ScoredCandidate, req: Dict[str, Any]) -> None:
    prefs = req.get("prefs") or {}
    drink_prefs = prefs.get("drink") or {}
    weights = priority_weights(effective_priority(req))
    bd: Dict[str, float] = {}

    names = " ".join(cand.product_names() + [cand.human_summary])
    tags = set(cand.tags or [])
    roles = count_roles(cand)
    codes = cand.codes_flat()
    pcodes = cand.product_codes()
    mcard = is_mcard_four(cand)
    dual = is_dual_1plus1(cand)
    coffee_list = coffee_hits(cand)
    cand.meta["mcardFour"] = mcard
    cand.meta["dual1plus1"] = dual
    cand.meta["coffeeHits"] = coffee_list
    cand.meta["mealPeriod"] = meal_period(req)

    # --- deal ---
    deal_cap = weights["deal"]
    deal = 0.0
    if DEAL_NAME_RE.search(names) or any("1+1" in t or "deal" in t for t in tags):
        deal += deal_cap * 0.55
    # cheaper within budget gets more
    budget = float(req.get("budgetFen") or 3500)
    if budget > 0 and cand.price_fen > 0:
        ratio = cand.price_fen / budget
        # flatter than 0.35: ¥28 vs ¥32 within budget should not alone bury fish-beef
        deal += deal_cap * 0.18 * max(0.0, 1.0 - ratio)  # lower price → mild boost
    # more filled items under budget
    n_items = len(cand.items)
    if n_items >= 2:
        deal += deal_cap * 0.10
    bd["deal"] = round(min(deal_cap, deal), 2)

    # --- satiety ---
    sat_cap = weights["satiety"]
    sat_mode = str(prefs.get("satiety") or "high")
    sat = 0.0
    if sat_mode == "off":
        sat = sat_cap * 0.5
    else:
        # main+side+drink already hard-passed
        sat += sat_cap * 0.45
        if roles["main"] >= 1:
            sat += sat_cap * 0.15
        if roles["side"] >= 1:
            sat += sat_cap * 0.20
        if roles["side"] >= 2 or (roles["main"] >= 1 and roles["side"] >= 1 and n_items >= 2):
            sat += sat_cap * 0.15
        if sat_mode == "high":
            sat = min(sat_cap, sat * 1.05)
        if "satiety" in tags:
            sat = min(sat_cap, sat + sat_cap * 0.05)
    bd["satiety"] = round(min(sat_cap, sat), 2)
    # 麦乐鸡仅作蛋白配时软降（主食已是汉堡则仍可通过硬门槛）
    if any(c in FORBIDDEN_AS_MAIN_CODES for c in codes):
        bd["satiety"] = round(max(0.0, bd["satiety"] - sat_cap * 0.12), 2)
        sat = bd["satiety"]

    # --- novelty / 新品 ---
    # MCP 无 isNew 字段；依据菜单 tags（上新/全新…）+ 名称启发式 + profile new*Codes。
    nov_cap = weights["novelty"]
    nov = 0.0
    new_hits = candidate_new_signals(cand, req)
    cand.meta["newSignals"] = new_hits
    prefer_new = bool(prefs.get("preferNewItems", True))
    # prefs.newItemBoost: multiplier on novelty for detected 新品 (profile-configurable)
    try:
        boost = float(prefs.get("newItemBoost") or 1.0)
    except (TypeError, ValueError):
        boost = 1.0
    boost = max(0.5, min(2.5, boost))
    if new_hits:
        # 明确新品：高抬，便于进 Top-K
        base_frac = 0.92 if prefer_new else 0.78
        nov = min(nov_cap, nov_cap * base_frac * (boost if prefer_new else 1.0))
        cand.meta["newItemBoost"] = boost if prefer_new else 1.0
        if "new" not in tags:
            cand.tags = list(cand.tags or []) + ["new"]
            tags.add("new")
    elif prefer_new:
        recent = recent_rotation_keys(req)
        nov = nov_cap * (0.35 if cand.rotation_key not in recent else 0.15)
    else:
        nov = nov_cap * 0.25
    # 少见主堡（鳕鱼/不素之霸等）：轻度抬升，便于进入 diversify Top-K
    main_for_nov = primary_main_code(cand)
    cand.meta["primaryMainCode"] = canonical_main_code(main_for_nov or cand.meta.get("primaryMainCode") or "")
    if main_for_nov in RARE_MAIN_CODES:
        nov = min(nov_cap, nov + nov_cap * 0.22)
    # 主食轨刚吃过同一新品主堡时，软压 novelty（不碰小食轨），避免同一上新主连刷
    main_recent = recent_track_codes(req, "mains")
    main_mult_nov = _recency_multiplier(main_for_nov, main_recent, floor=0.35, ceiling=0.85)
    if new_hits and main_mult_nov < 0.999:
        nov = nov * (0.55 + 0.45 * main_mult_nov)
    bd["novelty"] = round(min(nov_cap, max(0.0, nov)), 2)

    # --- low_repeat (main track + side track, independent) ---
    # Defaults: last historyWindowPicks=5 on each track, filtered by historyWindowDays=5.
    # Same rotationKey → 0; same main recent → strong cut; same side recent → milder cut.
    # Symmetric unseen boost: code absent from the SAME meal-window → soft lift
    # (only when that track's window is non-empty; empty history stays neutral so
    # every candidate does not get an identical bump). Seen-penalty and unseen-boost
    # are mutually exclusive per code; mains/sides never cross.
    # Soft only — never overrides budget / satiety hard filters. Stacks with novelty
    # via separate dims; within this dim, prefs.unseenComboCap caps the lift.
    lr_cap = weights["low_repeat"]
    lr = lr_cap
    main_code = primary_main_code(cand)
    side_code = primary_side_code(cand)
    cand.meta["primaryMainCode"] = canonical_main_code(main_code)
    cand.meta["primarySideCode"] = side_code
    try:
        unseen_main_boost = float(prefs.get("unseenMainBoost") or 1.22)
    except (TypeError, ValueError):
        unseen_main_boost = 1.22
    try:
        unseen_side_boost = float(prefs.get("unseenSideBoost") or 1.12)
    except (TypeError, ValueError):
        unseen_side_boost = 1.12
    try:
        unseen_combo_cap = float(prefs.get("unseenComboCap") or 1.32)
    except (TypeError, ValueError):
        unseen_combo_cap = 1.32
    unseen_main_boost = max(1.0, min(1.5, unseen_main_boost))
    unseen_side_boost = max(1.0, min(1.4, unseen_side_boost))
    unseen_combo_cap = max(1.0, min(1.6, unseen_combo_cap))
    cand.meta["unseenMain"] = False
    cand.meta["unseenSide"] = False
    lr_ceiling = lr_cap
    if prefs.get("lowRecentRepeat", True):
        recent = recent_rotation_keys(req)
        main_recent = recent_track_codes(req, "mains")
        side_recent = recent_track_codes(req, "sides")
        if cand.rotation_key in recent:
            lr = 0.0
        else:
            # soft penalty if same pairing prefix (before _)
            prefix = cand.rotation_key.split("_")[0]
            for rk in recent:
                if rk.split("_")[0] == prefix:
                    lr = lr_cap * 0.55
                    break
            main_mult = _recency_multiplier(main_code, main_recent, floor=0.12, ceiling=0.55)
            side_mult = _recency_multiplier(side_code, side_recent, floor=0.28, ceiling=0.80)
            lr = lr * main_mult * side_mult
            cand.meta["repeatMainMult"] = round(main_mult, 3)
            cand.meta["repeatSideMult"] = round(side_mult, 3)
            # Unseen lift: only when the track window has meals (else neutral).
            main_seen = {c for c, _ in main_recent}
            side_seen = {c for c, _ in side_recent}
            if main_recent and main_code and main_code not in main_seen:
                lr *= unseen_main_boost
                cand.meta["unseenMain"] = True
                cand.meta["unseenMainBoost"] = round(unseen_main_boost, 3)
            if side_recent and side_code and side_code not in side_seen:
                lr *= unseen_side_boost
                cand.meta["unseenSide"] = True
                cand.meta["unseenSideBoost"] = round(unseen_side_boost, 3)
            if cand.meta["unseenMain"] or cand.meta["unseenSide"]:
                lr_ceiling = lr_cap * unseen_combo_cap
                cand.meta["unseenComboCap"] = round(unseen_combo_cap, 3)
            # Family rotation is applied to the total score at ranking time
            # (see apply_family_rotation); low_repeat stays component-level.

    bd["low_repeat"] = round(max(0.0, min(lr_ceiling, lr)), 2)

    # --- drink ---
    dr_cap = weights["drink"]
    dr = 0.0
    prefer_codes = set(str(x) for x in (drink_prefs.get("preferCodes") or []))
    name_rx = drink_prefs.get("preferNameRegex")
    want_ice = ice_key_from_prefs(drink_prefs)
    drinks = drink_codes_in(cand)
    ice_keys = ice_keys_in(cand)
    if drinks:
        if prefer_codes and any(d in prefer_codes for d in drinks):
            dr += dr_cap * 0.6
        elif name_rx:
            try:
                if re.search(name_rx, names):
                    dr += dr_cap * 0.55
                else:
                    dr += dr_cap * 0.25
            except re.error:
                dr += dr_cap * 0.3
        else:
            dr += dr_cap * 0.35
        if ice_keys and want_ice in ice_keys:
            dr += dr_cap * 0.4
        elif ice_keys:
            dr += dr_cap * 0.1  # has ice mod but wrong
        else:
            # drink without ice mod — mild credit if drink present
            dr += dr_cap * 0.15
    bd["drink"] = round(min(dr_cap, dr), 2)

    # --- mcard_four ---
    mf_cap = weights.get("mcard_four", 0.0)
    mf = 0.0
    if mf_cap:
        if mcard:
            mf = mf_cap
        elif FOUR_NAME_RE.search(names):
            mf = mf_cap * 0.35  # 四件套 but not confirmed 麦金卡
    bd["mcard_four"] = round(min(mf_cap, mf), 2)

    # --- dual_1plus1 ---
    d11_cap = weights.get("dual_1plus1", 0.0)
    d11 = 0.0
    if d11_cap:
        if dual:
            d11 = d11_cap
        elif "1+1" in names or "随心配" in names:
            d11 = d11_cap * 0.4
    bd["dual_1plus1"] = round(min(d11_cap, d11), 2)

    # --- classic_three ---
    ct_cap = weights.get("classic_three", 0.0)
    ct = 0.0
    if ct_cap:
        if tags & {"classic_three", "three_piece"} or (cand.rotation_key or "").startswith("ct"):
            ct = ct_cap
            pmc = str(cand.meta.get("primaryMainCode") or "")
            # 麦香鱼是 classic_three 要补的缺口主堡；略抬权以便进入 Top-K 席位
            if pmc == "1600" or "麦香鱼" in names:
                ct = ct_cap * 1.35
            elif pmc == "1010":
                ct = ct_cap * 1.05
        elif re.search(r"(三件套|精选单人)", names) and not (mcard or dual):
            ct = ct_cap * 0.55
    bd["classic_three"] = round(min(ct_cap * 1.35, ct), 2)


    # --- coffee ---
    cf_cap = weights.get("coffee", 0.0)
    cf = 0.0
    if cf_cap:
        if coffee_list:
            cf = cf_cap
            # slight extra if preferCodes match
            prefer_cf = set(str(x) for x in (prefs.get("coffeePreferCodes") or drink_prefs.get("preferCodes") or []))
            if prefer_cf and any(c in prefer_cf for c in coffee_list):
                cf = cf_cap
        elif meal_period(req) == "breakfast":
            cf = 0.0
        else:
            cf = cf_cap * 0.05
    bd["coffee"] = round(min(cf_cap, cf), 2)

    cand.breakdown = bd
    cand.score = round(sum(bd.values()), 2)
    cand.meta["roles"] = roles
    cand.meta["weights"] = weights


def _quote_for(rotation_key: str, req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pick up a caller-supplied calculate-price result for one candidate."""
    quotes = req.get("priceQuotes")
    if not isinstance(quotes, dict):
        return None
    quote = quotes.get(rotation_key)
    if isinstance(quote, dict):
        # Accept either the raw calculate-price data object or a wrapper.
        return quote.get("data") if isinstance(quote.get("data"), dict) else quote
    return None


def candidates_from_request(req: Dict[str, Any]) -> Tuple[List[ScoredCandidate], List[str]]:
    warnings: List[str] = []
    raw = req.get("candidates")
    if isinstance(raw, list) and not raw:
        return [], ["explicit candidates[] is empty; historical fallback disabled"]
    if isinstance(raw, list) and raw:
        out: List[ScoredCandidate] = []
        for i, c in enumerate(raw):
            if not isinstance(c, dict):
                continue
            items = c.get("items")
            if not items:
                warnings.append(f"candidate[{i}] missing items; skipped")
                continue
            price = int(c.get("listPriceFen") or c.get("priceFen") or 0)
            rk = str(c.get("rotationKey") or c.get("id") or f"cand-{i}")
            tags = list(c.get("tags") or [])
            pcodes = [str(it.get("productCode") or "") for it in items]
            if any(pc in MCARD_FOUR_CODES for pc in pcodes):
                for t in ("mcard_four", "mcard", "four_piece"):
                    if t not in tags:
                        tags.append(t)
            if sum(1 for pc in pcodes if pc in DUAL11_PRODUCT_CODES) >= 2:
                for t in ("dual11", "deal"):
                    if t not in tags:
                        tags.append(t)
            meta = {}
            if c.get("primaryMainCode"):
                meta["primaryMainCode"] = str(c["primaryMainCode"])
            if c.get("mcardFour") or any(pc in MCARD_FOUR_CODES for pc in pcodes):
                meta["mcardFour"] = True
            quote = _quote_for(rk, req)
            cand = ScoredCandidate(
                rotation_key=rk,
                human_summary=str(c.get("humanSummary") or rk),
                price_fen=price,
                items=deepcopy(items),
                tags=tags,
                price_verified=False,
                source="candidates",
                meta=meta,
            )
            if quote:
                apply_quote(cand, quote)
            out.append(cand)
        if out:
            if any(c.get("priceVerified") for c in raw if isinstance(c, dict)):
                warnings.append("ignored input priceVerified flags; offline results require fresh calculate-price")
            return out, warnings
        warnings.append("explicit candidates[] unusable; historical fallback disabled")
        return [], warnings

    # limited enum from mealDetails
    enumed, w2 = enumerate_dual_11(req)
    warnings.extend(w2)
    if enumed:
        return enumed, warnings

    # fallback templates
    fb_cands, w3 = load_fallback_candidates(req)
    warnings.extend(w3)
    return fb_cands, warnings


def load_fallback_candidates(req: Dict[str, Any]) -> Tuple[List[ScoredCandidate], List[str]]:
    warnings = [
        "no candidates/mealDetails; loaded templates/fallback-combos.json",
        "fallback prices are historical; offline results require fresh calculate-price",
    ]
    path = FALLBACK_PATH
    if not path.is_file():
        return [], [f"fallback missing: {path}"]
    data = load_json(path)
    fallback_store = str(data.get("storeCodeDefault") or "")
    request_store = str(req.get("storeCode") or "")
    if fallback_store and request_store and fallback_store != request_store:
        return [], [f"fallback store {fallback_store} does not match request store {request_store}"]
    out: List[ScoredCandidate] = []
    for c in data.get("combos") or []:
        out.append(
            ScoredCandidate(
                rotation_key=str(c["rotationKey"]),
                human_summary=str(c.get("humanSummary") or c["rotationKey"]),
                price_fen=int(c.get("priceFen") or 0),
                items=deepcopy(c["items"]),
                tags=list(c.get("tags") or []),
                price_verified=False,
                source="fallback",
            )
        )
    return out, warnings


def _pick_drink_choice(
    choices: List[Dict[str, Any]],
    drink_prefs: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    prefer = [str(x) for x in (drink_prefs.get("preferCodes") or [])]
    rx = drink_prefs.get("preferNameRegex")
    # prefer codes first
    for code in prefer:
        for ch in choices:
            if ch["code"] == code:
                return ch
    if rx:
        try:
            cre = re.compile(rx)
            for ch in choices:
                if cre.search(ch["name"]):
                    return ch
        except re.error:
            pass
    # any known drink
    for ch in choices:
        if ch["code"] in KNOWN_DRINK_CODES or DRINK_NAME_RE.search(ch["name"]):
            return ch
    return None


def _pick_non_drink(choices: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for ch in choices:
        if ch["code"] in KNOWN_DRINK_CODES or DRINK_NAME_RE.search(ch["name"]):
            continue
        return ch
    return None


def _default_mod_for_drink(drink_prefs: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "values": [
            {
                "code": str(drink_prefs.get("iceCode") or "200002"),
                "key": ice_key_from_prefs(drink_prefs),
                "quantity": 1,
            }
        ]
    }


def _combo_item(code: str, mod: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    d: Dict[str, Any] = {"code": code, "quantity": 1}
    if mod:
        d["modification"] = mod
    return d


def enumerate_dual_11(req: Dict[str, Any]) -> Tuple[List[ScoredCandidate], List[str]]:
    """Limited enum: pair two productType-ish 1+1 meals from mealDetails.

    For each ordered pair (A,B) with A!=B:
      A white zone = preferred drink + ice
      B white zone = non-drink
    Red zone = first choice (or default) of round 1.
    Price estimate = sum of list prices from menuSnapshot if present.
    """
    warnings: List[str] = []
    details = req.get("mealDetails")
    if not isinstance(details, dict) or not details:
        return [], []

    drink_prefs = (req.get("prefs") or {}).get("drink") or {}
    menu = req.get("menuSnapshot") or {}
    price_map: Dict[str, int] = {}
    # menuSnapshot flexible shapes
    for bucket in (
        menu.get("products"),
        menu.get("items"),
        menu.get("meals"),
        menu.get("list"),
    ):
        if isinstance(bucket, list):
            for p in bucket:
                code = str(p.get("productCode") or p.get("code") or "")
                price = p.get("priceFen")
                if price is None and p.get("price") is not None:
                    # assume yuan float
                    try:
                        price = int(round(float(p["price"]) * 100))
                    except Exception:
                        price = None
                if code and price is not None:
                    price_map[code] = int(price)
        elif isinstance(bucket, dict):
            for code, p in bucket.items():
                if not isinstance(p, dict):
                    continue
                price = p.get("priceFen")
                if price is None and p.get("price") is not None:
                    try:
                        price = int(round(float(p["price"]) * 100))
                    except Exception:
                        price = None
                if price is not None:
                    price_map[str(code)] = int(price)

    # Also allow mealDetails[code].listPriceFen
    meal_metas: List[Tuple[str, Dict[str, Any], List[Dict[str, Any]]]] = []
    for pcode, detail in details.items():
        rounds = rounds_of(detail, path=f"mealDetails.{pcode}")
        if len(rounds) < 2:
            continue
        meal_metas.append((str(pcode), detail, rounds))

    if len(meal_metas) < 2:
        warnings.append(
            f"mealDetails has {len(meal_metas)} usable meals (<2); skip dual enum"
        )
        return [], warnings

    out: List[ScoredCandidate] = []
    for i, (code_a, det_a, rounds_a) in enumerate(meal_metas):
        for j, (code_b, det_b, rounds_b) in enumerate(meal_metas):
            if i == j:
                continue
            # Identify white zone as last round or name containing 白
            def white_and_red(rounds: List[Dict[str, Any]]):
                red = rounds[0]
                white = rounds[-1]
                for r in rounds:
                    if "白" in r["name"]:
                        white = r
                    if "红" in r["name"]:
                        red = r
                return red, white

            red_a, white_a = white_and_red(rounds_a)
            red_b, white_b = white_and_red(rounds_b)
            if not red_a["choices"] or not white_a["choices"]:
                continue
            if not red_b["choices"] or not white_b["choices"]:
                continue

            drink = _pick_drink_choice(white_a["choices"], drink_prefs)
            # white-zone non-drinks (pie/corn/sundae…) — keep a short list
            side_choices = [
                ch for ch in white_b["choices"]
                if not (
                    ch["code"] in KNOWN_DRINK_CODES or DRINK_NAME_RE.search(ch["name"])
                )
            ]
            if not drink or not side_choices:
                continue

            # Prefer burger/roll on drink leg; chicken/side-ish on white leg.
            def _is_burgerish(ch: Dict[str, Any]) -> bool:
                return classify_code(ch["code"], ch.get("name") or "") == "main" or bool(
                    MAIN_NAME_RE.search(ch.get("name") or "")
                )

            def _is_sideish(ch: Dict[str, Any]) -> bool:
                role = classify_code(ch["code"], ch.get("name") or "")
                return role in ("side", "other")

            mains_a = [ch for ch in red_a["choices"] if _is_burgerish(ch)] or list(red_a["choices"])
            mains_b = [ch for ch in red_b["choices"] if _is_sideish(ch)] or list(red_b["choices"])
            # cap fan-out to keep offline enum tractable
            mains_a = mains_a[:6]
            mains_b = mains_b[:6]
            sides = side_choices[:4]

            mid_rounds_a = [r for r in rounds_a if r["id"] not in (red_a["id"], white_a["id"])]
            mid_rounds_b = [r for r in rounds_b if r["id"] not in (red_b["id"], white_b["id"])]

            def build_item(
                pcode: str,
                detail: Dict[str, Any],
                red,
                white,
                mid,
                red_choice: Dict[str, Any],
                white_choice: Dict[str, Any],
                drink_mod: bool,
            ):
                rl = [
                    {
                        "round": red["id"],
                        "comboItemList": [_combo_item(red_choice["code"])],
                    }
                ]
                for m in mid:
                    if m["choices"]:
                        rl.append(
                            {
                                "round": m["id"],
                                "comboItemList": [_combo_item(m["choices"][0]["code"])],
                            }
                        )
                mod = _default_mod_for_drink(drink_prefs) if drink_mod else None
                rl.append(
                    {
                        "round": white["id"],
                        "comboItemList": [_combo_item(white_choice["code"], mod)],
                    }
                )
                return {
                    "productCode": pcode,
                    "productName": str(
                        detail.get("productName")
                        or detail.get("name")
                        or pcode
                    ),
                    "quantity": 1,
                    "roundList": rl,
                }

            pa = price_map.get(code_a) or int(det_a.get("listPriceFen") or det_a.get("priceFen") or 0)
            pb = price_map.get(code_b) or int(det_b.get("listPriceFen") or det_b.get("priceFen") or 0)
            if not pa and det_a.get("price") is not None:
                pa = int(round(float(det_a["price"]) * 100))
            if not pb and det_b.get("price") is not None:
                pb = int(round(float(det_b["price"]) * 100))
            # also accept currentPrice yuan strings via menu already in price_map
            price_base = int(pa) + int(pb)
            if price_base <= 0:
                warnings.append(f"enum {code_a}+{code_b} missing list price; skip pair")
                continue

            for main_a in mains_a:
                for main_b in mains_b:
                    if main_a["code"] == main_b["code"]:
                        continue
                    for side in sides:
                        item_a = build_item(
                            code_a, det_a, red_a, white_a, mid_rounds_a, main_a, drink, True
                        )
                        item_b = build_item(
                            code_b, det_b, red_b, white_b, mid_rounds_b, main_b, side, False
                        )
                        price = price_base
                        rk = (
                            f"enum{code_a[-4:]}{main_a['code']}+"
                            f"{code_b[-4:]}{main_b['code']}_w{side['code']}"
                        )
                        summary = (
                            f"{item_a['productName']}（{main_a['name'] or main_a['code']}+"
                            f"{drink['name'] or drink['code']}）+ "
                            f"{item_b['productName']}（{main_b['name'] or main_b['code']}+"
                            f"{side['name'] or side['code']}）≈¥{price/100:.1f}"
                        )
                        out.append(
                            ScoredCandidate(
                                rotation_key=rk,
                                human_summary=summary,
                                price_fen=price,
                                items=[item_a, item_b],
                                tags=["enum", "dual11", "deal"],
                                price_verified=False,
                                source="mealDetails_enum",
                                meta={"primaryMainCode": main_a["code"]},
                            )
                        )

    if out:
        warnings.append(f"enumerated {len(out)} dual-1+1 candidates from mealDetails")
    else:
        warnings.append("mealDetails present but dual-1+1 enum produced 0 combos")
    return out, warnings


def interleave_by_main_code(
    cands: List[ScoredCandidate], rng: "random.Random | None" = None
) -> List[ScoredCandidate]:
    """Round-robin by primaryMainCode inside a family.

    Keeps 鱼牛红区的鳕鱼/不素之霸 from collapsing into one product-seat behind 笋卷,
    while still spacing repeated mains. Equal-score groups are shuffled when rng given.
    """
    if len(cands) <= 1:
        return list(cands)
    # stable group by score then optional shuffle within ties
    grouped: List[List[ScoredCandidate]] = []
    for c in cands:
        if not grouped or grouped[-1][0].score != c.score:
            grouped.append([c])
        else:
            grouped[-1].append(c)
    ordered: List[ScoredCandidate] = []
    for g in grouped:
        if rng is not None and len(g) > 1:
            g = list(g)
            rng.shuffle(g)
        ordered.extend(g)
    buckets: Dict[str, List[ScoredCandidate]] = {}
    order: List[str] = []
    for c in ordered:
        key = primary_main_code(c) or c.rotation_key or str(id(c))
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(c)
    if len(order) <= 1:
        return ordered
    out: List[ScoredCandidate] = []
    idxs = {k: 0 for k in order}
    while any(idxs[k] < len(buckets[k]) for k in order):
        for k in order:
            i = idxs[k]
            if i < len(buckets[k]):
                out.append(buckets[k][i])
                idxs[k] = i + 1
    return out


def select_from_ranked(
    ranked: List[ScoredCandidate],
    req: Dict[str, Any],
    rng: random.Random,
) -> Tuple[ScoredCandidate, List[ScoredCandidate], str]:
    sel = req.get("selection") or {}
    mode = str(sel.get("mode") or "rank_then_random")
    top_k = int(sel.get("topK") or 5)
    random_mode = str(sel.get("random") or "uniform")
    # diversifyByMain: at most one seat per primaryMainCode inside Top-K
    diversify = sel.get("diversifyByMain")
    if diversify is None:
        diversify = True
    per_main = int(sel.get("maxPerMainInTopK") or 1)
    # diversifyByFamily: round-robin families so mcard/dual/coffee share Top-K
    diversify_fam = sel.get("diversifyByFamily")
    if diversify_fam is None:
        pri = effective_priority(req)
        diversify_fam = sum(1 for p in pri if p in ("mcard_four", "dual_1plus1", "classic_three", "coffee")) >= 2

    def take_pool(seq: List[ScoredCandidate]) -> List[ScoredCandidate]:
        pool: List[ScoredCandidate] = []
        per_counts: Dict[str, int] = {}
        for c in seq:
            if diversify and per_main > 0:
                mc = primary_main_code(c) or c.rotation_key
                if per_counts.get(mc, 0) >= per_main:
                    continue
                per_counts[mc] = per_counts.get(mc, 0) + 1
            pool.append(c)
            if len(pool) >= max(1, top_k):
                break
        return pool or seq[: max(1, top_k)]

    if diversify_fam:
        # bucket by family preserving score order, then round-robin
        buckets: Dict[str, List[ScoredCandidate]] = {}
        order: List[str] = []
        for c in ranked:
            fam = candidate_family(c)
            if fam not in buckets:
                buckets[fam] = []
                order.append(fam)
            buckets[fam].append(c)
        # within each family, interleave by main so 鳕鱼/不素可同时进 Top-K
        for fam in list(buckets.keys()):
            buckets[fam] = interleave_by_main_code(buckets[fam], rng)
        # prefer family order from priority / candidateFamilies
        pref_order = []
        for p in effective_priority(req):
            mapped = {"mcard_four": "mcard_four", "dual_1plus1": "dual_1plus1", "classic_three": "classic_three", "coffee": "coffee_combo"}.get(p)
            if mapped and mapped in buckets and mapped not in pref_order:
                pref_order.append(mapped)
        for fam in (req.get("candidateFamilies") or []) + order:
            if fam in buckets and fam not in pref_order:
                pref_order.append(fam)
        mixed: List[ScoredCandidate] = []
        idxs = {f: 0 for f in pref_order}
        while len(mixed) < max(1, top_k * 3) and any(idxs[f] < len(buckets[f]) for f in pref_order):
            progressed = False
            for f in pref_order:
                i = idxs[f]
                if i < len(buckets[f]):
                    mixed.append(buckets[f][i])
                    idxs[f] = i + 1
                    progressed = True
            if not progressed:
                break
        pool = take_pool(mixed)
    elif diversify and per_main > 0:
        pool = take_pool(ranked)
    else:
        pool = ranked[: max(1, top_k)]

    if mode == "rank_only":
        # rank_only means "best score", not "first after family interleaving".
        return ranked[0], ranked[1:], mode
    if top_k == 1 or len(pool) == 1:
        return pool[0], pool[1:], mode

    if random_mode == "softmax":
        # temperature soft; scores may be close
        scores = [c.score for c in pool]
        m = max(scores) if scores else 0.0
        exps = [math.exp((s - m) / 8.0) for s in scores]
        total = sum(exps) or 1.0
        probs = [e / total for e in exps]
        pick = rng.choices(pool, weights=probs, k=1)[0]
    else:
        pick = rng.choice(pool)

    alts = [c for c in pool if c.rotation_key != pick.rotation_key]
    return pick, alts, mode


def build_response(
    pick: ScoredCandidate,
    alts: List[ScoredCandidate],
    ranked: List[ScoredCandidate],
    req: Dict[str, Any],
    warnings: List[str],
    fallback_used: bool,
    mode: str,
) -> Dict[str, Any]:
    rank = next(
        (i + 1 for i, c in enumerate(ranked) if c.rotation_key == pick.rotation_key),
        1,
    )
    top_k = int((req.get("selection") or {}).get("topK") or 5)
    main_code = str(pick.meta.get("primaryMainCode") or primary_main_code(pick) or "")
    side_code = str(pick.meta.get("primarySideCode") or primary_side_code(pick) or "")
    return {
        "ok": True,
        "generatedAt": now_cst_iso(),
        "storeCode": str(req.get("storeCode") or ""),
        "mealPeriod": meal_period(req),
        "profileId": req.get("profileId"),
        "rotationKey": pick.rotation_key,
        "humanSummary": pick.human_summary,
        "priceFen": pick.price_fen,
        "priceYuan": round(pick.price_fen / 100.0, 2),
        "payableFen": payable_fen(pick, req),
        "needsQuoteBeforeConfirm": bool(pick.meta.get("needsQuoteBeforeConfirm")),
        "priceVerified": bool(pick.price_verified),
        "takeWayCodeHint": str(req.get("dining") or "eat-in"),
        "mainCode": main_code,
        "mainName": _code_display_name(main_code, pick),
        "sideCode": side_code,
        "sideName": _code_display_name(side_code, pick),
        "isNew": bool(pick.meta.get("newSignals")),
        "newSignals": list(pick.meta.get("newSignals") or []),
        "unseenMain": bool(pick.meta.get("unseenMain")),
        "unseenSide": bool(pick.meta.get("unseenSide")),
        "score": pick.score,
        "scoreBreakdown": pick.breakdown,
        "rank": rank,
        "topK": top_k,
        "selectionMode": mode,
        "items": pick.items,
        "alternates": [
            {
                "rotationKey": a.rotation_key,
                "score": a.score,
                "priceFen": a.price_fen,
                "humanSummary": a.human_summary,
                "rank": next(
                    (
                        i + 1
                        for i, c in enumerate(ranked)
                        if c.rotation_key == a.rotation_key
                    ),
                    None,
                ),
            }
            for a in alts
        ],
        "warnings": warnings,
        "fallbackUsed": fallback_used,
        "phase": "phase1_scored",
    }


def error_response(
    req: Dict[str, Any], warnings: List[str], fallback_used: bool = False
) -> Dict[str, Any]:
    return {
        "ok": False,
        "generatedAt": now_cst_iso(),
        "storeCode": str(req.get("storeCode") or ""),
        "rotationKey": "",
        "humanSummary": "no candidate passed hard filters",
        "priceFen": 0,
        "priceYuan": 0,
        "payableFen": None,
        "needsQuoteBeforeConfirm": False,
        "priceVerified": False,
        "takeWayCodeHint": str(req.get("dining") or "eat-in"),
        "score": 0,
        "scoreBreakdown": {},
        "rank": 0,
        "topK": int((req.get("selection") or {}).get("topK") or 5),
        "selectionMode": str((req.get("selection") or {}).get("mode") or ""),
        "items": [],
        "alternates": [],
        "warnings": warnings,
        "fallbackUsed": fallback_used,
        "phase": "phase1_scored",
    }


def run(req: Dict[str, Any], seed: Optional[int] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    warnings: List[str] = []
    cands, w = candidates_from_request(req)
    warnings.extend(w)
    fallback_used = any(c.source == "fallback" for c in cands) and not req.get("candidates")

    passed: List[ScoredCandidate] = []
    for c in cands:
        reason = hard_filter(c, req)
        if reason:
            c.hard_fail = reason
            warnings.append(f"filtered {c.rotation_key}: {reason}")
            continue
        score_candidate(c, req)
        passed.append(c)

    if not passed:
        # last resort: if we filtered everything but have fallback unverified path
        # try loading fallback ignoring only soft? No — still respect hard filters.
        # If candidates were non-fallback and all failed, try fallback once.
        if (
            "candidates" not in req
            and not any(c.source == "fallback" for c in cands)
            and not any("fallback" in warning for warning in warnings)
        ):
            fb, w2 = load_fallback_candidates(req)
            warnings.extend(w2)
            for c in fb:
                reason = hard_filter(c, req)
                if reason:
                    warnings.append(f"filtered fallback {c.rotation_key}: {reason}")
                    continue
                score_candidate(c, req)
                passed.append(c)
            fallback_used = bool(fb)

    if not passed:
        return error_response(
            req, warnings + ["all candidates failed hard filters"], fallback_used=fallback_used
        ), []

    apply_family_rotation(passed, req)
    ranked = sorted(passed, key=lambda c: (-c.score, c.price_fen, c.rotation_key))
    rng = random.Random(seed)
    pick, alts, mode = select_from_ranked(ranked, req, rng)
    resp = build_response(pick, alts, ranked, req, warnings, fallback_used, mode)

    rank_dump = [
        {
            "rank": i + 1,
            "rotationKey": c.rotation_key,
            "score": c.score,
            "scoreBreakdown": c.breakdown,
            "priceFen": c.price_fen,
            "source": c.source,
            "humanSummary": c.human_summary,
            "mainCode": str(c.meta.get("primaryMainCode") or primary_main_code(c) or ""),
            "sideCode": str(c.meta.get("primarySideCode") or primary_side_code(c) or ""),
            "newSignals": list(c.meta.get("newSignals") or []),
            "novelty": (c.breakdown or {}).get("novelty"),
            "unseenMain": bool(c.meta.get("unseenMain")),
            "unseenSide": bool(c.meta.get("unseenSide")),
            "lowRepeat": (c.breakdown or {}).get("low_repeat"),
        }
        for i, c in enumerate(ranked)
    ]
    return resp, rank_dump


def parse_seeds(spec: Optional[str], single: Optional[int]) -> List[Optional[int]]:
    """Parse --seeds 1-10 / 1,2,3 or fall back to --seed / [None]."""
    if spec:
        spec = str(spec).strip()
        out: List[int] = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                lo, hi = int(a), int(b)
                if hi < lo:
                    lo, hi = hi, lo
                out.extend(range(lo, hi + 1))
            else:
                out.append(int(part))
        if not out:
            raise SystemExit("empty --seeds")
        return out  # type: ignore[return-value]
    return [single]


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase1 McD offline scorer (profile-aware; no MCP/order)"
    )
    p.add_argument("-i", "--input", help="RecommendRequest JSON path (else stdin / --profile)")
    p.add_argument("--profile", help="profiles/<name>.json — materialize request defaults")
    p.add_argument("--prefs-file", help="overlay prefs/request JSON onto profile/input")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible Top-K pick")
    p.add_argument(
        "--seeds",
        default=None,
        help="Batch seeds, e.g. 1-10 or 1,3,5; serial; with --record-pick each pick updates history before next",
    )
    p.add_argument(
        "--history-file",
        default=None,
        help="Override history path (v2 JSON with mains[]/sides[]/entries[]); used for read + optional record",
    )
    p.add_argument(
        "--record-pick",
        action="store_true",
        help="After each successful pick, append main+side tracks to --history-file (or profile historyPath)",
    )
    p.add_argument(
        "--reset-history",
        action="store_true",
        help="Wipe history-file to empty tracks before the run (demo / serial batch)",
    )
    p.add_argument(
        "--rank",
        nargs="?",
        const="-",
        default=None,
        help="Write full ranked list to path (or stderr if '-' / flag alone); multi-seed → <path>.seedN",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print stdout JSON",
    )
    return p.parse_args(argv)


def apply_history_file(req: Dict[str, Any], history_file: Optional[str]) -> Dict[str, Any]:
    """Load history doc into req.history + req.historyTracks; set historyPath."""
    path = history_file or req.get("historyPath")
    if not path:
        # still normalize inline
        if req.get("history") is not None and "historyTracks" not in req:
            doc = normalize_history_doc(req.get("history"))
            req["history"] = list(doc.get("entries") or [])
            req["historyTracks"] = {
                "mains": list(doc.get("mains") or []),
                "sides": list(doc.get("sides") or []),
            }
        return req
    req["historyPath"] = path
    doc = load_history_doc(path)
    req["history"] = list(doc.get("entries") or [])
    req["historyTracks"] = {
        "mains": list(doc.get("mains") or []),
        "sides": list(doc.get("sides") or []),
    }
    return req


def resolve_request(args: argparse.Namespace) -> Dict[str, Any]:
    """Load request from --profile / -i / stdin, then apply --prefs-file / history-file."""
    from profile_lib import deep_merge

    req: Optional[Dict[str, Any]] = None
    if args.profile and args.input:
        req = load_profile(args.profile)
        overlay = load_json(Path(args.input))
        # merge overlay on top (candidates from -i win)
        req = deep_merge(req, overlay)
    elif args.profile:
        req = load_profile(args.profile)
    elif args.input:
        req = load_json(Path(args.input))
    else:
        raw = sys.stdin.read()
        if not raw.strip():
            raise SystemExit("empty stdin; pass --profile, -i request.json, or pipe JSON")
        req = json.loads(raw)
    if not isinstance(req, dict):
        raise SystemExit("request root must be object")
    if args.prefs_file:
        req = apply_prefs_file(req, args.prefs_file)
    # history-file always wins over embedded history from -i / profile inline
    hist_path = args.history_file or req.get("historyPath")
    if args.reset_history:
        if not hist_path:
            raise SystemExit("--reset-history requires --history-file or profile historyPath")
        save_history_doc(hist_path, {"version": 2, "mains": [], "sides": [], "entries": []})
    req = apply_history_file(req, args.history_file)
    # default window picks
    req.setdefault("historyWindowPicks", 5)
    req.setdefault("historyWindowDays", 5)
    return req


def record_response_pick(req: Dict[str, Any], resp: Dict[str, Any], seed: Optional[int]) -> None:
    """Write primary main/side onto tracks; also append any extra mains/sides in the combo."""
    path = req.get("historyPath")
    if not path or not resp.get("ok"):
        return
    # Rebuild a light candidate from items to enumerate all role codes
    items = resp.get("items") or []
    extra_mains: List[Tuple[str, str]] = []
    extra_sides: List[Tuple[str, str]] = []
    if items:
        tmp = ScoredCandidate(
            rotation_key=str(resp.get("rotationKey") or ""),
            human_summary=str(resp.get("humanSummary") or ""),
            price_fen=int(resp.get("priceFen") or 0),
            items=items,
        )
        extra_mains = codes_by_role(tmp, "main")
        extra_sides = codes_by_role(tmp, "side")
    primary_main = str(resp.get("mainCode") or (extra_mains[0][0] if extra_mains else ""))
    primary_side = str(resp.get("sideCode") or (extra_sides[0][0] if extra_sides else ""))
    main_name = str(resp.get("mainName") or "")
    side_name = str(resp.get("sideName") or "")
    for c, n in extra_mains:
        if c == primary_main and n and not main_name:
            main_name = n
    for c, n in extra_sides:
        if c == primary_side and n and not side_name:
            side_name = n
    doc = append_history_pick(
        str(path),
        rotation_key=str(resp.get("rotationKey") or ""),
        price_fen=int(resp.get("priceFen") or 0),
        main_code=primary_main,
        main_name=main_name,
        side_code=primary_side,
        side_name=side_name,
        seed=seed,
    )
    # additional mains/sides in same pick (e.g. dual 1+1 second burger) — same timestamp
    at = (doc.get("entries") or [{}])[-1].get("at")
    from profile_lib import load_history_doc, save_history_doc

    doc = load_history_doc(str(path))
    for c, n in extra_mains:
        if c == primary_main:
            continue
        doc.setdefault("mains", []).append(
            {
                "code": c,
                "name": n,
                "at": at,
                "rotationKey": str(resp.get("rotationKey") or ""),
                **({"seed": int(seed)} if seed is not None else {}),
            }
        )
    for c, n in extra_sides:
        if c == primary_side:
            continue
        doc.setdefault("sides", []).append(
            {
                "code": c,
                "name": n,
                "at": at,
                "rotationKey": str(resp.get("rotationKey") or ""),
                **({"seed": int(seed)} if seed is not None else {}),
            }
        )
    # also list extra codes on the flat entry
    if doc.get("entries"):
        ent = doc["entries"][-1]
        extras_m = [c for c, _ in extra_mains if c != primary_main]
        extras_s = [c for c, _ in extra_sides if c != primary_side]
        if extras_m:
            ent["extraMainCodes"] = extras_m
        if extras_s:
            ent["extraSideCodes"] = extras_s
    save_history_doc(str(path), doc)
    req["history"] = list(doc.get("entries") or [])
    req["historyTracks"] = {
        "mains": list(doc.get("mains") or []),
        "sides": list(doc.get("sides") or []),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        req = resolve_request(args)
    except json.JSONDecodeError as e:
        print(f"invalid JSON: {e}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2

    seeds = parse_seeds(args.seeds, args.seed)
    profile_id = req.get("profileId")
    responses: List[Dict[str, Any]] = []
    exit_ok = True

    for seed in seeds:
        # reload history from disk each round so external writers also count;
        # record_pick already refreshed req in-memory after previous round.
        if args.record_pick or args.history_file or req.get("historyPath"):
            apply_history_file(req, args.history_file or req.get("historyPath"))
        try:
            resp, rank_dump = run(req, seed=seed)
        except ContractError as exc:
            print(f"input contract error: {exc}", file=sys.stderr)
            return 2
        if profile_id:
            resp["profileId"] = profile_id
            resp.setdefault("warnings", []).insert(0, f"profile:{profile_id}")
        if seed is not None:
            resp["seed"] = seed
        if args.record_pick:
            if not req.get("historyPath"):
                print("warning: --record-pick ignored (no historyPath / --history-file)", file=sys.stderr)
            else:
                record_response_pick(req, resp, seed)
                resp.setdefault("warnings", []).append(
                    f"recorded_pick→{req.get('historyPath')} main={resp.get('mainCode')} side={resp.get('sideCode')}"
                )
        if args.rank is not None:
            dump_text = json.dumps(rank_dump, ensure_ascii=False, indent=2)
            if args.rank == "-":
                print(dump_text, file=sys.stderr)
            else:
                rank_path = Path(args.rank)
                if len(seeds) > 1 and seed is not None:
                    rank_path = rank_path.with_name(f"{rank_path.stem}.seed{seed}{rank_path.suffix or '.json'}")
                rank_path.write_text(dump_text + "\n", encoding="utf-8")
        responses.append(resp)
        if not resp.get("ok"):
            exit_ok = False

    out_obj: Any = responses[0] if len(responses) == 1 else {"ok": exit_ok, "runs": responses}
    if args.pretty:
        print(json.dumps(out_obj, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(out_obj, ensure_ascii=False, separators=(",", ":")))
    return 0 if exit_ok else 1


if __name__ == "__main__":
    sys.exit(main())
