# 证据索引（由 src/run_examples.py 生成）

模型模式：`mock`｜请求数：11

| 请求 | 场景 | 验收点 | 状态 | 计划 | 费用 | 改选 | 步骤(成功/失败) | 执行路径 |
|---|---|---|---|---|---|---|---|---|
| [Q2-01](traces/Q2-01.trace.json) | 正常完成（参考实例：12→8 改选） | 正常完成、预算超限 | completed | P1001@AP-B | 8.0/10（余 2.0） | AP-A→AP-B | 4/0 | `estimate_cost → estimate_cost → generate_script → save_plan → finish` |
| [Q2-02](traces/Q2-02.trace.json) | 缺预算 | 缺预算或截止时间 | completed | 未保存 | None | — | 1/0 | `search_materials → finish` |
| [Q2-03](traces/Q2-03.trace.json) | 缺截止时间 | 缺预算或截止时间 | completed | 未保存 | 10 | — | 1/0 | `search_materials → finish` |
| [Q2-04](traces/Q2-04.trace.json) | 工具失败（脚本生成连续失败） | 工具失败 | completed | 未保存 | 10 | AP-A→AP-B | 2/2 | `estimate_cost → estimate_cost → generate_script → generate_script → finish` |
| [Q2-05](traces/Q2-05.trace.json) | 预算超限（无可行方案） | 预算超限 | completed | 未保存 | 4 | AP-A→AP-B；AP-B→AP-C | 3/0 | `estimate_cost → estimate_cost → estimate_cost → finish` |
| [Q2-06](traces/Q2-06.trace.json) | 重复执行（已有方案） | 重复执行 | completed | P1001@AP-B | 8.0/10（余 2.0） | — | 0/0 | `finish` |
| [Q2-07](traces/Q2-07.trace.json) | 工具失败（方案保存失败） | 工具失败 | completed | 未保存 | 10 | AP-A→AP-B | 3/2 | `estimate_cost → estimate_cost → generate_script → save_plan → save_plan → finish` |
| [Q2-08](traces/Q2-08.trace.json) | 失败步骤续跑·第一段（保存失败停止） | 工具失败、续跑 | completed | 未保存 | 10 | AP-A→AP-B | 3/2 | `estimate_cost → estimate_cost → generate_script → save_plan → save_plan → finish` |
| [Q2-09](traces/Q2-09.trace.json) | 失败步骤续跑·第二段（修复后续跑完成） | 续跑 | completed | P1001@AP-B | 8.0/10（余 2.0） | AP-A→AP-B | 4/2 | `estimate_cost → save_plan → finish` |
| [Q2-10](traces/Q2-10.trace.json) | 截止时间超限（无可行方案） | 预算超限 | completed | 未保存 | 10 | AP-A→AP-B；AP-B→AP-C | 3/0 | `estimate_cost → estimate_cost → estimate_cost → finish` |
| [Q2-11](traces/Q2-11.trace.json) | 执行上限（未完成状态） | 执行上限 | incomplete_limit_reached | 未保存 | 10 | AP-A→AP-B | 2/0 | `estimate_cost → estimate_cost` |

每份 trace 含请求快照、逐步动作/参数/观察、决策依据、系统报告（步骤状态/费用/改选/保存结果）。
console 输出见同名 `.console.txt`。
