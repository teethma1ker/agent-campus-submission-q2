"""Agent 主循环：模型选动作 → 执行工具（落账步骤状态）→ 观察结果 → 调整或结束。

与题目验收的对应：
- 计划与状态：每次工具调用记入 ctx["steps"]（success/failed），报告与续跑均以其为准；
- 冲突与失败：模型可在观察到失败后重试/换方案/停止，成功结果永不被覆盖或丢失；
- 重复执行：save_plan 幂等；--resume 时已成功步骤不再重复执行；
- 硬性上限：步数 / 工具调用，超限返回 incomplete_limit_reached；
- 防虚报：报告只取工具真值；对模型总结做“保存成功/完成/预算内”声明核对。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from gateway import SYSTEM_PROMPT, MockLLM, extract_json, render_user_prompt
from tools import build_registry, schema_error

TOOL_ACTIONS = ("search_materials", "estimate_cost", "generate_script", "save_plan")
SAVE_CLAIMS = ("已保存", "保存成功")
DONE_CLAIMS = ("任务已完成", "任务完成", "全部完成", "已完成全部")
BUDGET_CLAIMS = ("预算内", "未超预算", "不超预算")


@dataclass
class Limits:
    max_steps: int = 24
    max_tool_calls: int = 18
    max_protocol_errors: int = 5


@dataclass
class RunResult:
    status: str
    request_id: str
    trace: list[dict] = field(default_factory=list)
    report: dict = field(default_factory=dict)


def summarize(action: str, obs: dict) -> str:
    if not isinstance(obs, dict):
        return str(obs)[:140]
    if obs.get("error"):
        return f"error: {obs['error']}"
    if action == "search_materials":
        return f"命中 {obs.get('count')} 条素材：{', '.join(m['id'] for m in obs.get('materials', []))}"
    if action == "estimate_cost":
        feas = ("可行" if obs.get("feasible") is True else
                "不可行" if obs.get("feasible") is False else "无法判断")
        return (f"{obs.get('approach_id')} 成本={obs.get('total_cost')} 耗时={obs.get('total_minutes')}分钟 "
                f"预算={obs.get('budget')} 截止={obs.get('deadline_hours')}h → {feas}"
                f"{('，超预算 ' + str(obs.get('over_budget_by'))) if obs.get('over_budget_by') else ''}"
                f"{('，超时 ' + str(obs.get('over_deadline_by_min')) + '分钟') if obs.get('over_deadline_by_min') else ''}")
    if action == "generate_script":
        return f"脚本 {obs.get('script_id')}（{obs.get('approach_id')}）已生成"
    if action == "save_plan":
        if obs.get("duplicate"):
            return f"duplicate=true plan_id={obs.get('plan_id')}（已有方案，未重复保存）"
        return (f"plan_id={obs.get('plan_id')} 方案 {obs.get('approach_id')} "
                f"成本 {obs.get('total_cost')} 单位已保存")
    return str(obs)[:140]


class Agent:
    def __init__(self, gateway, ctx: dict, limits: Limits | None = None):
        self.gateway = gateway
        self.ctx = ctx
        self.limits = limits or Limits()

    def run_request(self, request: dict) -> RunResult:
        task = request["task"]
        registry = build_registry(self.ctx, task, request["id"])
        trace: list[dict] = []
        tool_calls = 0
        protocol_errors = 0
        status = "incomplete_limit_reached"
        finish_summary = None
        saved_this_run: list[str] = []
        duplicates: list[dict] = []
        failed_calls: list[dict] = []
        is_mock = isinstance(self.gateway, MockLLM)

        # 记录运行前已存在的步骤数（用于续跑统计）
        prior_steps = list(request.get("prior_steps") or [])
        prior_count = len(prior_steps)

        while True:
            if len(trace) >= self.limits.max_steps:
                status = "incomplete_limit_reached"
                break

            if is_mock:
                decision = self.gateway.decide(request, trace)
            else:
                try:
                    raw = self.gateway.complete(SYSTEM_PROMPT, render_user_prompt(request, trace))
                except Exception as e:  # noqa: BLE001
                    trace.append({"step": len(trace) + 1, "thought": "", "action": "model_error",
                                  "arguments": {}, "observation_summary": f"模型调用失败：{e}",
                                  "decision_basis": "模型不可用，终止本次运行"})
                    status = "model_error"
                    break
                try:
                    decision = extract_json(raw)
                except Exception as e:  # noqa: BLE001
                    protocol_errors += 1
                    trace.append({"step": len(trace) + 1, "thought": "模型输出不可解析",
                                  "action": "protocol_error", "arguments": {},
                                  "observation_summary": f"输出被拒绝：{e}",
                                  "decision_basis": "解析失败，未执行任何动作"})
                    if protocol_errors >= self.limits.max_protocol_errors:
                        status = "model_protocol_error"
                        break
                    continue

            action = decision.get("action")
            args = decision.get("arguments") or {}
            thought = str(decision.get("thought", ""))[:300]

            if action == "finish":
                finish_summary = str((args or {}).get("summary", ""))[:600]
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "finish",
                              "arguments": args, "observation_summary": "会话结束",
                              "decision_basis": thought})
                status = "completed"
                break

            err = schema_error(action, args)
            if err:
                protocol_errors += 1
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "invalid_action",
                              "arguments": args if isinstance(args, dict) else {},
                              "observation_summary": f"动作被拒绝：{err}",
                              "decision_basis": "动作/参数不符合工具协议，未执行"})
                if protocol_errors >= self.limits.max_protocol_errors:
                    status = "model_protocol_error"
                    break
                continue

            if tool_calls >= self.limits.max_tool_calls:
                status = "incomplete_limit_reached"
                trace.append({"step": len(trace) + 1, "thought": thought, "action": "limit_reached",
                              "arguments": {}, "observation_summary": "工具调用次数已达上限，停止执行",
                              "decision_basis": "达到工具调用上限"})
                break

            # ---- 续跑：若该动作在历史步骤中已成功，跳过重复调用
            canon = json.dumps(args, sort_keys=True, ensure_ascii=False)
            skipped = next((st for st in prior_steps
                            if st["tool"] == action and st["status"] == "succeeded"
                            and json.dumps(st["args"], sort_keys=True, ensure_ascii=False) == canon), None)
            if skipped and action != "search_materials":
                trace.append({"step": len(trace) + 1, "thought": thought, "action": action,
                              "arguments": args,
                              "observation_summary": f"resumed：该步骤上次已成功（序号 {skipped['seq']}），跳过重复执行",
                              "observation": {"ok": True, "resumed": True, **skipped.get("result", {})},
                              "decision_basis": "断点续跑：保留已成功结果，不重复执行"})
                tool_calls += 1
                continue

            try:
                obs = json.loads(registry[action](**args))
            except Exception as e:  # noqa: BLE001
                obs = {"ok": False, "error": f"工具执行异常：{e}"}
            tool_calls += 1
            if action == "save_plan":
                if obs.get("ok") and not obs.get("duplicate"):
                    saved_this_run.append(obs.get("plan_id"))
                elif obs.get("duplicate"):
                    duplicates.append({"plan_id": obs.get("plan_id")})
            if obs.get("ok") is False:
                failed_calls.append({"tool": action, "args": args, "error": obs.get("error")})

            trace.append({"step": len(trace) + 1, "thought": thought, "action": action,
                          "arguments": args, "observation_summary": summarize(action, obs),
                          "observation": obs, "decision_basis": thought})

        report = self._build_report(request, trace, status, finish_summary,
                                    saved_this_run, duplicates, failed_calls, tool_calls, prior_count)
        report["resumed_steps"] = sum(1 for t in trace
                                      if str(t.get("observation_summary", "")).startswith("resumed"))
        return RunResult(status=status, request_id=request.get("id", "request"), trace=trace, report=report)

    # -------------------------------------------------------------- 报告
    def _build_report(self, request, trace, status, finish_summary,
                      saved_this_run, duplicates, failed_calls, tool_calls, prior_count) -> dict:
        task = request["task"]
        plan = self.ctx["plans"].get(request["id"])
        steps = self.ctx["steps"]
        estimates = [s for s in steps if s["tool"] == "estimate_cost" and s["status"] == "succeeded"]
        latest_per_ap = {}
        for s in estimates:
            latest_per_ap[s["result"]["approach_id"]] = s["result"]
        scripts = [s for s in steps if s["tool"] == "generate_script" and s["status"] == "succeeded"]

        reroutes = []
        ordered = [st["result"] for st in steps if st["tool"] == "estimate_cost" and st["status"] == "succeeded"]
        for prev, cur in zip(ordered, ordered[1:]):
            if prev["approach_id"] != cur["approach_id"]:
                reroutes.append({
                    "from": prev["approach_id"], "from_cost": prev["total_cost"],
                    "from_issue": ("超预算 " + str(prev["over_budget_by"])) if prev.get("over_budget_by") else
                    ("超时 " + str(prev.get("over_deadline_by_min")) + "分钟") if prev.get("over_deadline_by_min") else "不可行",
                    "to": cur["approach_id"], "to_cost": cur["total_cost"]})

        steps_view = [{"seq": s["seq"], "tool": s["tool"], "args": s["args"],
                       "status": s["status"],
                       "status_view": "succeeded" if s["status"] == "succeeded"
                       else "failed（本次尝试未成功，结果未被记为成功）",
                       "summary": summarize(s["tool"], s["result"]) if s["status"] == "succeeded"
                       else s["result"].get("error")}
                      for s in steps]

        claim_notes = []
        text = finish_summary or ""
        negations = ("未保存", "没有保存", "保存失败", "未完成", "不能完成", "无法完成", "不可行", "未超预算",
                     "已完成步骤", "完成的部分")
        positive = text
        for n in negations:
            positive = positive.replace(n, "")
        final_ok = plan is not None
        # 只检查与“保存”直接相关的总结意图；若模型多次尝试保存且均失败，仍声称保存成功则判虚报
        save_attempts = [s for s in steps if s["tool"] == "save_plan"]
        failed_saves = [s for s in save_attempts if s["status"] == "failed"]
        if any(w in positive for w in SAVE_CLAIMS) and not final_ok and failed_saves:
            claim_notes.append("模型总结声称已保存，但保存动作均失败且方案库无记录，判定为虚报")
        elif any(w in positive for w in SAVE_CLAIMS) and not final_ok and save_attempts:
            claim_notes.append("模型总结声称已保存，但方案库无记录，判定为虚报")
        if any(w in positive for w in DONE_CLAIMS) and not final_ok:
            claim_notes.append("模型总结声称任务完成，但未保存方案，判定为虚报")
        done_claim = any(w in positive for w in DONE_CLAIMS)
        if any(w in positive for w in BUDGET_CLAIMS) and plan is None and done_claim:
            claim_notes.append("模型总结声称预算内完成任务，但未保存方案，判定为虚报")

        return {
            "request_id": request.get("id"),
            "task": task,
            "status": status,
            "status_note": {
                "completed": "本次运行动作已结束",
                "incomplete_limit_reached": "达到执行步数/工具调用上限，存在未完成步骤",
                "model_protocol_error": "模型连续输出非法协议内容，运行中止",
                "model_error": "模型调用失败，运行中止",
            }.get(status, status),
            "model_summary": finish_summary,
            "claim_check": {"passed": not claim_notes, "issues": claim_notes},
            "plan": plan,
            "plan_saved": final_ok,
            "draft_duplicate": bool(duplicates),
            "saved_this_run": saved_this_run,
            "reroutes": reroutes,
            "cost_summary": {
                "budget": task.get("budget"),
                "latest_estimates": [{"approach_id": k, "total_cost": v["total_cost"],
                                      "total_minutes": v["total_minutes"],
                                      "feasible": v.get("feasible")} for k, v in latest_per_ap.items()],
                "saved_plan_cost": plan["total_cost"] if plan else None,
                "saved_plan_minutes": plan["total_minutes"] if plan else None,
                "budget_left": plan["budget_left"] if plan else None,
            },
            "steps": steps_view,
            "step_counts": {
                "prior": prior_count,
                "this_run": len(steps) - prior_count,
                "succeeded": sum(1 for s in steps if s["status"] == "succeeded"),
                "failed": sum(1 for s in steps if s["status"] == "failed"),
            },
            "scripts": [{"script_id": s["result"]["script_id"], "approach_id": s["result"]["approach_id"]} for s in scripts],
            "failed_calls": failed_calls,
            "publishing": "模拟环境：save_plan 不产生任何外部动作",
            "tool_calls": tool_calls,
            "steps_count": len(trace),
        }
