"""工具与费用引擎测试。"""
import json
import unittest

from helpers import setup
from tools import load_context, build_registry, price_approach, feasibility, schema_error


def fresh(task=None):
    ctx = load_context()
    task = task or {"theme": "秋季新品发布", "budget": 10, "deadline_hours": 3}
    return ctx, build_registry(ctx, task, "T1")


class CostEngineTest(unittest.TestCase):
    def test_reference_estimates(self):
        ctx, _ = fresh()
        a = price_approach(ctx, "AP-A")
        b = price_approach(ctx, "AP-B")
        c = price_approach(ctx, "AP-C")
        self.assertEqual(a["total_cost"], 12.0)   # 参考实例：初始方案 12
        self.assertEqual(b["total_cost"], 8.0)    # 参考实例：替代方案 8
        self.assertEqual(a["total_minutes"], 585)
        self.assertEqual(b["total_minutes"], 150)
        self.assertEqual(c["total_cost"], 5.5)

    def test_budget_and_deadline_flags(self):
        ctx, _ = fresh()
        a = feasibility(ctx, {"budget": 10, "deadline_hours": 3}, price_approach(ctx, "AP-A"))
        b = feasibility(ctx, {"budget": 10, "deadline_hours": 3}, price_approach(ctx, "AP-B"))
        self.assertFalse(a["feasible"])
        self.assertEqual(a["over_budget_by"], 2.0)
        self.assertEqual(a["over_deadline_by_min"], 585 - 180)  # 超出截止 3h=180 分钟
        self.assertTrue(b["feasible"])

    def test_material_license_by_quality(self):
        ctx, _ = fresh()
        # 自选素材：MA02(high=2) + MA06(low=0.5)
        pricing = price_approach(ctx, "AP-C", ["MA02", "MA06"])
        lines = {(l["item"], l["ref"]): l for l in pricing["lines"]}
        self.assertEqual(lines[("material_license", "MA02")]["cost"], 2)
        self.assertEqual(lines[("material_license", "MA06")]["cost"], 0.5)
        self.assertEqual(pricing["total_cost"], 0.5 + 2 + 0.5 + 2 + 1 + 0.5)  # 6.5
        self.assertEqual(sum(l["cost"] for l in pricing["lines"]), pricing["total_cost"])


class ToolChainTest(unittest.TestCase):
    def test_search_hits_by_tag(self):
        _, reg = fresh()
        obs = json.loads(reg["search_materials"]("秋季新品"))
        self.assertGreater(obs["count"], 0)
        self.assertTrue(any(m["id"] == "MA02" for m in obs["materials"]))

    def test_script_requires_feasible_estimate(self):
        _, reg = fresh()
        self.assertFalse(json.loads(reg["generate_script"]("AP-B"))["ok"])  # 未估算
        reg["estimate_cost"]("AP-A")  # A 超预算
        out = json.loads(reg["generate_script"]("AP-A"))
        self.assertFalse(out["ok"])
        self.assertIn("未通过", out["error"])

    def test_save_requires_script(self):
        ctx, reg = fresh()
        reg["estimate_cost"]("AP-B")
        out = json.loads(reg["save_plan"]("AP-B"))
        self.assertFalse(out["ok"])
        self.assertIn("脚本", out["error"])

    def test_full_chain_and_idempotent_save(self):
        ctx, reg = fresh()
        reg["estimate_cost"]("AP-A")
        reg["estimate_cost"]("AP-B")
        reg["generate_script"]("AP-B", "standard")
        first = json.loads(reg["save_plan"]("AP-B"))
        second = json.loads(reg["save_plan"]("AP-B"))
        self.assertTrue(first["ok"])
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(len(ctx["plans"]), 1)
        self.assertEqual(first["budget_left"], 2.0)

    def test_step_state_records_failures(self):
        ctx, reg = fresh()
        ctx["faults"]["generate_script"] = {"error": "boom", "remaining": 1}
        reg["estimate_cost"]("AP-B")
        reg["generate_script"]("AP-B")
        reg["generate_script"]("AP-B")
        statuses = [(s["tool"], s["status"]) for s in ctx["steps"] if s["tool"] == "generate_script"]
        self.assertEqual(statuses, [("generate_script", "failed"), ("generate_script", "succeeded")])


class SchemaTest(unittest.TestCase):
    def test_reject_unknown_and_incomplete(self):
        self.assertIsNotNone(schema_error("publish_content", {}))
        self.assertIsNotNone(schema_error("estimate_cost", {}))
        self.assertIsNone(schema_error("estimate_cost", {"approach_id": "AP-A"}))


if __name__ == "__main__":
    unittest.main()


class MissingInputToolTest(unittest.TestCase):
    def test_estimate_requires_budget_and_deadline(self):
        ctx = load_context()
        reg = build_registry(ctx, {"theme": "秋季新品发布", "budget": None, "deadline_hours": 3}, "T1")
        obs = json.loads(reg["estimate_cost"]("AP-A"))
        self.assertFalse(obs["ok"])
        self.assertIn("预算", obs["error"])
