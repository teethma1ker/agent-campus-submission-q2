# 03 评估

## 测试预期与实际结果

测试命令：`python3 -m unittest discover -s tests -v`（26 个测试，实际输出见 `examples/test-results.txt`，全部通过）。
示例命令：`python3 -m src.run_examples`（11 个请求，索引见 `examples/index.md`；真实模型见 `examples/real-model/index.md`）。

| 验收点 | 预期 | 实际（证据文件） | 结果 |
|---|---|---|---|
| 正常完成 | 在预算内完成并保存方案，费用与预算核对一致 | `examples/traces/Q2-01.trace.json`：AP-A(12) 超限 → AP-B(8) 可行 → 脚本 → 保存，剩余 2.0 | 通过 |
| 缺预算 | 不强行规划，返回缺失项 | `examples/traces/Q2-02.trace.json`：仅检索后结束，报告缺预算 | 通过 |
| 缺截止时间 | 同上 | `examples/traces/Q2-03.trace.json` | 通过 |
| 工具失败（脚本） | 重试一次后停止，保留成功步骤 | `examples/traces/Q2-04.trace.json`：估算 2 次成功保留，脚本失败 2 次 | 通过 |
| 工具失败（保存） | 明确报失败，不虚报 | `examples/traces/Q2-07.trace.json`：保存失败 2 次，方案未保存 | 通过 |
| 预算超限（改选） | 12→8 改选后继续 | `examples/traces/Q2-01.trace.json`；reroutes 显示 AP-A→AP-B | 通过 |
| 预算超限（无解） | 返回未完成与缺口 | `examples/traces/Q2-05.trace.json`：三方案全超限，报告超预算 1.5 | 通过 |
| 截止时间超限 | 返回未完成与缺口 | `examples/traces/Q2-10.trace.json`：报告超时 75 分钟 | 通过 |
| 重复执行 | 已有方案不重复保存 | `examples/traces/Q2-06.trace.json`：无工具调用，方案库仍 1 份 | 通过 |
| 失败步骤续跑（加分） | 只重做未成功步骤 | `examples/traces/Q2-08.trace.json` → `Q2-09.trace.json`：第二段仅 1 个新成功步骤（save_plan），历史成功步骤全部 resumed | 通过 |
| 执行上限 | 超限返回未完成状态 | `examples/traces/Q2-11.trace.json`：incomplete_limit_reached | 通过 |
| 防虚报 | 伪装完成/保存被标注 | 测试 `test_fake_completion_flagged`、`test_budget_claim_without_plan_flagged` | 通过 |

## 真实模型（DeepSeek）补充验证

`examples/real-model/index.md` 为同组 11 个请求的真实模型运行（deepseek-chat，2026-10-30）：
参考实例完整复现（AP-A 12 超预算 → 降级 AP-B 8 → 保存，费用剩余 2.0）；失败重试、重复执行、续跑
与 Mock 一致。真实运行中发现并修复的模型问题（详见 `docs/04-ai-usage.md`）：
① 缺预算时仍尝试估算 → 工具改为直接返回缺失错误 + 提示词收紧；
② 缺截止时间时被误导性报错驱动循环重试 → 同上修复，重跑后 2 步内结束。

## 失败与限制

- 成本为确定性的单价×档位计算，未覆盖浮动报价、折扣与并发资源约束；
- 续跑依赖同一进程内状态（CLI 顺序执行），跨进程需自行持久化 `ctx["steps"]`；
- 故障注入为模拟（`faults` 字段），非真实服务故障；
- Mock 决策器只证明流程与防御逻辑；真实模型策略质量依赖模型本身，系统只保证不越权、不虚报；
- 每一步有重试上限（1 次），复杂故障场景可能过早停止（有意设计，保证可解释）。
