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
with timer.stage("calculate-price") as checkpoint:
    response = call_mcp_calculate_price()
    # 响应体里的 datetime 是接口盖章，不是工具返回时刻。
    checkpoint["mark_mcp_returned"](response)
    assemble_local_snapshots(response)
timer.finish()
```

`mark_mcp_returned(stage, response=...)` 也可以在阶段仍打开时直接调用。它只记下调用方时钟上的工具返回瞬间。上游 JSON 的 `datetime` 不会被解析，也不会写成 `mcpReturnedAt`。

阶段结束时：

- `durationSeconds`：阶段开始到 `stage_end` 的整段，和以前一样。
- `rpcDurationSeconds`：阶段开始到 `mark_mcp_returned`。没有这次标记时不写。
- `snapshotDurationSeconds`：工具返回之后到阶段结束，也就是本地快照组装。没有标记时不写。

查看：

```bash
python3 engine/stage_timer.py summarize runs/timings/lunch-20261110.jsonl
```

无 Python 嵌入时，可用 CLI（例程第一步）：

```bash
python3 engine/stage_timer.py run_start --log runs/timings/lunch.jsonl --label lunch --wrapper /path/to/mcp-wrapper
python3 engine/stage_timer.py stage-start --log runs/timings/lunch.jsonl query-meals
python3 engine/stage_timer.py mark-mcp-returned --log runs/timings/lunch.jsonl --response runs/raw/query-meals.json
python3 engine/stage_timer.py stage-end --log runs/timings/lunch.jsonl query-meals
python3 engine/stage_timer.py finish --log runs/timings/lunch.jsonl
```

`mark-mcp-returned` 只记录调用方时钟；响应里的 `datetime` 不会当作 MCP 返回时刻。

输出区分三类信息：

- `stages[]`：**有起止**的阶段才有 `durationSeconds`。调用方标记过工具返回时，同一条还会带 `rpcDurationSeconds` 和 `snapshotDurationSeconds`。
- `missingEnd[]`：只有开始、没有结束的阶段，不给时长。
- `unattributed[]`：相邻可观测时刻之间的间隔，标注 `no stage start/end inside this interval`。

规则：

- 没有起止证据的间隔不得记到菜单、详情或核价头上。
- 官方响应里的 `datetime` 是接口盖章，不是 MCP/工具调用返回的时刻。调用方必须自己标记工具返回，再把之后的快照组装分开计时。
- 仓库不输出加速倍数。没有两次可对照的实测运行时，不要宣称变快了。

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

**稳定菜单指纹**：午间复用与详情键绑定使用 `mcp_cache.stable_menu_fingerprint(menu)`（剥离 `fetchedAt` / `apiDatetime` / `traceId` 等信封字段）。`mcp_cache.fingerprint` 仍用于单条缓存条目的 payload 完整性校验；不要把带时间戳的整包 slim 直接当午间命中依据。

`menuFingerprint` 是详情缓存键的一部分。缺省指纹会被拒绝，不能当成通配。两份菜单的详情落在不同目录，读取另一份菜单得到 `missing`，不会返回上一份详情；若文件里的指纹与查找键不一致，结果是 `context_changed`。

`mcp_cache.py` CLI 的 `--root` **必填**（无默认 `engine/runs/cache`，flat runtime 必须显式传入根目录）。

菜单 TTL 默认 900s、详情 86400s（绑定稳定指纹）。**午间仍必须 live `query-meals`**（`reservationDate=""`），不得因 TTL 未过期而跳过；预热缓存只减少详情补拉。

预热目录建议隔离，例如 `runs/cache/prewarm/YYYY-MM-DD` 与 `runs/prewarm/YYYY-MM-DD/manifest.json`。失败 tombstone 不得污染主缓存。可选 `ingest-menu --expires-at` 仅作文档标记，不能作为跳过午间 `query-meals` 的理由。

预约键 `reservationDate="YYYY-MM-DD 12:00"` 与即时堂食键 `reservationDate=""` 隔离；确认内容为午餐菜单后，用 `recommend_run.py promote` 复制到即时键（见下文）。

损坏的 `entry.json`、payload 指纹不符、损坏的 tombstone 都不是命中。`prune` 会删除损坏条目和过期超过保留窗口的条目，**不会**删除 tombstone；失败标记要到下一次成功 `put` 才清除。

**缓存价不是已核实价。** 缓存只保存菜单/详情快照，打分输出的仍是 `estimate`，仍需实时 `calculate-price`。预热只是把菜单和详情提前取好，不预热任何"实付"。这些缓存、计时和换餐同步模块没有接到午餐/早餐例程里；例程不调用它们之前，不能说已经生效。

## 4. 价态（`engine/quote_state.py`、`engine/budget.py`）

| 价态 | 含义 | 用户文案 | `needsQuoteBeforeConfirm` |
|---|---|---|---|
| `estimate` | 只有菜单价/估价 | `估价 ¥x` | `true` |
| `quoted` | 当前流程 `calculate-price` 且未过期、items 与上下文匹配 | `实付 ¥x` | `false` |
| `stale` | 曾报价但过期，或 items/门店/预约已变 | `估价 ¥x（核价已过期，需重新核价）` | `true` |

`apply_quote` 会拒绝以下报价：缺少 `price`、没有 `quotedAt`/`expiresAt`、超过 TTL、`expiresAt` 已过、报价时间明显晚于当前时间、候选带有 items 但报价没有可绑定的 items、`items` 指纹不同、请求里已经填写的门店/履约/预约字段在报价中缺失或不一致。被拒绝时清空 `payableFen` 并置 `quoteRejected` / `quoteStale`，旧"实付"不会继续显示。

报价省略某个上下文字段不算匹配。堂食报价不能确认外送单，午餐报价不能确认次日早餐预约。

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

- 写入幂等：同一 `(kind, 作用域, 日期, rotationKey, seq)` 只写一次，重复调用返回 `duplicate`。调用方自带的 `eventKey` 不会另算一条。
- 日志文件损坏时拒绝覆盖。`summary` 以退出码 2 报告 `unreadable`，不打印全 0 计数。打分响应带 `exposureLogUnreadable=true`，此时 `priorExposure=false` 不能读成从未曝光；不降权，也不写成厌恶。
- 跳过 **不**写成交，也不写"用户不爱"。
- 打分侧通过请求的 `exposurePath` 或内联 `exposureEvents` 读取；没有日志时完全不推断。例程未传入这份日志时，曝光分轨不会生效。
- 已曝光未接受只给有上限的轮换降权（默认 `exposure^0.88 × skip^0.9`，下限 0.5），并在响应里输出 `priorExposure` / `exposureCount` / `skipCount` / `exposurePenalty` / `exposurePenaltyReason`。
- 成交仍只由既有的 history 轨道表达；曝光日志不新增成交。

成交、跳过、曝光分别属于不同事实。早餐"多次跳过"应记为多条 `skip`，不能被读成"不喜欢"。

## 6. 点名套餐与换餐同步（`engine/combo_edit.py`）

- `plan_named_combo` 对每一轮排序：显式偏好 > 未近期吃过 > 差价小；零加价项**不会**仅因免费而默认胜出。`keep` 只能钉住该轮真实存在的 code：code 不存在时回退到排序结果，但 `pinned` 为 false，并写入 `pinMissed` / `unresolvedKeeps`。不会凭空生成 SKU。
- `describe_swap` 列出 pending 与 calc 的逐轮差异。
- `sync_snapshots` 以 calc 快照为唯一价格来源重建 pending：items、`roundList`、价态、文案一并同步。calc 缺少 items 时直接拒绝（`calc_items_missing`），不会把 pending 的商品当成已核价。两者描述不同订单时拒绝（`snapshots_describe_different_orders`）。门店或履约不一致时拒绝（`quote_context_mismatch`）。
- 报价过期时 pending 进入 `estimate_requote_required`，绝不会停留在 `quoted_awaiting_confirm`。成功同步后，`humanSummary`、`roundList` 和金额都来自同一份 calc。

## 7. 候选家族诊断（`engine/build_candidates.py`）

`request-*-built.json` 新增 `candidateDiagnostics`，并在 CLI 摘要里输出 `familyYield` / `familyBlockers`。声明的家族产出为 0 时给出具体原因：

- `missing_detail:<code>`：缺 `query-meal-detail` 快照
- `over_budget:<legs>=<base>><budget>`：基础价已超预算
- `missing_menu_price:<code>`：菜单无 `currentPrice`
- `no_discovered_legs` / `no_menu_match_within_budget`

这些只是诊断，不会为了凑数把商品塞进候选池。

## 8. 午餐脚本化流水线（`engine/recommend_run.py`）

不调用 MCP；只消费 `CallDynamicTool(..., write_to_file=true)` 写下的 JSON。`--cache-root` / `--root` 必填。

| 子命令 | 作用 |
|---|---|
| `ingest-menu` |  unwrap 信封 → slim → `stable_menu_fingerprint` → `needed-details.json` + manifest → `mcp_cache put` |
| `ingest-details` | 按稳定指纹写入详情缓存；SKU 来自当日菜单 `discover_*`，硬编码仅退路 |
| `check-menu` | 午间 live 菜单与预热指纹比对：0=可复用，1=缺详情需补拉，2=预热不可用（全量 fallback） |
| `build-score-calc` | build → score → `presented[]`（`presentCount` 默认 3，A=rank_only 第一，B/C 来自 diversify 池）→ `calc-requests/{A,B,C}.json` |
| `merge-quotes` | 以 `calculate-price` 为唯一价源，经 `combo_edit.sync_snapshots` 合并三份快照 |
| `promote` | 午餐内容确认后，从预约 `reservationDate` 缓存复制 menu+details 到 `reservationDate=""` |

曝光写入不在 build/score 内完成；例程在用户看到菜单后再记 exposure。

价不得预热：预热只含菜单/详情；`calculate-price` 仍在午间 live 调用，每个 `presented` 各一次。

## 9. 仍未覆盖

- wrapper 缺失与恢复耗时：仓库不可见，只能由例程用 `mark_dependency` + `note` 记录。
- 调度排队、交接延迟：外部。
- 未观测到的阶段起点（菜单/详情/核价的请求开始时刻）：需调用方埋点；仓库不会用文件 mtime 推断耗时。
- 早餐提醒因 usage 不足失败：外部账号与额度问题。
