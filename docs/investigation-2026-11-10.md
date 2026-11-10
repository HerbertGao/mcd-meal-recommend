# 2026-11-10 午餐推荐延迟与质量问题：代码侧调查

本文件只记录**仓库内可复现**的结论与证据分级。运行环境数据（例程转录、文件 mtime、MCP 请求日志）不作为本仓库的长期资产，仓库内也未提交任何真实门店、地址、订单或历史。

## 1. 证据分级

| 级别 | 判定标准 | 本文中的用途 |
|---|---|---|
| A 代码可复现 | 仓库代码 + 合成 fixture 可稳定复现，且已加测试 | 归因代码缺陷 |
| B 代码可解释 | 代码路径与观测一致，但缺少完整运行时输入（如真实菜单/详情） | 提出候选机制，不下结论 |
| C 未归因 | 无起止证据 | 只列为未知 |

## 2. 耗时（约 24.5 分钟）

结论：**本次没有可用于拆分阶段的计时证据，因此不报告任何阶段耗时，也不报告加速倍数。**

可核验的事实只有两类：

1. 少量"结果时刻"（菜单响应、首个详情响应、打分文件、核价响应、交接）；
2. 这些时刻之间的长间隔。

`engine/stage_timer.py` 对同样的数据只输出 `unattributed[]`（`no stage start/end inside this interval`），不会把间隔记到菜单、详情或核价头上。核价尤其如此：核价结果时刻存在，但请求开始时刻不存在，所以**不能**把"打分完成 → 核价结果"整段当成核价耗时。

按 A/B/C 分级：

- **A**：仓库没有阶段埋点，这是仓库侧的观测缺口（已由 `stage_timer` 补上显式接口）。
- **B**：环境就绪（wrapper 缺失→补回）与 MCP 初始化占据了可观测长间隔的前段；stat 旁证与该解释相容，但不能独立证明缺失起点。
- **C**：调度排队、箱体恢复、工具启动、交接延迟的确切占比。

**仓库不控制也不修复** wrapper 恢复：本仓库不读取、不保存、不恢复任何 wrapper 或凭据，只提供"记录依赖是否存在"的接口（`mark_dependency(..., present=...)`，仅做存在性检查）。

## 3. 推荐质量

### 3.1 估价被当成可确认价（A，已修复）

`priceVerified=false` 时 `needsQuoteBeforeConfirm` 仍为 `false`。原因是 `needsQuoteBeforeConfirm` 只在**外送未报价**这一条预算路径上置位，堂食估价的候选完全不经过它。

修复后价态显式三值：

- `estimate`（估价，必须重新核价）
- `quoted`（当前有效核价，唯一可以显示实付的状态）
- `stale`（报价过期，或 items/门店/履约/预约已变）

三者共用同一套文案渲染，`payableFen` 只在 `quoted` 有值。

### 3.2 pending 与 calc 文案不一致（A，已修复）

同一组合在 score/pending/calc 三处的价格后缀不同：`≈¥23.9` / `，实付¥23.9` / 无后缀。商品码、`roundList`、金额本身一致。

修复：`combo_edit.sync_snapshots` 以 calc 快照为唯一价格来源重建 pending，items、`roundList`、价态、文案一并同步；两者描述不同订单时拒绝合并（`snapshots_describe_different_orders`）。报价过期时 pending 落到 `estimate_requote_required`，不会停留在"待确认"。

### 3.3 已曝光未接受被当成全 unseen（A/B）

history 只记录成交，因此"昨天推过、没点"在打分侧不可见。同一天同一组合可以同时是 `unseenMain/Side/Drink=true` 与"昨天刚推过"。

修复：新增 `exposure_log`，曝光 / 跳过 / 换餐 / 成交四轨独立、写入幂等。打分侧仅在调用方显式提供日志时做**有上限**的轮换降权（下限 0.5），并在响应里给出 `priorExposure`、`exposureCount`、`skipCount`、`shownNotAccepted`、`exposurePenalty`、`exposurePenaltyReason`。

明确不做的事：

- 跳过不写成交；
- 不因为小样本断言"用户不喜欢某菜"；
- 没有日志时不产生任何推断（不会凭空降权）。

注意：窗口口径本身（`historyWindowDays=5` / `historyWindowPicks=5`）会让较早成交的 code 滑出窗口，于是同一个小食可以再次被判为 unseen。这是窗口设计的直接结果，不是新 bug；`exposure_log` 让"曾经推过"这件事不再丢失。

### 3.4 声明 `dual_1plus1` 却 0 条候选（A，已给出可解释性）

`built` 请求声明了四个家族，但候选池里没有双 1+1，也没有对应商品码。代码侧两条**可复现**的阻断路径：

1. 双 1+1 需要左腿 + 右腿**两份** `query-meal-detail` 快照；缺任一就整族为 0（`expand_dual_1plus1` 在 `details.get()` 为空时直接跳过）。
2. 三件套左腿 + 真有料右腿的基础价已超过堂食预算时，该配对整族为 0（`budget_fen` 在展开阶段就不再枚举）。

现在 `build_candidates.py` 输出 `candidateDiagnostics`（CLI 里是 `familyYield` / `familyBlockers`），逐对列出 `missing_detail:<code>`、`over_budget:<legs>=<base>><budget>`、`missing_menu_price:<code>`、`no_discovered_legs`。

仍然**不做**的事：为了凑数把商品 SKU 硬塞进候选池。若两条阻断路径都不成立而结果仍为 0，说明是第三种原因（例如当日菜单根本没有该分类），需要用当日快照复现后再判断。

### 3.5 点名套餐的零加价默认（A，已给出接口）

`plan_named_combo` 按"显式偏好 > 未近期吃过 > 差价小"排序，零加价**不会**因为免费而默认胜出；`keep` 只能钉住该轮真实存在的 code。

需要说明的边界（避免过度归因）：如果某一轮在详情里只有一个可选项，那么"取第一项"与"取唯一项"是同一件事，不构成把零加价默认强加给用户。是否属于这种情况要以当日 detail 快照为准。

## 4. 未覆盖

- wrapper 缺失与恢复的实际耗时、调度排队、交接延迟：仓库不可见。
- 菜单 / 详情 / 核价的**请求开始**时刻：需要调用方埋点；仓库不会用文件 mtime 推断。
- 早餐提醒因账号额度失败：外部账号问题。
- 线上菜单是否当日真的缺双 1+1 分类：需要当日脱敏菜单快照才能定论。
- 缓存预热只能减少重复的菜单/详情拉取；它不改变核价链路，也不能把缓存价当成已核实价。
