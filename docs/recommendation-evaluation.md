# 推荐评估基线

运行：

```bash
python3 engine/evaluate_recommendations.py
```

评估器只使用标准库，从 `engine/examples/fixtures/` 重新生成候选，再执行 `engine/evaluation/scenarios.json` 中的 10 个固定场景。覆盖预算等于/低于价格、主食近期/过期历史、Top-K=1、softmax 固定 seed、早餐有无咖啡和显式空候选。

## 指标

- `failedExpectations`：场景对 `ok`、主食、价格或选择模式的明确断言失败数；必须为零。
- `hardConstraintViolations`：对成功响应重新执行硬过滤发现的违规数；必须为零。
- `unexpectedVerifiedPrices`：离线结果被标记为已实时核价的数量；必须为零。
- `candidateFilterRetention`：通过硬过滤并进入排名的候选数 / 输入候选数。它用于发现过滤突变，**不是用户满意度或真实召回率**。
- `meanTopKMainCoverage`：成功场景中，排名前 K 的不同主食数 / 全部排名主食数。它衡量排名多样性，不代表随机抽取后的长期分布。

命令在任何断言、硬约束或核价状态失败时返回非零。报告不含当前时间，因此可重复比较。

## 如何使用

修改评分、候选生成或历史逻辑前先运行并保存报告；修改后比较各场景和指标。若行为变化是需求的一部分，更新对应场景断言并说明原因，不要只调高分数或放宽硬过滤。

当前没有真实用户采纳日志或人工偏好对，因此不计算偏好准确率，也不据此调整权重。取得真实标注后，再增加 pairwise 偏好准确率和连续历史回放；协同过滤、embedding、LLM 重排和 bandit 均不在当前基线内。
