# 麦当劳配餐推荐 — Phase0/1（可复用 profiles）

> 状态：**契约 / 模板 / profile / 只读打分与候选扩展**。不自动下单、不改例程、不 create-order / 领券。  
> 读者：使用本引擎的开发者与调用方；主调用方：午餐 / 早餐 Skill 或例程。

## 复用原则

**本地生活两个任务（午餐 / 早餐）只换筛选条件**：指定 `profiles/lunch.json` 或 `profiles/breakfast.json`（或传入 prefs JSON）。  
**加新场景（如晚餐）**：复制一份 profile 改 `mealPeriod` / `prefs` / `candidateFamilies` / `budgetFen`，**不必改** `score_lunch.py` / `build_candidates.py` 核心逻辑。

核心脚本只认：

- `mealPeriod`（lunch|breakfast|dinner|snack…）
- `prefs.priority`（含 `mcard_four` / `dual_1plus1` / `coffee` / `satiety` / …）
- `prefs.requireCoffee`、`drink`、`selection`
- `candidateFamilies`（含 `classic_three`；或由 priority 推断）

不要在 Skill 里写死两套算法。

**新品**：MCP `query-meals` / `query-meal-detail` **无** `isNew` 布尔字段；用菜单 `categories[].meals[].tags`（如「上新」「全新升级」）+ 名称启发式 + profile `prefs.newTagRegex` / `newProductCodes` / `newMainCodes` 抬 `novelty`。

**主食规则**：麦乐鸡等鸡块/纯小食不得作为主食或双1+1 红区左腿；分类为 side，候选生成会跳过，打分硬门槛会淘汰「麦乐鸡当主」的组合。

**经典三件套**：`classic_three` 从当日菜单动态发现「三件套 / 精选单人餐 / 汉堡·鱼套餐」，**只收标价≤`budgetFen`**（午餐默认 ¥35），用于补齐麦香鱼等优惠红区未覆盖的主堡；SKU 不写死进 Skill。

## 目录

```
engine/
  profile_lib.py
  profiles/
    lunch.json                 # 午餐：麦金卡四件套 + 双1+1 + ≤预算经典三件套
    breakfast.json             # 早餐：履约见 JSON（麦乐送预约+咖啡家族）；预算¥20
  score_lunch.py               # 只读打分；--profile / --prefs-file / -i
  build_candidates.py          # 按 families 扩候选；同样吃 --profile
  schema/recommend-request.schema.json
  schema/recommend-response.schema.json
  examples/request-default.json
  examples/request-breakfast-default.json
  templates/fallback-combos.json
  runs/…                       # 当日菜单缓存 / 试跑
  README.md
```

## Profile 字段（摘要）

履约参数**全部进 profile**（可用 `--prefs-file` overlay 覆盖）。Skill / 例程不要写死任何门店或 SKU。

### 履约（fulfillment）

| 字段 | 含义 |
|---|---|
| `dining` | `eat-in` | `delivery`（也可 `take-in-store`） |
| `storeCode` | 门店编码；外送时为可配送店，**不是**午餐堂食默认 |
| `beCode` | 外送业务编码（`delivery-query-stores`）；堂食可省略 |
| `beType` / `orderType` | 1=到店堂食，2=麦乐送外送 |
| `addressId` / `addressName` | 外送地址；`addressLabel` 为 `addressName` 别名 |
| `reservation` | `{ enabled, time: "HH:mm", dayOffset }`；`profile_lib` 算 `reservationDate`（`yyyy-MM-dd HH:mm`，Asia/Shanghai） |
| `reservationDate` | 物化结果；overlay 可直接盖；`enabled=false` 时不传 MCP |
| `mealPeriod` | `breakfast` | `lunch` \| …；打分/硬过滤键 |
| `budgetFen` | 价格上限（分） |
| `candidateFamilies` | `mcard_four` `dual_1plus1` `dabao` `classic_three` `coffee_combo` |

打印履约摘要（不扩候选、不下单）：

```bash
python3 profile_lib.py lunch breakfast
```

### 其它

| 字段 | 含义 |
|---|---|
| `prefs.priority` | 软分权重顺序；可含 `mcard_four` `dual_1plus1` `coffee` |
| `prefs.requireCoffee` | true → 硬过滤无咖啡（早餐默认） |
| `prefs.drink` | 饮料 code / 名称 / 冰量 |
| `selection` | Top-K / `diversifyByMain` |
| `history` / `historyPath` | 少重复；磁盘约定 `runs/history/<profile>.json`（v2：`mains[]`/`sides[]`/`entries[]`） |
| `historyWindowPicks` | 各轨近 N 次（默认 5）；与 `historyWindowDays` 同时生效 |
| `prefs.unseenMainBoost` / `unseenSideBoost` / `unseenComboCap` | 窗口内未见主/小食抬权系数与 `low_repeat` 上限（默认 1.22 / 1.12 / 1.32） |
| `paths` | 默认 menu / details / output（相对本目录；**按 profile 店码命名**，勿写死） |

## CLI

```bash
# 从仓库根目录进入 engine
cd engine

# 1) 按 profile 扩候选（只读，用当日 meal-details + slim 菜单）
python3 build_candidates.py --profile lunch -o runs/request-lunch-built.json
python3 build_candidates.py --profile breakfast -o runs/request-breakfast-built.json

# 2) 打分（可复现）
python3 score_lunch.py --profile lunch -i runs/request-lunch-built.json --seed 1 --pretty
python3 score_lunch.py --profile breakfast -i runs/request-breakfast-built.json --seed 1 --pretty

# 覆盖偏好而不改 profile 文件
python3 score_lunch.py --profile lunch -i runs/request-lunch-built.json \
  --prefs-file /tmp/prefs-overlay.json --seed 2

# 3) 主食/小食分轨历史：串行 10 轮，每轮中选后写入再打下一轮
python3 score_lunch.py --profile lunch -i runs/request-lunch-built.json \
  --history-file runs/history/lunch.json --reset-history --record-pick \
  --seeds 1-10 --pretty
```

**历史降权 + 未见抬权（默认）**：`historyWindowPicks=5`（按**餐次**计，一餐双堡会写入多条 code 但只占 1 次窗口）+ `historyWindowDays=5`。主食轨 / 小食轨分开记（互不交叉）：
- **已选降权**：窗口内出现过的 code，距今越近降得越狠（`_recency_multiplier`）。
- **未见抬权**：同一窗口**非空**且 code 从未出现 → 乘 `prefs.unseenMainBoost`（默认 1.22）/ `unseenSideBoost`（默认 1.12）；两轨可叠加，但 `low_repeat` 维不超过 `lr_cap × unseenComboCap`（默认 1.32）。
- **空历史**：两轨都无餐 → **不抬**（全体中性），避免「全员同加」淹没其它维度。
- 已选/未见对同一 code **互斥**，共用窗口，不双计。软分，不碰预算硬门槛。可与新品 `novelty` 叠加（不同维）。

`--record-pick` 追加 `{code,name,at}` 到对应轨并写 `entries`；双 1+1 会把组合内全部 main/side 都记上。

- stdout：`RecommendResponse`（含 `profileId` / `mealPeriod`；`items` 可交给外部调用方做 `calculate-price`）
- 离线引擎始终输出 `priceVerified=false`；输入模板中的历史验价标记不会被沿用
- 退出码：`0` ok，`1` 无候选，`2` 输入坏或 MCP 快照契约错误
- 显式 `candidates=[]` 或候选全部被过滤时不使用历史 fallback；失败响应的 `items=[]`

## 给 🍔本地生活

1. **先读 profile 履约字段**（`dining` / `storeCode` / `beCode` / `address*` / `reservation` / `mealPeriod` / `budgetFen` / `candidateFamilies`），例程 `--prefs-file` 仍可盖预算/地址/店码。  
2. 午餐：`--profile lunch` → 从 profile 读取堂食门店、预算与候选家族。
3. 早餐：`--profile breakfast` → 从 profile 读取麦乐送预约、地址、预算与候选家族。`query-meals` / detail / `calculate-price` 带物化后的 `reservationDate` + 外送参数；**不要**用午市堂食缓存。
4. MCP 链路：`query-meals` → 按 `candidateFamilies` 拉 `query-meal-detail` → `build_candidates` → `score_lunch` → **`calculate-price`** → 对人确认 → 才 `create-order`（本仓库脚本禁止下单）。  
5. 麦金卡会员身份问题在算价/下单错误时由本地生活处理。

## 推荐回归评估

从仓库根目录运行 `python3 engine/evaluate_recommendations.py`。固定场景、指标与限制见 `../docs/recommendation-evaluation.md`。

## 软分维度

`deal` / `satiety` / `novelty` / `low_repeat` / `drink` / **`mcard_four`** / **`dual_1plus1`** / **`classic_three`** / **`coffee`**，按 `prefs.priority` 调权。  
硬过滤：预算、饱腹、白区饮料去重；`requireCoffee` 时无咖啡直接剔除。  
`low_repeat`：主食 code / 小食 code **分轨**读近期历史（见上）；整单 `rotationKey` 命中仍直接清零该维；窗口内未见主/小食对称抬权（`unseenMainBoost` / `unseenSideBoost`）。

## 行为基线

- `engine/tests/` 的离线用例覆盖预算口径、`rank_only` 语义、历史窗口与菜单字段适配。
- `engine/evaluate_recommendations.py` 用合成夹具跑固定场景；指标口径见 `../docs/recommendation-evaluation.md`。
- 预算与召回的研究结论见 `../docs/mcp-budget-and-recall.md`。

## 禁令

- 不上独立 HTTP 服务；不把账号会话写入本目录  
- 不改午餐/早餐 routine；本目录脚本 **禁止 create-order**  
- 菜单日更：模板仅 fallback；主路径每日 `query-meals`
