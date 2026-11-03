"""Validation and normalization at the offline MCP snapshot boundary."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


class ContractError(ValueError):
    pass


def load_snapshot(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"{path}: file not found") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"{path}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}") from exc


def normalize_menu(menu: Any, path: str = "menu") -> dict[str, Any]:
    if not isinstance(menu, dict):
        raise ContractError(f"{path}: expected object")
    # Real query-meals returns `meals`; older snapshots used `mealIndex`.
    if "mealIndex" not in menu and isinstance(menu.get("meals"), dict):
        menu["mealIndex"] = menu["meals"]
    meal_index = menu.get("mealIndex")
    if meal_index is not None and not isinstance(meal_index, dict):
        raise ContractError(f"{path}.mealIndex: expected object")
    for code, meta in (meal_index or {}).items():
        meta_path = f"{path}.mealIndex.{code}"
        if not isinstance(meta, dict):
            raise ContractError(f"{meta_path}: expected object")
        if "currentPrice" in meta:
            try:
                price = float(meta["currentPrice"])
            except (TypeError, ValueError) as exc:
                raise ContractError(f"{meta_path}.currentPrice: expected non-negative number") from exc
            if not math.isfinite(price) or price < 0:
                raise ContractError(f"{meta_path}.currentPrice: expected non-negative number")
    categories = menu.get("categories")
    if categories is not None:
        if not isinstance(categories, list):
            raise ContractError(f"{path}.categories: expected array")
        for i, category in enumerate(categories):
            if not isinstance(category, dict):
                raise ContractError(f"{path}.categories[{i}]: expected object")
            meals = category.get("meals")
            if meals is not None and not isinstance(meals, list):
                raise ContractError(f"{path}.categories[{i}].meals: expected array")
            for j, meal in enumerate(meals or []):
                if not isinstance(meal, dict):
                    raise ContractError(f"{path}.categories[{i}].meals[{j}]: expected object")
    return menu


def rounds_of(detail: Any, path: str = "mealDetail") -> list[dict[str, Any]]:
    """Accept known MCP aliases and return one internal round representation."""
    if not isinstance(detail, dict):
        raise ContractError(f"{path}: expected object")
    rounds = detail.get("rounds") or detail.get("roundList")
    if not isinstance(rounds, list) or not rounds:
        raise ContractError(f"{path}.rounds: required non-empty array (or roundList)")
    normalized = []
    for i, round_data in enumerate(rounds):
        round_path = f"{path}.rounds[{i}]"
        if not isinstance(round_data, dict):
            raise ContractError(f"{round_path}: expected object")
        round_id = round_data.get("id", round_data.get("round"))
        if round_id in (None, ""):
            raise ContractError(f"{round_path}.id: required (or round)")
        choices = round_data.get("choices") or round_data.get("comboItemList") or round_data.get("products")
        if not isinstance(choices, list):
            raise ContractError(f"{round_path}.choices: required array (or comboItemList/products)")
        normalized_choices = []
        for j, choice in enumerate(choices):
            choice_path = f"{round_path}.choices[{j}]"
            if not isinstance(choice, dict):
                raise ContractError(f"{choice_path}: expected object")
            code = choice.get("code") or choice.get("productCode")
            if code in (None, ""):
                raise ContractError(f"{choice_path}.code: required (or productCode)")
            diff_price = choice.get("diffPrice") or ""
            if diff_price and not re.search(r"([+-])\s*¥?\s*([0-9]+(?:\.[0-9]+)?)", str(diff_price)):
                raise ContractError(f"{choice_path}.diffPrice: expected signed price such as +¥1.50")
            normalized_choices.append(
                {
                    "code": str(code),
                    "name": str(choice.get("name") or choice.get("productName") or choice.get("title") or ""),
                    "diffFen": _parse_diff_fen(diff_price),
                    "raw": choice,
                }
            )
        normalized.append(
            {
                "id": str(round_id),
                "name": str(round_data.get("name") or ""),
                "choices": normalized_choices,
            }
        )
    return normalized


def _parse_diff_fen(diff: Any) -> int:
    match = re.search(r"([+-])\s*¥?\s*([0-9]+(?:\.[0-9]+)?)", str(diff or ""))
    if not match:
        return 0
    sign = 1 if match.group(1) == "+" else -1
    return sign * int(round(float(match.group(2)) * 100))
