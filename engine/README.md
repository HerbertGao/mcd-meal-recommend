# 配餐推荐引擎

按 profile 生成候选、打分并选出推荐组合的离线引擎。脚本只读取本地 JSON 快照，不访问网络、不创建订单；实时菜单与算价由调用方通过 MCP 获取。

## 设计约定

一个引擎服务多个用餐场景，差异全部落在 profile：场景参数（`mealPeriod`、`prefs`、`candidateFamilies`、`budgetFen`、履约字段）写在 `profiles/<name>.json`，核心逻辑不随场景分叉。新增场景时复制一份 profile 修改字段即可。

引擎只识别以下入口键：

- `mealPeriod`：`lunch` / `breakfast` / `dinner` / `snack`
- `prefs.priority`：软分维度排序，可含 `mcard_four` / `dual_1plus1` / `coffee` / `satiety`
- `prefs.requireCoffee`、`prefs.drink`、`selection`
- `candidateFamilies`：显式列出，或由 `prefs.priority` 推断

### 新品信号

`query-meals` 与 `query-meal-detail` 都没有 `isNew` 布尔字段。`novelty` 维度的输入是菜单 `categories[].meals[].tags`（如「上新」「全新升级」）、名称启发式，以及 profile 的 `prefs.newTagRegex` / `newProductCodes` / `newMainCodes`。

### 主食判定

鸡块类（如麦乐鸡）与纯炸鸡小食归为 `side`，不会作为主食生成候选；硬过滤另有 `nugget_forbidden_as_main` 兜底，避免它们成为双 1+1 的左腿。

### 经典三件套

`classic_three` 从当日菜单动态发现「三件套 / 精选单人餐 / 汉堡套餐」，只收标价不超过 `budgetFen` 的产品，用于补齐优惠红区未覆盖的主堡。产品码不写进配置。

### 套餐可选项

候选按「先主食，再小食，再饮品」展开。主食、小食、饮品都来自该套餐 `query-meal-detail` 的轮次，不把某一组小食或某一杯饮品写成唯一默认值。

- 麦金卡四件套：主食轮定堡，接着枚举各小食轮的有效组合，再枚举饮品轮。组合数达到上限时按各轮选项轮转保留，避免靠前的一种小食把名额占满。同一主食在菜单给出多组小食、多种饮品且未超预算时，会留下多组候选。
- 大堡口福、经典三件套、双 1+1、早餐咖啡套餐同样从对应轮次枚举，并用预算截断。
- `prefs.preferSideCodes` 与 `prefs.drink.preferCodes` 只影响截断时的排序。列表非空时仍会给非偏好项留位置，避免把整轮锁死。模板里 `preferSideCodes` 为空数组。
- 麦乐鸡可以出现在小食轮，不能作为主食。

### 发现、配对、裁剪

优惠家族按「发现 → 配对 → 裁剪」生成，商品码来自当日菜单。

- **发现**：双 1+1 看分类、名称或标签是否含「随心配 / 1+1」，再用名称和 detail 轮次区分左腿（三件套、双吉：主食 + 饮品）和右腿（真有料：小食蛋白 + 配菜）。麦金卡四件套看「麦金卡」分类加上「四件套」名称；大堡口福看「大堡口福」分类或名称。双人、分享、开心乐园一类不进入这些家族。菜单没有对应信号时，才退回内置商品码。
- **配对**：只把发现出的左腿和右腿按预算、每轮选项上限展开，口径与原来的双 1+1 相同。
- **裁剪**：`--max`（默认 100）时按家族轮转；双 1+1 再按左腿商品码轮转，麦金卡、大堡口福、经典三件套按各自套餐商品码轮转。更贵的新左腿、更便宜的同族左腿，以及各个麦金卡套餐会一起留在打分池里。

## 目录

```
engine/
  profile_lib.py              # profile 解析、履约物化、历史读写
  build_candidates.py         # 菜单 + detail → 候选组合（含家族产出诊断）
  score_lunch.py              # 硬过滤、打分、选择、响应组装
  mcp_contract.py             # MCP 快照校验与字段归一
  budget.py                   # 堂食/外送预算口径与报价绑定
  quote_state.py              # 估价/已核价/过期三态与文案渲染
  mcp_cache.py                # 按门店/履约/餐段/预约隔离的快照缓存
  exposure_log.py             # 曝光/跳过/换餐/成交分轨（幂等）
  combo_edit.py               # 点名套餐排序、换餐差异、pending/calc 同步
  stage_timer.py              # 阶段计时与不可归因间隙
  evaluate_recommendations.py # 固定场景回归评估
  profiles/
    README.md
    templates/*.example.json  # 可提交的模板
    <name>.json               # 真实履约参数，本地保留，不入库
  schema/recommend-request.schema.json
  schema/recommend-response.schema.json
  examples/fixtures/          # 合成 MCP 快照（menu / meal-details）
  examples/*.json             # 请求与响应样例
  evaluation/scenarios.json   # 评估场景
  templates/fallback-combos.json
  tests/
  runs/                       # 运行期缓存与历史，不入库
```

## Profile 字段

履约参数全部来自 profile，可被 `--prefs-file` 覆盖。

### 履约

| 字段 | 含义 |
|---|---|
| `dining` | `eat-in` / `take-in-store` / `delivery` |
| `storeCode` | 门店编码；外送场景取可配送店，与堂食店码无关 |
| `beCode` | 外送业务编码，来自 `delivery-query-stores`；堂食可省略 |
| `beType` / `orderType` | 1 = 到店，2 = 麦乐送 |
| `addressId` / `addressName` | 外送地址；`addressLabel` 为 `addressName` 别名 |
| `reservation` | `{ enabled, time: "HH:mm", dayOffset }`，由 `profile_lib` 物化为 `reservationDate`（`yyyy-MM-dd HH:mm`，Asia/Shanghai） |
| `reservationDate` | 物化结果，可被 overlay 直接覆盖；`enabled=false` 时不传 MCP |
| `mealPeriod` | 打分与硬过滤的场景键 |
| `budgetFen` | 价格上限（分） |
| `candidateFamilies` | `mcard_four` / `dual_1plus1` / `dabao` / `classic_three` / `coffee_combo` |

打印履约摘要（不扩候选、不下单）：

```bash
python3 profile_lib.py lunch breakfast
```

### 偏好与选择

| 字段 | 含义 |
|---|---|
| `prefs.priority` | 软分维度权重顺序 |
| `prefs.requireCoffee` | true 时硬过滤无咖啡组合 |
| `prefs.drink` | 饮料 code、名称正则、冰量。`preferCodes` 是软排序，不是唯一饮品 |
| `prefs.preferSideCodes` | 小食软排序；空数组表示不额外偏好，仍枚举菜单小食轮 |
| `selection.mode` | `rank_only` 取最高分；`rank_then_random` 在 Top-K 内抽取 |
| `selection.topK` / `diversifyByMain` / `maxPerMainInTopK` / `diversifyByFamily` | 候选池构成 |
| `history` / `historyPath` | 少重复；磁盘格式见下 |
| `historyWindowPicks` / `historyWindowDays` | 各轨近 N 次、近 N 天，取交集；设为 0 关闭 |
| `prefs.unseenMainBoost` / `unseenSideBoost` / `unseenDrinkBoost` / `unseenComboCap` | 窗口内未见主食、小食、饮品的抬权系数与 `low_repeat` 上限（默认 1.22 / 1.12 / 1.12 / 1.32） |
| `prefs.familyRepeatPenalty` | 家族轮换因子，作用于总分（默认 0.55，1 关闭） |
| `paths` | 默认菜单、detail 目录与输出路径，相对 `engine/` |

## CLI

```bash
cd engine

# 扩候选（只读，输入当日 meal-details 与精简菜单）
python3 build_candidates.py --profile lunch -o runs/request-lunch-built.json
python3 build_candidates.py --profile breakfast -o runs/request-breakfast-built.json

# 打分（固定 seed 可复现）
python3 score_lunch.py --profile lunch -i runs/request-lunch-built.json --seed 1 --pretty

# 覆盖偏好而不改 profile
python3 score_lunch.py --profile lunch -i runs/request-lunch-built.json \
  --prefs-file /tmp/prefs-overlay.json --seed 2

# 分轨历史：串行多轮，每轮记录中选项后再打下一轮
python3 score_lunch.py --profile lunch -i runs/request-lunch-built.json \
  --history-file runs/history/lunch.json --reset-history --record-pick \
  --seeds 1-10 --pretty
```

输出与退出码：

- stdout 为 `RecommendResponse`，`items` 可直接交给外部调用方做 `calculate-price`
- 离线引擎始终输出 `priceVerified=false`；输入中的历史验价标记不会被沿用
- 价态三值（`priceState`）：`estimate` 只有估价、`quoted` 有当前有效核价、`stale` 报价过期或与 items/上下文不匹配
- 任何 `estimate` / `stale` 都输出 `needsQuoteBeforeConfirm=true`，`humanSummary` 用「估价」而不是「实付」；只有 `quoted` 才可能显示实付
- `payableFen` 仅在 `quoted` 时有值；估价、过期报价、被拒报价一律为 `null`
- 退出码：`0` 成功，`1` 无候选，`2` 输入错误或快照契约错误
- 显式 `candidates=[]`、候选全部被过滤、或 fallback 门店不匹配时均不使用历史 fallback，失败响应 `items=[]`

### 预算口径

堂食按商品价判定。外送实付含打包费与配送费，且两者是否减免取决于订单金额门槛与当期活动，因此：

- 有报价时按 `payableFen` 判定，可产生 `over_budget_payable`
- 无报价时仅当商品价已超预算才拒绝，否则放行但标记 `needsQuoteBeforeConfirm`

调用方可将 `calculate-price` 的结果按 `rotationKey` 回填到请求的 `priceQuotes`，引擎据此覆盖商品价并写入实付价。

### 历史格式

`runs/history/<profile>.json` 为 v2 文档：`mains[]`、`sides[]`、`drinks[]`、`entries[]`。`--record-pick` 会把组合内全部主食、小食和饮品写入对应轨，同时追加一条 `entries`。历史按追加顺序判断新旧——同一次运行可能写入相同时间戳。

降权与抬权规则：

- **已选降权**：窗口内出现过的 code，距当前越近降幅越大（`_recency_multiplier`）
- **小食和饮品看整单**：四件套里的第二份小食、以及饮品轮选出的饮品码，都参与各自轨道的降权。不只看 `primarySideCode`，也不只看 `preferCodes`
- 同一轨有多个 code 时只取最强的一次降权，不把两份小食的系数相乘
- **未见抬权**：窗口非空且该轨仍有未见 code 时乘对应系数；每轨最多抬一次。主食、小食、饮品可同时抬，但 `low_repeat` 不得超过 `lr_cap × unseenComboCap`
- **空历史不抬权**，避免整体抬升淹没其它维度
- 已选与未见对同一 code 互斥，不双计；两者均为软分，不影响预算硬门槛

### 曝光 / 跳过 / 换餐 / 成交分轨

history 只记录成交，因此"昨天推过但没点"在打分侧不可见。需要时由调用方提供独立的曝光日志（`exposure_log`，请求里用 `exposurePath` 或内联 `exposureEvents`）：

- `exposure` 曝光、`skip` 跳过、`swap` 换餐、`deal` 成交四类事件分开写，互不合并
- 写入幂等：同一 `(kind, 作用域, 日期, rotationKey, seq)` 只写一次
- 跳过不写成交，也不推断"用户不喜欢"；小样本只做有上限的轮换降权
- 没有曝光日志时不产生任何推断，`low_repeat` 口径不变

详见 [../docs/observability-and-integration.md](../docs/observability-and-integration.md)。

## 打分与选择

软分维度：`deal` / `satiety` / `novelty` / `low_repeat` / `drink` / `mcard_four` / `dual_1plus1` / `classic_three` / `coffee`，按 `prefs.priority` 重分配权重。

硬过滤：预算、饱腹结构（主食/小食/饮）、白区饮料去重、`requireCoffee`、鸡块不得作主食。

### 家族轮换

`low_repeat` 只占一个维度，单靠它无法改变家族归属：某家族即使被降权，其余维度仍足以让它继续中选，而且连锁套餐更换子项就会产生新的 `rotationKey`，绕开降权。

因此排序前对总分再施加一次家族因子：上一餐所属家族乘 `familyRepeatPenalty`，上上餐家族乘其平方根。家族由 `rotationKey` 前缀判定，历史文件无需额外字段。

`selection.mode=rank_only` 时直接取调整后的最高分；`rank_then_random` 在按家族轮转、主食去重后的 Top-K 池中抽取。

## 调用方链路

```text
query-meals → query-meal-detail（按 candidateFamilies）
  → build_candidates.py --profile <name>
  → score_lunch.py --profile <name> -i <built>
  → calculate-price
  → 用户确认
  → 外部调用方 create-order
```

- 预约场景需带物化后的 `reservationDate` 与外送参数；外送不要复用堂食菜单缓存
- 会员资格导致的算价或下单失败由调用方处理，引擎不校验会员状态
- 本仓库脚本禁止 `create-order`，也不提供常驻 HTTP 服务

## 验证

```bash
python3 engine/evaluate_recommendations.py
python -m unittest discover -s engine/tests -v
```

`engine/tests/` 覆盖预算口径、选择语义、历史窗口与菜单字段适配；评估场景与指标口径见 `../docs/recommendation-evaluation.md`，预算与召回的相关结论见 `../docs/mcp-budget-and-recall.md`，快照兼容规则见 `../docs/mcp-contract.md`。
