"""批量运行 data/requests.json 的示例请求，产出证据（traces / requests / index.md）。

  python -m src.run_examples                      # mock 模式
  python -m src.run_examples --model real         # 真实模型（写入 examples/real-model/）
  python -m src.run_examples --only Q2-08,Q2-09   # 只跑续跑两段
  python -m src.run_examples --index-only         # 仅重建索引
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import Agent, Limits  # noqa: E402
from cli import load_requests, make_ctx, make_payload, print_result  # noqa: E402
from gateway import build_gateway, load_dotenv  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    load_dotenv(os.path.join(ROOT, ".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=["mock", "real"], default="mock")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", help="逗号分隔请求 ID")
    ap.add_argument("--index-only", action="store_true")
    args = ap.parse_args()
    if args.out is None:
        args.out = os.path.join(ROOT, "examples" if args.model == "mock" else "examples/real-model")

    reqs = load_requests()
    if args.only:
        wanted = {s.strip() for s in args.only.split(",") if s.strip()}
        reqs = [r for r in reqs if r["id"] in wanted]
    os.makedirs(os.path.join(args.out, "traces"), exist_ok=True)
    os.makedirs(os.path.join(args.out, "requests"), exist_ok=True)

    index_rows = []
    ctx_pool: dict[str, dict] = {}
    for req in reqs:
        trace_path = os.path.join(args.out, "traces", f"{req['id']}.trace.json")
        if args.index_only:
            try:
                t = json.load(open(trace_path, encoding="utf-8"))
            except OSError:
                print(f"[{req['id']}] 缺少 trace，跳过")
                continue
            index_rows.append(_row(req, t["status"], t["report"], " → ".join(s["action"] for s in t["trace"])))
            continue

        resume_of = req.get("resume_of")
        if resume_of and resume_of in ctx_pool:
            ctx = ctx_pool[resume_of]
            payload = make_payload(req, ctx, prior_steps=list(ctx["steps"]))
            ctx["faults"].clear()
        else:
            ctx = make_ctx(req)
            payload = make_payload(req, ctx)
        ctx_pool[req["id"]] = ctx

        with open(os.path.join(args.out, "requests", f"{req['id']}.request.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        gateway = build_gateway(args.model)
        limits = Limits(max_steps=req.get("limit_steps", 24), max_tool_calls=req.get("limit_tools", 18))
        result = Agent(gateway, ctx, limits=limits).run_request(payload)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            print_result(req, result, args.model)
        with open(os.path.join(args.out, "traces", f"{req['id']}.console.txt"), "w", encoding="utf-8") as f:
            f.write(buf.getvalue())
        run = {"request": req, "mode": args.model, "status": result.status,
               "report": result.report, "trace": result.trace}
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(run, f, ensure_ascii=False, indent=2)
        print(buf.getvalue().splitlines()[1] + "｜" + buf.getvalue().splitlines()[2])
        index_rows.append(_row(req, result.status, result.report, " → ".join(t["action"] for t in result.trace)))

    if args.index_only:
        # 重建索引时保留顺序
        index_rows.sort(key=lambda r: [q["id"] for q in load_requests()].index(r["id"]))
    lines = [
        "# 证据索引（由 src/run_examples.py 生成）",
        "",
        f"模型模式：`{args.model}`｜请求数：{len(index_rows)}",
        "",
        "| 请求 | 场景 | 验收点 | 状态 | 计划 | 费用 | 改选 | 步骤(成功/失败) | 执行路径 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in index_rows:
        lines.append(f"| [{r['id']}](traces/{r['id']}.trace.json) | {r['scenario']} | {'、'.join(r['acceptance'])} | "
                     f"{r['status']} | {r['plan']} | {r['cost']} | {r['reroute']} | {r['counts']} | `{r['path']}` |")
    lines += ["", "每份 trace 含请求快照、逐步动作/参数/观察、决策依据、系统报告（步骤状态/费用/改选/保存结果）。",
              "console 输出见同名 `.console.txt`。"]
    with open(os.path.join(args.out, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"证据已写入 {args.out}/")
    return 0


def _row(req, status, rep, path):
    plan = rep["plan"]
    plan_txt = f"{plan['plan_id']}@{plan['approach_id']}" if plan else "未保存"
    cost = (f"{plan['total_cost']}/{rep['cost_summary']['budget']}（余 {plan['budget_left']}）"
            if plan else f"{rep['cost_summary']['budget']}")
    reroute = "；".join(f"{r['from']}→{r['to']}" for r in rep["reroutes"]) or "—"
    counts = f"{rep['step_counts']['succeeded']}/{rep['step_counts']['failed']}"
    return {"id": req["id"], "scenario": req["scenario"], "acceptance": req.get("acceptance", []),
            "status": status, "plan": plan_txt, "cost": cost, "reroute": reroute, "counts": counts, "path": path}


if __name__ == "__main__":
    raise SystemExit(main())
