# 内容任务规划 Agent

帮运营在**预算与截止时间**内完成一份内容活动方案：检索合成素材、按费用与耗时表估算方案、
在超预算时**改选替代方案**、生成脚本、保存方案；失败步骤可**续跑**，任何超限或失败都明确返回
未完成与缺口，绝不把失败步骤记为成功。

## 1. 环境与安装

- Python ≥ 3.10，**仅用标准库**，无需安装第三方依赖。
- 默认 `mock` 模式离线可跑；真实模型模式需一个 OpenAI 兼容接口（已在 DeepSeek `deepseek-chat` 上验证）。

```bash
git clone <repo-url> && cd agent-campus-submission-q2
python3 --version
python3 -m unittest discover -s tests -v      # 26 个测试，全部应通过
```

## 2. 运行（CLI 方案）

```bash
# ① 查看全部任务请求
python3 -m src.cli

# ② 参考实例：预算 10，AP-A 估算 12 超限 → 改选 AP-B 估算 8 → 脚本 → 保存
python3 -m src.cli --requests Q2-01

# ③ 失败步骤续跑（加分项）：第一段保存失败停止，第二段沿用成功步骤续跑完成
python3 -m src.cli --requests Q2-08,Q2-09

# ④ 全部 11 个场景
python3 -m src.cli --all

# ⑤ 重生成全部证据（Mock）
python3 -m src.run_examples
```

真实模型模式：

```bash
cp .env.example .env    # 编辑：MODEL_MODE=real，并设置 DEEPSEEK_API_KEY
python3 -m src.cli --requests Q2-01 --model real
python3 -m src.run_examples --model real   # 结果写入 examples/real-model/
```

离线 Mock 边界：`mock` 是确定性决策器，只验证流程、状态机与防御逻辑，**不证明真实模型的语义决策质量**；
两种模式共用同一循环、同一工具、同一份输入，可逐条对比（见 `examples/index.md` 与 `examples/real-model/index.md`）。

## 3. 评审入口

- 一页式证据索引（Mock）：`examples/index.md`
- 一页式证据索引（真实模型）：`examples/real-model/index.md`
- 参考实例：`examples/traces/Q2-01.trace.json`（12→8 改选）与 `examples/real-model/traces/Q2-01.trace.json`
- 续跑两段：`examples/traces/Q2-08.trace.json` + `Q2-09.trace.json`
- 验收点与文件对应：`docs/03-evaluation.md`
- 测试实际输出：`examples/test-results.txt`

## 4. 目录

```
src/      gateway.py(模型接入: 真实 LLM + 离线决策器)  tools.py(四工具+费用引擎+步骤状态)
          agent.py(ReAct 循环/上限/防虚报)  cli.py  run_examples.py
tests/    test_tools.py(费用引擎/依赖/幂等/失败落账)  test_scenarios.py(验收+续跑+防御)
examples/ traces/ requests/ index.md test-results.txt（Mock）；real-model/（真实模型）
data/     materials.json tool_costs.json approaches.json requests.json
docs/     01-requirements ~ 05-retrospective
```

## 5. 已知限制（详见 docs/03-evaluation.md）

- 费用表与方案库为合成数据；成本按固定单价×档位计算，未考虑浮动报价；
- 续跑基于同一进程内的步骤状态（CLI 顺序执行两段），未做跨进程状态持久化；
- 无真实发布/外部动作：`save_plan` 仅写入本地模拟方案库；
- 真实模型输出质量依赖所选模型；系统通过依赖校验、上限与声明核对接住错误，但不保证最优策略。

仓库内全部为合成数据，不含任何真实密钥或个人隐私。
