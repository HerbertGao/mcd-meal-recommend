---
name: mcd-lunch-recommend
description: >-
  Use when recommending a McDonald's China dine-in/delivery combo via this
  repository's engine: choose a profiles/*.json (or --prefs-file
  overlay), build_candidates → score_lunch → MCP calculate-price, then wait for
  explicit confirm—never auto create-order.
---
# McDonald's meal recommend (profile-driven)

One reusable engine. **Scene differences live only in profile / prefs JSON**—not in this skill and not as a second scorer. Lunch, breakfast, or a later scene: pass `--profile <name>` (or `--prefs-file`). Stop for confirmation; never place the order from this skill alone.

## Where things live

- Engine + CLI: `engine/` (`README.md`, `build_candidates.py`, `score_lunch.py`, `profile_lib.py`)
- Scene prefs: `engine/profiles/*.json` (add a scene by **copying a profile**, not by editing this skill)

Run commands from the repository root. If the Skill is installed elsewhere, first resolve the absolute path of this repository's `engine/` directory.
- I/O contract: `schema/`, `examples/`
- Fallbacks: `templates/fallback-combos.json`

JSON money fields are **fen**; show **yuan** to the user. One-off overrides (budget, ice, history, store, delivery) go in `--prefs-file` or `-i`, not hard-coded here.

## Hard rules

1. **Load a profile** (or prefs file) before building/scoring. Do not bake product names, burger codes, or store codes into chat prompts as permanent defaults—read them from the profile.
2. **Satiety, budget, drink/coffee, deal families, diversification, fulfillment**: driven by profile fields (`prefs`, `candidateFamilies`, `selection`, `mealPeriod`, `dining`/`storeCode`/`reservation`/…). Do not lock recommendations to one red-zone item or one store.
3. **Always** `build_candidates.py --profile …` (or equivalent request with `candidates[]`) before `score_lunch.py`.
4. **Price-check** chosen `items` with MCP `calculate-price` before quoting a total. `round` values are **strings**. Membership-only deals may fail at price/order—handle there; scripts do not verify membership. Until that quote exists, describe the amount as 估价 (`priceState=estimate`); re-quote if the quote expired or the items/context changed.
5. **Order**: show summary + yuan; wait for an explicit yes before `create-order`. Routines must not auto-order.
6. When the user names a combo or swaps a side, re-derive the summary from the same `roundList` and price state (`engine/combo_edit.py`); do not keep a stale side with a new total, and do not auto-pick each round's first zero-price option.

## Fulfillment (from profile only)

Read these from `profiles/<name>.json` or `--prefs-file` overlay—**never hard-code store/SKU/address in this skill**:

- `dining`, `storeCode`, `beCode`, `beType`, `orderType`
- `addressId` / `addressName` (delivery)
- `reservation` → `{enabled, time, dayOffset}` (engine materializes `reservationDate`)
- `mealPeriod`, `budgetFen`, `candidateFamilies`

Lunch vs breakfast differences are **profile values**, not separate algorithms. Overlay may still cover budget/address/store for a routine.

## Flow

1. Resolve profile name for this task (from the routine/user) + optional prefs overlay; print/check fulfillment (`python3 profile_lib.py <name>`).
2. MCP `query-meals` using the profile’s fulfillment (`storeCode` / `beType` / `orderType` / `beCode` / `reservationDate` when reserved); pull `query-meal-detail` only as needed for `candidateFamilies` in that profile. Cache slim snapshots under `runs/` if helpful (path from `profile.paths`, not a fixed store).
3. Build candidates:

```bash
python3 engine/build_candidates.py --profile <name> \
  -o engine/runs/request-<name>-built.json
# optional: --prefs-file /path/overlay.json
```

4. Score:

```bash
python3 engine/score_lunch.py --profile <name> \
  -i engine/runs/request-<name>-built.json --seed <n> --pretty
```

   Non-zero / empty → use `templates/fallback-combos.json`, still price-check.
5. `calculate-price`; if over budget or error, try next ranked / fallback.
6. Present combo + yuan; on yes → `create-order` using profile fulfillment (takeWay for eat-in; delivery + reservationDate when reserved); after pay → `query-order` / pickup hints as needed.
7. After confirm (or a deliberate dry-run with `--record-pick`), append to the profile `historyPath`: **mains** and **sides** tracks separately (`{code,name,at}` each) plus a flat `entries` row (`date/rotationKey/priceFen/mainCode/sideCode`). Do not hard-code SKUs here—read/write the history file only. Serial multi-seed scoring must record after each pick so the next round sees updated tracks.

## 新品

MCP menu/detail has **no** `isNew` field. Prefer menu `tags`（上新/全新…）via engine + profile `prefs.newTagRegex` / `newProductCodes` / `newMainCodes`—do not hard-code SKUs in this skill.

## Anti-overfit

- Do **not** copy yesterday’s winning SKUs into this skill or into a fixed “always recommend X”.
- Do **not** shrink any family (e.g. dual 1+1) to `choices[0]` only—enumerate red-zone options from meal detail.
- Prefer ranking + Top-K random (and profile diversify rules) over forcing a single template.
- When taste changes, **edit the profile JSON** (or pass overlay prefs)—leave this skill generic.

## Notes

- Scripts never call MCP and never create-order. Cache, exposure log and stage timing are offline file helpers only; there is no standing service.
- Price state is explicit: `estimate` / `quoted` / `stale`. Never present an estimate or an expired quote as 实付; a live `calculate-price` is required before asking the user to confirm.
- Exposure, skip, swap and deal are separate tracks. A skip is never written as a deal and never asserted as "user dislikes this". Pass `exposurePath` only if the caller actually records exposures.
- Forbidden: secrets in this repository, standing HTTP service for this, auto-order, assistant-specific store/drink/SKU hardcodes in this skill body.
- Wrapper recovery, scheduling and MCP calls live outside this repository; see [../docs/observability-and-integration.md](../docs/observability-and-integration.md).
