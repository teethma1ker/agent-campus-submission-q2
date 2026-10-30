"""场景测试：任务规划的验收点 + 续跑 + 防御。"""
import json
import unittest

from helpers import ScriptLLM, setup
from agent import Agent, Limits
from gateway import MockLLM


def run(request_id, limits=None, gateway=None):
    req, ctx, payload = setup(request_id)
    result = Agent(gateway or MockLLM(), ctx, limits=limits or Limits()).run_request(payload)
    return result, ctx


class NormalCompletionTest(unittest.TestCase):
    def test_reference_reroute_12_to_8(self):
        """参考实例：预算 10，AP-A 估算 12 超限 → 改选 AP-B 估算 8 → 脚本 → 保存。"""
        result, ctx = run("Q2-01")
        rep = result.report
        self.assertEqual(result.status, "completed")
        self.assertTrue(rep["plan_saved"])
        self.assertEqual(rep["plan"]["approach_id"], "AP-B")
        self.assertAlmostEqual(rep["plan"]["total_cost"], 8.0)
        self.assertAlmostEqual(rep["cost_summary"]["budget_left"], 2.0)
        self.assertEqual(len(rep["reroutes"]), 1)
        self.assertEqual(rep["reroutes"][0]["from"], "AP-A")
        self.assertEqual(rep["reroutes"][0]["to"], "AP-B")
        self.assertEqual(rep["step_counts"]["failed"], 0)
        # 步骤顺序体现依赖：估算 → 估算 → 脚本 → 保存
        tools = [s["tool"] for s in rep["steps"] if s["status"] == "succeeded"]
        self.assertEqual(tools, ["estimate_cost", "estimate_cost", "generate_script", "save_plan"])
        self.assertTrue(rep["claim_check"]["passed"])

    def test_published_is_not_a_concept(self):
        result, _ = run("Q2-01")
        self.assertIn("不产生任何外部动作", result.report["publishing"])


class MissingInputTest(unittest.TestCase):
    def test_missing_budget(self):
        result, ctx = run("Q2-02")
        rep = result.report
        self.assertFalse(rep["plan_saved"])
        self.assertIsNone(rep["plan"])
        self.assertIn("预算", rep["model_summary"])
        self.assertNotIn("estimate_cost", [s["tool"] for s in rep["steps"]])
        self.assertEqual(ctx["plans"], {})

    def test_missing_deadline(self):
        result, _ = run("Q2-03")
        self.assertFalse(result.report["plan_saved"])
        self.assertIn("截止时间", result.report["model_summary"])


class ToolFailureTest(unittest.TestCase):
    def test_script_failure_retries_then_stops_keeping_successes(self):
        result, ctx = run("Q2-04")
        rep = result.report
        self.assertFalse(rep["plan_saved"])
        gen = [s for s in rep["steps"] if s["tool"] == "generate_script"]
        self.assertEqual([s["status"] for s in gen], ["failed", "failed"])  # 一次重试后停止
        self.assertEqual(rep["step_counts"]["succeeded"], 2)                # 两次估算成功保留
        self.assertEqual(rep["step_counts"]["failed"], 2)
        self.assertNotIn("generate_script", [s["tool"] for s in rep["steps"] if s["status"] == "succeeded"])
        self.assertTrue(rep["claim_check"]["passed"])                       # 失败未虚报

    def test_save_failure_retries_then_reports(self):
        result, _ = run("Q2-07")
        rep = result.report
        self.assertFalse(rep["plan_saved"])
        self.assertEqual(rep["step_counts"]["succeeded"], 3)  # 2 估算 + 1 脚本
        saves = [s for s in rep["steps"] if s["tool"] == "save_plan"]
        self.assertEqual([s["status"] for s in saves], ["failed", "failed"])
        self.assertIn("未完成", rep["model_summary"])


class BudgetExceededTest(unittest.TestCase):
    def test_no_feasible_plan_reports_gap(self):
        result, ctx = run("Q2-05")
        rep = result.report
        self.assertFalse(rep["plan_saved"])
        self.assertEqual(len(rep["reroutes"]), 2)             # A→B→C 两次改选
        estimates = rep["cost_summary"]["latest_estimates"]
        self.assertEqual({e["approach_id"] for e in estimates}, {"AP-A", "AP-B", "AP-C"})
        self.assertIn("超预算 1.5", rep["model_summary"])     # 缺口明确
        self.assertEqual(ctx["plans"], {})

    def test_deadline_exceeded_reports_gap(self):
        result, _ = run("Q2-10")
        self.assertFalse(result.report["plan_saved"])
        self.assertIn("超时 75 分钟", result.report["model_summary"])


class RepeatExecutionTest(unittest.TestCase):
    def test_existing_plan_not_duplicated(self):
        result, ctx = run("Q2-06")
        rep = result.report
        self.assertTrue(rep["plan_saved"])
        self.assertEqual(rep["tool_calls"], 0)       # 无任何工具调用
        self.assertEqual(len(ctx["plans"]), 1)

    def test_same_store_two_runs_one_plan(self):
        from cli import make_payload
        req, ctx, payload = setup("Q2-01")
        r1 = Agent(MockLLM(), ctx).run_request(payload)
        payload2 = make_payload(req, ctx)   # 与 CLI 一致：重新提交时快照包含已有方案
        r2 = Agent(MockLLM(), ctx).run_request(payload2)
        self.assertTrue(r1.report["plan_saved"])
        self.assertTrue(r2.report["plan_saved"])      # 方案库中已有记录，仍可查询到
        self.assertEqual(r2.report["tool_calls"], 0)  # 第二次运行没有重复保存（无工具调用）
        self.assertEqual(len(ctx["plans"]), 1)


class ResumeTest(unittest.TestCase):
    def test_resume_skips_succeeded_steps(self):
        """加分项：第一段保存失败 → 第二段以历史步骤续跑，只重做失败步骤。"""
        req1, ctx, payload1 = setup("Q2-08")
        r1 = Agent(MockLLM(), ctx).run_request(payload1)
        self.assertFalse(r1.report["plan_saved"])
        self.assertEqual(r1.report["step_counts"]["succeeded"], 3)

        # 第二段：复用同一 ctx（步骤状态与草稿库延续），故障已修复
        ctx["faults"].clear()
        req2 = setup("Q2-09")[0]
        prior = list(ctx["steps"])
        from cli import make_payload
        payload2 = make_payload(req2, ctx, prior_steps=prior)
        r2 = Agent(MockLLM(), ctx).run_request(payload2)
        self.assertTrue(r2.report["plan_saved"])
        # 本次运行只有 1 个新成功步骤（save_plan），历史成功的估算/脚本未被重复执行
        self.assertEqual(r2.report["step_counts"]["this_run"], 1)
        self.assertGreaterEqual(r2.report["resumed_steps"], 1)  # 历史成功步骤被跳过
        # 本次运行没有重复生成脚本/重复估算可行方案，只补做了失败过的 save_plan
        this_run_tools = [t["action"] for t in r2.trace if t["action"] != "finish"
                          and not t["observation_summary"].startswith("resumed")]
        self.assertEqual(this_run_tools, ["save_plan"])
        self.assertEqual(len(ctx["plans"]), 1)
        self.assertTrue(r2.report["claim_check"]["passed"])


class LimitTest(unittest.TestCase):
    def test_step_limit_incomplete(self):
        result, _ = run("Q2-11", limits=Limits(max_steps=2))
        self.assertEqual(result.status, "incomplete_limit_reached")
        self.assertLessEqual(len(result.trace), 2)

    def test_tool_limit_incomplete(self):
        req, ctx, payload = setup("Q2-01")
        result = Agent(MockLLM(), ctx, limits=Limits(max_steps=50, max_tool_calls=2)).run_request(payload)
        self.assertEqual(result.status, "incomplete_limit_reached")
        self.assertFalse(result.report["plan_saved"])


class GuardrailTest(unittest.TestCase):
    def test_fake_completion_flagged(self):
        script = ScriptLLM([
            {"thought": "直接保存", "action": "save_plan", "arguments": {"approach_id": "AP-A"}},  # 依赖未满足
            {"thought": "任务已完成", "action": "finish",
             "arguments": {"summary": "方案已保存，任务已完成，预算内完成。"}},
        ])
        req, ctx, payload = setup("Q2-01")
        result = Agent(script, ctx).run_request(payload)
        self.assertFalse(result.report["plan_saved"])
        self.assertFalse(result.report["claim_check"]["passed"])
        failed_save = [s for s in result.report["steps"] if s["tool"] == "save_plan" and s["status"] == "failed"]
        self.assertEqual(len(failed_save), 1)  # 依赖缺失被工具拒绝，未伪造成功

    def test_budget_claim_without_plan_flagged(self):
        script = ScriptLLM([
            {"thought": "估一下", "action": "estimate_cost", "arguments": {"approach_id": "AP-A"}},
            {"thought": "收尾", "action": "finish", "arguments": {"summary": "任务已完成，预算内完成。"}},
        ])
        req, ctx, payload = setup("Q2-01")
        result = Agent(script, ctx).run_request(payload)
        self.assertFalse(result.report["claim_check"]["passed"])

    def test_garbage_output_then_finish(self):
        script = ScriptLLM(["不是JSON", {"thought": "收尾", "action": "finish", "arguments": {"summary": "缺少预算"}}])
        req, ctx, payload = setup("Q2-02")
        result = Agent(script, ctx).run_request(payload)
        self.assertEqual(result.status, "completed")
        self.assertEqual([t["action"] for t in result.trace].count("protocol_error"), 1)


if __name__ == "__main__":
    unittest.main()
