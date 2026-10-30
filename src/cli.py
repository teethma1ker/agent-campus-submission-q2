"""命令行入口。

  python -m src.cli                          # 列出任务请求
  python -m src.cli --requests Q2-01         # 运行单个场景（默认 mock）
  python -m src.cli --requests Q2-08,Q2-09   # 演示断点续跑（同一上下文顺序执行）
  python -m src.cli --all                    # 全部场景
  python -m src.cli --requests Q2-01 --model real   # 真实模型
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import Agent, Limits  # noqa: E402
from gateway import build_gateway, load_dotenv  # noqa: E402
from tools import load_context  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_requests() -> list[dict]:
    with open(os.path.join(ROOT, "data", "requests.json"), encoding="utf-8") as f:
        return json.load(f)


def make_ctx(req: dict) -> dict:
    ctx = load_context(os.path.join(ROOT, "data"))
    fault_spec = req.get("faults") or {}
    for tool, spec in fault_spec.items():
        ctx["faults"][tool] = dict(spec)
    if req.get("existing_plan"):
        ep = req["existing_plan"]
        ctx["plans"][req["id"]] = {
            "plan_id": f"P{len(ctx['plans']) + 1001}", "request_id": req["id"],
            "approach_id": ep["approach_id"], "approach_name": ctx["approaches"][ep["approach_id"]]["name"],
            "material_ids": ctx["approaches"][ep["approach_id"]].get("use_materials", []),
            "cost_lines": [], "total_cost": ep["total_cost"], "total_minutes": ep["total_minutes"],
            "budget": req.get("budget"), "budget_left": (req.get("budget") or 0) - ep["total_cost"],
            "script_id": "S000", "saved_at": "2026-10-31T09:00:00",
        }
    return ctx


def make_payload(req: dict, ctx: dict, prior_steps: list | None = None) -> dict:
    return {
        "id": req["id"],
        "date": req.get("date", "2026-10-31"),
        "request": req.get("request", ""),
        "task": {"theme": req["theme"], "budget": req.get("budget"),
                 "deadline_hours": req.get("deadline_hours")},
        "candidates": [
            {**ctx["approaches"][cid], "items": ctx["approaches"][cid]["items"]}
            for cid in req["candidates"] if cid in ctx["approaches"]
        ],
        "cost_table": {**ctx["costs"]["tools"], **ctx["costs"]["services"]},
        "existing_plan": ctx["plans"].get(req["id"]),
        "prior_steps": prior_steps or [],
    }


def print_result(req: dict, result, mode: str) -> None:
    rep = result.report
    print(f"\n=== {req['id']}｜{req['scenario']}｜模型 {mode} ===")
    print(f"状态：{rep['status']}（{rep['status_note']}）｜步数 {rep['steps_count']}｜工具调用 {rep['tool_calls']}")
    print(f"计划：{'已保存 ' + rep['plan']['plan_id'] + ' @ ' + rep['plan']['approach_id'] + ' 成本 ' + str(rep['plan']['total_cost']) if rep['plan'] else '未保存'}"
          f"｜预算 {rep['cost_summary']['budget']}｜剩余 {rep['cost_summary']['budget_left']}")
    if rep["reroutes"]:
        for r in rep["reroutes"]:
            print(f"改选：{r['from']}({r['from_cost']}，{r['from_issue']}) → {r['to']}({r['to_cost']})")
    print(f"步骤：成功 {rep['step_counts']['succeeded']}｜失败 {rep['step_counts']['failed']}｜本次 {rep['step_counts']['this_run']}")
    for st in rep["steps"]:
        print(f"  [{st['seq']}] {st['tool']} {json.dumps(st['args'], ensure_ascii=False)} → {st['status']}｜{st['summary']}")
    if rep["model_summary"]:
        print(f"模型总结：{rep['model_summary']}")
    if not rep["claim_check"]["passed"]:
        print(f"!! 声明核对未通过：{rep['claim_check']['issues']}")


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser(description="内容任务规划 Agent")
    ap.add_argument("--requests", help="逗号分隔请求 ID")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--model", choices=["mock", "real", "auto"], default=None)
    ap.add_argument("--limit-steps", type=int, default=24)
    ap.add_argument("--limit-tools", type=int, default=18)
    ap.add_argument("--trace-dir")
    args = ap.parse_args()

    reqs = load_requests()
    by_id = {r["id"]: r for r in reqs}
    mode = args.model or os.environ.get("MODEL_MODE", "mock")

    if not args.requests and not args.all:
        print(f"共 {len(reqs)} 个任务请求：\n")
        for r in reqs:
            print(f"{r['id']:<7}{r['scenario']:<30}主题={r['theme']} 预算={r.get('budget')} 截止={r.get('deadline_hours')}h")
        print("\n运行示例：python -m src.cli --requests Q2-01（全部：--all）")
        return 0

    chosen = reqs if args.all else [by_id[i.strip()] for i in args.requests.split(",") if i.strip() in by_id]
    if not chosen:
        print("没有可运行的请求")
        return 2

    state: dict[str, dict] = {}
    failures = 0
    for req in chosen:
        resume_of = req.get("resume_of")
        if resume_of and resume_of in state:
            ctx = state[resume_of]["ctx"]
            prior_steps = [s for s in ctx["steps"]]
            payload = make_payload(req, ctx, prior_steps)
            # 续跑请求的故障已修复：移除故障注入
            ctx["faults"].clear()
        else:
            ctx = make_ctx(req)
            payload = make_payload(req, ctx)
        state[req["id"]] = {"ctx": ctx}
        gateway = build_gateway("mock" if mode == "mock" else "real")
        limits = Limits(max_steps=req.get("limit_steps", args.limit_steps),
                        max_tool_calls=req.get("limit_tools", args.limit_tools))
        result = Agent(gateway, ctx, limits=limits).run_request(payload)
        print_result(req, result, mode)
        if args.trace_dir:
            os.makedirs(args.trace_dir, exist_ok=True)
            path = os.path.join(args.trace_dir, f"{req['id']}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"request": req, "mode": mode, "status": result.status,
                           "report": result.report, "trace": result.trace},
                          f, ensure_ascii=False, indent=2)
            print(f"trace 已写入 {path}")
        if result.status in ("model_error", "model_protocol_error"):
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
