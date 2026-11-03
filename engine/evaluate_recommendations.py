#!/usr/bin/env python3
"""Run the fixed, standard-library-only recommendation regression evaluation."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import build_candidates
import score_lunch
from mcp_contract import load_snapshot
from profile_lib import deep_merge

ENGINE = Path(__file__).resolve().parent
EXAMPLES = ENGINE / "examples"


def replace_codes(value: Any, replacements: dict[str, str]) -> None:
    if isinstance(value, dict):
        for key, old in list(value.items()):
            if isinstance(old, (dict, list)):
                replace_codes(old, replacements)
            elif key in {"code", "productCode"} and str(old) in replacements:
                value[key] = replacements[str(old)]
    elif isinstance(value, list):
        for item in value:
            replace_codes(item, replacements)


def evaluate() -> dict[str, Any]:
    base = load_snapshot(EXAMPLES / "request-offline.json")
    menu = load_snapshot(EXAMPLES / "fixtures" / "menu.json")
    details = {
        path.stem: load_snapshot(path)
        for path in (EXAMPLES / "fixtures" / "meal-details").glob("*.json")
    }
    generated = build_candidates.expand(
        details,
        menu,
        base["prefs"],
        families=base["candidateFamilies"],
        budget_fen=base["budgetFen"],
    )
    scenarios = load_snapshot(ENGINE / "evaluation" / "scenarios.json")
    results = []
    hard_violations = 0
    unexpected_verified = 0
    coverage_values = []
    total_generated = 0
    total_ranked = 0

    for scenario in scenarios:
        request = deep_merge(base, scenario.get("overrides") or {})
        if "candidates" not in (scenario.get("overrides") or {}):
            request["candidates"] = deepcopy(generated)
        replace_codes(request, scenario.get("replaceCodes") or {})
        response, ranked = score_lunch.run(request, seed=scenario.get("seed", 1))
        expected = scenario.get("expect") or {}
        expectation_passed = all(response.get(key) == value for key, value in expected.items())

        if response["ok"]:
            check = score_lunch.ScoredCandidate(
                rotation_key=response["rotationKey"],
                human_summary=response["humanSummary"],
                price_fen=response["priceFen"],
                items=response["items"],
            )
            if score_lunch.hard_filter(check, request):
                hard_violations += 1
            if response["priceVerified"]:
                unexpected_verified += 1
            top_k = int((request.get("selection") or {}).get("topK") or 5)
            all_mains = {row["mainCode"] for row in ranked if row.get("mainCode")}
            top_mains = {row["mainCode"] for row in ranked[:top_k] if row.get("mainCode")}
            if all_mains:
                coverage_values.append(len(top_mains) / len(all_mains))

        total_generated += len(request.get("candidates") or [])
        total_ranked += len(ranked)
        results.append(
            {
                "name": scenario["name"],
                "expectationPassed": expectation_passed,
                "ok": response["ok"],
                "mainCode": response.get("mainCode"),
                "selectedRank": response.get("rank"),
                "generated": len(request.get("candidates") or []),
                "ranked": len(ranked),
                "priceVerified": response["priceVerified"],
                "fallbackUsed": response["fallbackUsed"],
            }
        )

    return {
        "scenarioCount": len(results),
        "failedExpectations": sum(not result["expectationPassed"] for result in results),
        "hardConstraintViolations": hard_violations,
        "unexpectedVerifiedPrices": unexpected_verified,
        "candidateFilterRetention": round(total_ranked / total_generated, 4) if total_generated else 0,
        "meanTopKMainCoverage": round(sum(coverage_values) / len(coverage_values), 4) if coverage_values else 0,
        "scenarios": results,
    }


def main() -> int:
    report = evaluate()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(
        report["failedExpectations"]
        or report["hardConstraintViolations"]
        or report["unexpectedVerifiedPrices"]
    )


if __name__ == "__main__":
    raise SystemExit(main())
