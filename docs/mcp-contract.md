# MCP 快照契约与变更处理

本仓库不实现 MCP 客户端。调用方负责身份、门店/地址参数和网络请求，引擎只读取已保存的 JSON 快照。这样可以在无账号环境中复现问题，也避免推荐脚本获得下单能力。

## 数据流

```text
MCP query-meals → menu JSON ─┐
                            ├→ build_candidates → score_lunch → 推荐结果
MCP query-meal-detail ──────┘                                  ↓
                                      外部调用方 calculate-price ← items
```

`engine/mcp_contract.py` 是菜单/detail 的入口边界。候选构建使用规范化后的轮次和选项；最终 `items` 只是提交给外部 `calculate-price` 的请求材料，不是已确认订单。

## 已支持的 detail 形态

| 含义 | 接受字段 |
|---|---|
| 轮次数组 | `rounds` 或 `roundList` |
| 轮次 ID | `id` 或 `round` |
| 选项数组 | `choices`、`comboItemList` 或 `products` |
| 选项 code | `code` 或 `productCode` |
| 选项名称 | `name`、`productName` 或 `title` |
| 选项差价 | `diffPrice`，例如 `+¥1.50` |

轮次数组必须非空；每个轮次必须有 ID 和选项数组；每个选项必须有 code，非空 `diffPrice` 必须含带符号金额。菜单根节点必须是 object，`mealIndex` 必须是 object，`categories` 及其中的 `meals` 必须是 array；`currentPrice` 出现时必须是非负数值，候选定价所需价格缺失时会报错。

未识别的额外字段会保留在原始快照中并被解析器忽略，因此上游新增可选字段不会破坏推荐。`engine/examples/fixtures/` 同时覆盖 `rounds/choices` 和 `roundList/comboItemList`。

## 上游变更时

1. 保存新的脱敏 menu/detail 快照，不保存地址、账号、订单或身份信息。
2. 运行 `python -m unittest discover -s engine/tests -v` 和 README 的离线全链路命令。
3. 若字段缺失或类型改变，CLI 会返回非零状态并报告快照及字段路径。先在 `mcp_contract.py` 增加有证据的别名或修正调用方导出的快照，不要在各个评分分支中分别打补丁。
4. 若结构语义改变，先增加最小失败夹具，再修改规范化或候选生成。不要通过放宽预算、饱腹或饮料硬过滤来掩盖空数据。
5. 用真实 MCP 重新核价和确认可售性；离线测试通过不代表线上接口仍然可用。

## 价格与 fallback

- `build_candidates.py` 生成的候选始终为 `priceVerified=false`。
- 离线评分会丢弃输入候选和固定模板中的历史 `priceVerified=true`，并输出警告。只有外部调用方在当前流程完成 `calculate-price` 后，才能将结果视为已核价。
- 显式提供 `candidates` 时，空候选或全部候选被过滤都会返回 `ok=false`、`items=[]`，不会退回历史 SKU。
- 固定模板只服务于未提供候选的旧演示请求；模板默认门店与请求门店不一致时禁用。它不是 MCP 故障时的实时备用菜单。

## 不在此层处理

网络超时、重试、鉴权刷新和限流属于外部 MCP 调用方。本仓库没有在线客户端，因此不引入断路器、后台刷新或运行时插件。
