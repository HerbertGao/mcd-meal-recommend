# MCP 快照下的预算与召回研究

记录在真实 MCP 快照上核对引擎行为的结论。文中不含真实门店、地址、账号或订单数据；示例值均以占位符表示。

## 1. 用餐规格与 profile 映射

两套规格共用同一引擎，差异只在 profile 字段。

| 维度 | 午餐 | 早餐 |
|---|---|---|
| `dining` | `eat-in` | `delivery` |
| `beType` / `orderType` | `1` / `1` | `2` / `2` |
| 门店来源 | profile `storeCode` | 由地址反查可配送店 |
| `reservation` | `{enabled: false}` | `{enabled: true, time, dayOffset}` |
| `budgetFen` | 3500 | 2000 |
| `candidateFamilies` | `mcard_four`、`dual_1plus1`、`dabao`、`classic_three` | `coffee_combo`、`mcard_four` |
| `prefs.requireCoffee` | `false` | `true` |
| `satiety.requireSideOrChicken` | `true` | `false` |

`storeCode` 与 `addressId` 必须由 profile 或 overlay 显式提供；`profile_lib.profile_to_request` 对缺失或非数字店码直接报错，不再回退到内置门店。

## 2. 召回能力

在真实快照上运行 `build_candidates.py`：

- 堂食菜单约百品、十余个可展开套餐，生成百余候选，绝大多数落在预算内。
- 外送预约菜单约六七十品，其中十余个可展开套餐，生成十余候选，均满足咖啡硬约束。
- 价格累加与 `calculate-price` 对齐：选装项按 `diffPrice` 叠加后与报价一致。

结论：在真实可售菜单上，候选生成与价格累加可用；缺陷集中在字段适配与预算口径。

## 3. 已修复缺陷

| 缺陷 | 修复 |
|---|---|
| 真实菜单字段不被识别 | `normalize_menu` 接受顶层 `meals` 并补出内部索引 `mealIndex` |
| 预算按商品价判定 | 新增 `engine/budget.py`：外送未报价返回 `unverified_delivery_total` 并标记 `needsQuoteBeforeConfirm`；有报价时按实付价判定 |
| 外送实付不可见 | 响应新增 `payableFen`（未知为 `null`）与 `needsQuoteBeforeConfirm` |
| `rank_only` 不返回最高分 | 直接返回 `ranked[0]`，不再受家族重排影响 |
| `historyWindowPicks/Days = 0` 无效 | 改用缺省值语义，`0` 可真正关闭窗口 |

## 4. 预算口径

外送实付包含商品价、打包费与配送费，后两者是否减免取决于订单金额门槛与当期活动。观测到两种情形：

| 情形 | 商品价 | 打包费 | 配送费 | 优惠 | 实付 |
|---|---|---|---|---|---|
| 未达门槛 | `P` | 收取 | 收取 | 仅打包费 | `P + 配送费` |
| 达到门槛 | `Q` | 免除 | 免除 | 打包费 + 配送费 | `Q` |

由此得出两条约束：

1. **预算必须按实付价判定。** 只看商品价会把未达免配门槛的外送单误判为合规，实际可能超支一个配送费。
2. **门槛值不可硬编码。** 它随门店、时段与活动变化；实测中相邻价格档位即出现“收费”与“全免”的分界。

因此引擎不推算费用规则，只消费 `calculate-price` 的结果。

## 5. 会员状态

### 无法查询

`tools/list` 未提供任何返回会员/卡状态的工具；关键词检索工具描述与入参均无命中。账户类工具只返回积分余额，不含卡类型。

### 无需查询

权益已由报价净掉：会员专享套餐的原价与实付在 `calculate-price` 返回中分别体现为原价字段与优惠字段，实付即最终应付金额。引擎无需建模会员等级或权益比例。

## 6. 报价回填

请求可携带 `priceQuotes`，以 `rotationKey` 索引 `calculate-price` 的 `data`：

```json
{
  "priceQuotes": {
    "<rotationKey>": {"productPrice": 0, "price": 0, "discount": 0}
  }
}
```

引擎行为：

1. `productPrice` 覆盖候选商品价，`price` 写入 `payableFen`。
2. 预算按 `payableFen` 判定，可产生 `over_budget_payable`。
3. 未提供报价的外送候选仍参与排序，但输出 `needsQuoteBeforeConfirm=true`、`payableFen=null`。

已验证：同一外送候选在回填报价前仅受商品价约束而通过，回填后按实付价被正确拒绝。

## 7. 上游契约风险

- 服务端工具名与官方文档存在拼写差异（例如 `party-store` 与 `partystore`），调用方应以 `tools/list` 为准。
- 服务端可能提供文档未列出的工具。
- 下架商品在 `query-meal-detail` 上返回专用错误码，需与参数错误区分。

处理方式见 `docs/mcp-contract.md`。

## 8. 待处理

1. `enumerate_dual_11` 从真实菜单取价时报告缺少价格，走该路径会产出空候选；主线 `build_candidates` 不受影响，但取价来源应统一。
2. 多选轮次（`quantity` / `maxQuantity` 大于 1）仍只保留单个 code，当前两套规格未用到。
3. 候选前置裁剪（每轮只枚举前若干选项、跨家族主食限额）尚未放宽。

## 9. 结论

现有 profile 已能产出满足硬约束的候选，瓶颈在预算口径与召回正确性，而不在评分权重比例。权重调整应在上述缺陷修复并有稳定报价链路后再做。
