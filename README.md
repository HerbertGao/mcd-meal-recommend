# mcd-meal-recommend

麦当劳中国堂食/麦乐送的离线配餐推荐引擎：按门店、预算、候选家族和偏好生成组合并排序。仓库通过快照复现推荐过程；真实菜单和实时算价由调用方通过麦当劳 MCP 获取。本项目不是麦当劳官方项目。

**不自动下单。** 推荐结果必须先经 MCP `calculate-price` 重新核价，并向用户确认后才能由外部调用方执行 `create-order`。样例中的价格、SKU 和 `priceVerified` 不代表当前可售状态。引擎输出的 `priceState` 只有 `estimate` / `quoted` / `stale` 三态，估价与过期报价不会被写成实付。

本仓库**不包含**运行例程、MCP 客户端或 box 环境脚本：wrapper 恢复、调度、网络调用都在仓库之外，仓库改动不会缩短这些外部环节的耗时。接口与接入方式见 [docs/observability-and-integration.md](docs/observability-and-integration.md)。

## 环境

- Python 3.9+；正常 build/score 只使用标准库。
- 离线示例不需要 MCP、账号或网络。

## 拉取后运行

从仓库根目录执行一条命令，完成候选构建和固定 seed 打分：

```bash
python3 engine/build_candidates.py \
  --base engine/examples/request-offline.json \
  --menu engine/examples/fixtures/menu.json \
  --details-dir engine/examples/fixtures/meal-details \
  -o engine/runs/request-offline-built.json && \
python3 engine/score_lunch.py \
  -i engine/runs/request-offline-built.json --seed 1 --pretty
```

示例使用合成门店 `000000` 和脱敏的最小菜单/detail 快照。`engine/runs/` 中的生成结果不会入库。命令中的显式相对路径以当前工作目录为基准；profile 内的 `paths` 仍以 `engine/` 为基准。

套餐候选按菜单轮次生成：先定主食，再从小食轮和饮品轮枚举，不用固定的一组小食或唯一默认饮品。近期吃过的小食、饮品都会降权。真实 `lunch.json` 不入库。

只验证已有候选时可直接运行：

```bash
python3 engine/score_lunch.py \
  -i engine/examples/candidates-sample.json --seed 1 --pretty
```

## 使用真实菜单

1. 复制模板并填写自己的履约参数：

   ```bash
   cp engine/profiles/templates/lunch.example.json engine/profiles/lunch.json
   cp engine/profiles/templates/breakfast.example.json engine/profiles/breakfast.json
   ```

   真实 `lunch.json`、`breakfast.json`、地址和历史文件均被 gitignore，不应提交。

2. 由 MCP 调用方执行 `query-meals`，保存精简菜单；按需要的候选家族执行 `query-meal-detail`，保存为 `productCode.json`。默认读取 profile 的 `paths.menuSlim` 和 `paths.detailsDir`。
3. 构建、打分、实时核价：

   ```text
   query-meals / query-meal-detail
     → build_candidates.py --profile <name>
     → score_lunch.py --profile <name> -i <built-request.json>
     → MCP calculate-price
     → 用户明确确认
     → 外部调用方才允许 create-order
   ```

完整字段和 CLI 说明见 [engine/README.md](engine/README.md)，快照兼容与上游变更流程见 [docs/mcp-contract.md](docs/mcp-contract.md)，Skill 调用约束见 [skill/SKILL.md](skill/SKILL.md)。

## 测试

测试额外使用 `jsonschema`，仅用于开发校验：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m unittest discover -s engine/tests -v
python engine/evaluate_recommendations.py
```

测试覆盖离线全链路、固定 seed、成功/失败响应 schema 和硬约束，不访问真实 MCP。推荐评估的场景与指标解释见 [docs/recommendation-evaluation.md](docs/recommendation-evaluation.md)。

## 目录

```text
engine/
  build_candidates.py       菜单/detail → 候选组合（含家族产出诊断）
  score_lunch.py            硬过滤、评分和 Top-K 选择
  profile_lib.py            profile、履约参数和历史
  quote_state.py            估价/已核价/过期三态
  budget.py                 堂食/外送预算与报价绑定
  mcp_cache.py              按门店/履约/餐段/预约隔离的快照缓存
  exposure_log.py           曝光/跳过/换餐/成交分轨
  combo_edit.py             点名套餐排序与 pending/calc 同步
  stage_timer.py            阶段计时与不可归因间隙
  examples/fixtures/        可复现的合成 MCP 快照
  profiles/templates/       午餐/早餐配置模板
  schema/                   请求与响应 JSON Schema
  tests/                    离线契约、回归与症状回放
skill/SKILL.md              外部 Agent 的 MCP 串联规则
docs/observability-and-integration.md   外部接入与可观测性说明
```

## License

[MIT](LICENSE)
