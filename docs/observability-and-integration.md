# 可观测性与外部接入说明

本文件说明仓库内新增的离线接口，以及**必须由外部例程完成**的事项。仓库不包含例程，不读取仓库外的脚本内容，不调用 MCP，不创建订单。

## 1. 边界

| 事项 | 归属 |
|---|---|
| 菜单/详情/核价/下单调用 | 外部 MCP 客户端（不在本仓库） |
| box 重置后 wrapper / 凭据恢复 | 外部运行环境与例程（本仓库不读、不存、不恢复） |
| 调度排队、handoff | 外部例程与本地生活 Bot |
| 候选生成、打分、价态判定、曝光分轨、快照一致 | 本仓库 |

因此本仓库的改动**没有**解决 wrapper 缺失，也**没有**缩短 11/10 那次 24.5 分钟；仓库只能让下一次运行把这些时间显式记录下来。

## 2. 阶段计时（`engine/stage_timer.py`）

只写日志，不测网速、不重试、不 sleep。调用方在例程里显式起止：

```python
from engine.stage_timer import StageTimer

timer = StageTimer("runs/timings/lunch-20261110.jsonl", run_label="lunch")
# 仅做存在性检查；不要读文件内容，不要读取凭据
timer.mark_dependency("mcp-wrapper", present=has_wrapper, detail="stat only")
with timer.stage("query-meals"):
    ...
with timer.stage("query-meal-detail"):
    ...
with timer.stage("build-candidates"):
    ...
with timer.stage("score"):
    ...
with timer.stage("calculate-price"):
    ...
timer.finish()
```

查看：

```bash
python3 engine/stage_timer.py summarize runs/timings/lunch-20261110.jsonl
```

输出区分三类信息：

- `stages[]`：**有起止**的阶段才有 `durationSeconds`。
- `missingEnd[]`：只有开始、没有结束的阶段，不给时长。
- `unattributed[]`：相邻可观测时刻之间的间隔，标注 `no stage start/end inside this interval`。

规则：没有起止证据的间隔不得记到菜单、详情或核价头上。仓库不输出"加速倍数"。

### 例程建议顺序（外部实现）

1. 例程开始：`mark_dependency("mcp-wrapper", present=...)`。
2. wrapper 缺失时由例程自行恢复（恢复过程与凭据都在外部），恢复前后各打一个 `note`。
3. MCP `initialize` 前后各一个 stage。
4. `query-meals` / `query-meal-detail` / build / score / `calculate-price` 各自成段。
5. 交接给用户前 `finish()`。

## 3. 菜单/详情缓存（`engine/mcp_cache.py`）

仅本地文件缓存，隔离维度全部参与 key：

`kind`、`storeCode`、`dining`、`mealPeriod`、`reservationDate`、`beCode`、`addressId`、`productCode`、`menuFingerprint`

失效条件：

- TTL 到期或显式 `expiresAt` 到期
- 任一隔离维度不匹配（不同门店/履约/餐段/预约互不复用）
- 菜单指纹变化 → 详情条目全部失效
- 调用方用 `fail` 记录了 MCP/解析错误
- payload 无法解析或未通过调用方 `validator`

CLI（只读写本地文件）：

```bash
# 把外部已拿到的快照放进缓存
python3 engine/mcp_cache.py put --kind menu --store <STORE> --dining delivery \
  --meal-period breakfast --reservation-date "2026-11-11 07:00" \
  --be-code <BE> --address-id <ADDR> --payload /path/menu.json --ttl-seconds 600

python3 engine/mcp_cache.py put --kind detail --store <STORE> --dining delivery \
  --meal-period breakfast --reservation-date "2026-11-11 07:00" \
  --product-code <CODE> --menu-fingerprint <FP> --payload /path/detail.json

# 命中则复用，未命中以退出码 1 表示需要实时拉取
python3 engine/mcp_cache.py get --kind menu --store <STORE> ...

python3 engine/mcp_cache.py fail --kind detail --store <STORE> --product-code <CODE> --error "timeout"
python3 engine/mcp_cache.py stats
python3 engine/mcp_cache.py prune
```

`menuFingerprint` 由调用方对菜单 payload 取 `mcp_cache.fingerprint(menu)`；菜单一变，详情缓存自动判为 `context_changed`。

**缓存价不是已核实价。** 缓存只保存菜单/详情快照，打分输出的仍是 `estimate`，仍需实时 `calculate-price`。预热只是把菜单和详情提前取好，不预热任何"实付"。

## 4. 价态（`engine/quote_state.py`、`engine/budget.py`）

| 价态 | 含义 | 用户文案 | `needsQuoteBeforeConfirm` |
|---|---|---|---|
| `estimate` | 只有菜单价/估价 | `估价 ¥x` | `true` |
| `quoted` | 当前流程 `calculate-price` 且未过期、items 与上下文匹配 | `实付 ¥x` | `false` |
| `stale` | 曾报价但过期，或 items/门店/预约已变 | `估价 ¥x（核价已过期，需重新核价）` | `true` |

`apply_quote` 会拒绝以下报价：缺少 `price`、超过 TTL、`expiresAt` 已过、`items` 指纹不同、门店/履约/预约等上下文不一致。被拒绝时清空 `payableFen` 并置 `quoteRejected` / `quoteStale`，旧"实付"不会继续显示。

TTL 来源：请求或 profile 的 `quoteTtlMinutes`（默认 1800 秒）。

## 5. 曝光 / 跳过 / 换餐 / 成交（`engine/exposure_log.py`）

四种事件分轨记录，互不合并：

```bash
python3 engine/exposure_log.py --file runs/history/exposure-lunch.json record \
  --kind exposure --rotation-key <RK> --store <STORE> --dining eat-in --meal-period lunch

python3 engine/exposure_log.py --file runs/history/exposure-lunch.json record \
  --kind skip --rotation-key <RK> --store <STORE> --dining eat-in --meal-period lunch

python3 engine/exposure_log.py --file runs/history/exposure-lunch.json summary --store <STORE>
```

- 写入幂等：同一 `(kind, 作用域, 日期, rotationKey, seq)` 只写一次，重复调用返回 `duplicate`。
- 跳过 **不**写成交，也不写"用户不爱"。
- 打分侧通过请求的 `exposurePath` 或内联 `exposureEvents` 读取；没有日志时完全不推断。
- 已曝光未接受只给有上限的轮换降权（默认 `exposure^0.88 × skip^0.9`，下限 0.5），并在响应里输出 `priorExposure` / `exposureCount` / `skipCount` / `exposurePenalty` / `exposurePenaltyReason`。
- 成交仍只由既有的 history 轨道表达；曝光日志不新增成交。

成交、跳过、曝光分别属于不同事实。早餐"多次跳过"应记为多条 `skip`，不能被读成"不喜欢"。

## 6. 点名套餐与换餐同步（`engine/combo_edit.py`）

- `plan_named_combo` 对每一轮排序：显式偏好 > 未近期吃过 > 差价小；零加价项**不会**仅因免费而默认胜出。`keep` 只能钉住该轮真实存在的 code，不会凭空生成 SKU。
- `describe_swap` 列出 pending 与 calc 的逐轮差异。
- `sync_snapshots` 以 calc 快照为唯一价格来源重建 pending：items、`roundList`、价态、文案一并同步；两者描述不同订单时直接拒绝（`snapshots_describe_different_orders`），不静默合并。
- 报价过期时 pending 进入 `estimate_requote_required`，绝不会停留在 `quoted_awaiting_confirm`。

## 7. 候选家族诊断（`engine/build_candidates.py`）

`request-*-built.json` 新增 `candidateDiagnostics`，并在 CLI 摘要里输出 `familyYield` / `familyBlockers`。声明的家族产出为 0 时给出具体原因：

- `missing_detail:<code>`：缺 `query-meal-detail` 快照
- `over_budget:<legs>=<base>><budget>`：基础价已超预算
- `missing_menu_price:<code>`：菜单无 `currentPrice`
- `no_discovered_legs` / `no_menu_match_within_budget`

这些只是诊断，不会为了凑数把商品塞进候选池。

## 8. 仍未覆盖

- wrapper 缺失与恢复耗时：仓库不可见，只能由例程用 `mark_dependency` + `note` 记录。
- 调度排队、交接延迟：外部。
- 未观测到的阶段起点（菜单/详情/核价的请求开始时刻）：需调用方埋点；仓库不会用文件 mtime 推断耗时。
- 早餐提醒因 usage 不足失败：外部账号与额度问题。
