"""业务工具：search_materials / estimate_cost / generate_script / save_plan。

- 费用引擎：严格按 data/tool_costs.json 计价，选材质量影响授权费。
- 步骤依赖：脚本生成要求存在“通过的估算”；保存方案要求“估算通过 + 脚本成功”。
- 步骤状态：每次工具调用（成功/失败）都记入 ctx["steps"]，供报告与续跑使用。
- 幂等：同一请求重复保存只保留一份方案，返回 duplicate=true。
- 故障注入：save_plan / generate_script 可注入失败，用于验证失败处理与续跑。
"""
from __future__ import annotations

import json
import os
from datetime import datetime

ALLOWED_SCRIPT_TIERS = ("premium", "standard", "template")


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_context(data_dir: str = "data") -> dict:
    costs = load_json(os.path.join(data_dir, "tool_costs.json"))
    return {
        "materials": {m["id"]: m for m in load_json(os.path.join(data_dir, "materials.json"))},
        "approaches": {a["id"]: a for a in load_json(os.path.join(data_dir, "approaches.json"))},
        "costs": costs,
        "plans": {},                      # request_id -> plan（幂等存储）
        "steps": [],                      # 步骤状态流水（成功/失败均记录）
        "seq": 0,
        "faults": {},                     # {"generate_script": {...}, "save_plan": {...}}
        "script_seq": 0,
    }


# ---------------------------------------------------------------- 费用引擎
def tier_price(costs: dict, name: str, tier: str) -> dict:
    table = costs["tools"].get(name) or costs["services"].get(name)
    if not table or "tiers" not in table:
        raise KeyError(f"费用表缺少 {name}")
    if tier not in table["tiers"]:
        raise KeyError(f"{name} 不支持档位 {tier}（可选：{', '.join(table['tiers'])}）")
    return table["tiers"][tier]


def price_approach(ctx: dict, approach_id: str, material_ids: list[str] | None = None) -> dict:
    ap = ctx["approaches"].get(approach_id)
    if not ap:
        raise KeyError(f"方案 {approach_id} 不存在")
    mats = ctx["materials"]
    if material_ids is None:
        material_ids = list(ap.get("use_materials", []))
    has_license = any(i["name"] == "material_license" for i in ap["items"])
    if material_ids and not has_license:
        raise KeyError(f"方案 {approach_id} 不含素材授权项，不能选材")
    unknown = [m for m in material_ids if m not in mats]
    if unknown:
        raise KeyError(f"素材不存在：{', '.join(unknown)}")
    if has_license and not material_ids:
        raise KeyError(f"方案 {approach_id} 需要素材授权，请提供 material_ids")

    lines, total_cost, total_minutes = [], 0.0, 0
    for item in ap["items"]:
        if item["name"] == "material_license":
            for mid in material_ids:
                mat = mats[mid]
                price = ctx["costs"]["services"]["material_license"]["tiers"][mat["quality"]]
                total_cost += price["cost"]
                total_minutes += price["minutes"]
                lines.append({"item": "material_license", "tier": mat["quality"],
                              "ref": mid, "cost": price["cost"], "minutes": price["minutes"]})
            continue
        price = tier_price(ctx["costs"], item["name"], item["tier"])
        total_cost += price["cost"]
        total_minutes += price["minutes"]
        lines.append({"item": item["name"], "tier": item["tier"], "ref": None,
                      "cost": price["cost"], "minutes": price["minutes"]})
    return {"approach_id": approach_id, "approach_name": ap["name"],
            "material_ids": material_ids, "lines": lines,
            "total_cost": round(total_cost, 2), "total_minutes": total_minutes}


def feasibility(ctx: dict, task: dict, pricing: dict) -> dict:
    budget = task.get("budget")
    deadline = task.get("deadline_hours")
    budget = None if budget in (None, "") else float(budget)
    deadline = None if deadline in (None, "") else float(deadline)
    deadline_minutes = None if deadline is None else int(deadline * 60)
    within_budget = None if budget is None else pricing["total_cost"] <= budget + 1e-9
    within_deadline = None if deadline_minutes is None else pricing["total_minutes"] <= deadline_minutes
    feasible = None if (within_budget is None or within_deadline is None) else (within_budget and within_deadline)
    return {"budget": budget, "deadline_hours": deadline,
            "deadline_minutes": deadline_minutes,
            "within_budget": within_budget, "within_deadline": within_deadline,
            "feasible": feasible,
            "over_budget_by": None if (budget is None or within_budget) else round(pricing["total_cost"] - budget, 2),
            "over_deadline_by_min": None if (deadline_minutes is None or within_deadline) else pricing["total_minutes"] - deadline_minutes}


# ---------------------------------------------------------------- 步骤流水
def record_step(ctx: dict, tool: str, args: dict, status: str, result: dict) -> int:
    ctx["seq"] += 1
    ctx["steps"].append({
        "seq": ctx["seq"], "tool": tool, "args": args, "status": status,
        "result": result, "at": datetime.now().isoformat(timespec="seconds"),
    })
    return ctx["seq"]


def find_succeeded(ctx: dict, tool: str, args: dict) -> dict | None:
    canon = json.dumps(args, sort_keys=True, ensure_ascii=False)
    for st in reversed(ctx["steps"]):
        if st["tool"] == tool and st["status"] == "succeeded" \
                and json.dumps(st["args"], sort_keys=True, ensure_ascii=False) == canon:
            return st["result"]
    return None


def latest_estimate(ctx: dict, approach_id: str) -> dict | None:
    cands = [s for s in ctx["steps"] if s["tool"] == "estimate_cost" and s["status"] == "succeeded"]
    for st in reversed(cands):
        if st["result"].get("approach_id") == approach_id:
            return st["result"]
    return None


def script_done(ctx: dict, approach_id: str) -> dict | None:
    cands = [s for s in ctx["steps"] if s["tool"] == "generate_script" and s["status"] == "succeeded"]
    for st in reversed(cands):
        if st["result"].get("approach_id") == approach_id:
            return st["result"]
    return None


def _fault(ctx: dict, tool: str) -> str | None:
    fault = ctx["faults"].get(tool)
    if fault and fault.get("remaining", 0) > 0:
        fault["remaining"] -= 1
        return fault.get("error", f"{tool} 服务暂不可用")
    return None


def _fail(ctx: dict, tool: str, args: dict, error: str) -> str:
    record_step(ctx, tool, args, "failed", {"error": error})
    return json.dumps({"ok": False, "tool": tool, "error": error}, ensure_ascii=False)


def _ok(ctx: dict, tool: str, args: dict, result: dict) -> str:
    record_step(ctx, tool, args, "succeeded", result)
    return json.dumps({"ok": True, **result}, ensure_ascii=False)


# ---------------------------------------------------------------- 工具 1：素材检索
def build_search_materials(ctx: dict):
    def search_materials(query: str) -> str:
        args = {"query": query}
        q = (query or "").strip()
        if not q:
            return _fail(ctx, "search_materials", args, "检索关键词为空")
        hits = []
        tokens = [tok for tok in q.replace("，", " ").replace(",", " ").split() if tok]
        for mid, m in ctx["materials"].items():
            hay = " ".join([m["title"], m["note"], *m["tags"]])
            tag_hit = any(tag in q for tag in m["tags"])   # 中文长查询：素材标签出现在查询中
            if q in hay or tag_hit or any(tok in hay for tok in tokens):
                hits.append({"id": mid, "title": m["title"], "type": m["type"],
                             "quality": m["quality"], "tags": m["tags"], "note": m["note"]})
        return _ok(ctx, "search_materials", args,
                   {"query": q, "count": len(hits), "materials": hits[:8]})
    return search_materials


# ---------------------------------------------------------------- 工具 2：成本估算
def build_estimate_cost(ctx: dict, task: dict):
    def estimate_cost(approach_id: str, material_ids: list | None = None) -> str:
        args = {"approach_id": approach_id}
        if material_ids is not None:
            args["material_ids"] = material_ids
        missing = []
        if task.get("budget") in (None, ""):
            missing.append("预算")
        if task.get("deadline_hours") in (None, ""):
            missing.append("截止时间")
        if missing:
            return _fail(ctx, "estimate_cost", args,
                         f"任务缺少 {'、'.join(missing)}，无法评估成本是否可行；请先补充后再规划")
        fault = _fault(ctx, "estimate_cost")
        if fault:
            return _fail(ctx, "estimate_cost", args, fault)
        try:
            if material_ids is not None and not isinstance(material_ids, list):
                raise KeyError("material_ids 必须是数组")
            pricing = price_approach(ctx, approach_id, material_ids)
        except KeyError as e:
            return _fail(ctx, "estimate_cost", args, str(e))
        feas = feasibility(ctx, task, pricing)
        return _ok(ctx, "estimate_cost", args, {
            **pricing, "lines": pricing["lines"], **feas,
            "budget": feas["budget"], "deadline_minutes": feas["deadline_minutes"],
        })
    return estimate_cost


# ---------------------------------------------------------------- 工具 3：脚本生成
def build_generate_script(ctx: dict):
    def generate_script(approach_id: str, tier: str = "standard") -> str:
        args = {"approach_id": approach_id, "tier": tier}
        if tier not in ALLOWED_SCRIPT_TIERS:
            return _fail(ctx, "generate_script", args, f"不支持的脚本档位 {tier}")
        estimate = latest_estimate(ctx, approach_id)
        if estimate is None:
            return _fail(ctx, "generate_script", args, "缺少通过的估算：请先调用 estimate_cost 评估该方案")
        if estimate.get("feasible") is not True:
            return _fail(ctx, "generate_script", args,
                         f"方案 {approach_id} 的估算未通过（预算/截止时间内不可行），不能生成脚本")
        fault = _fault(ctx, "generate_script")
        if fault:
            return _fail(ctx, "generate_script", args, fault)
        ap = ctx["approaches"][approach_id]
        ctx["script_seq"] += 1
        script_id = f"S{ctx['script_seq']:03d}"
        mats = "、".join(estimate["material_ids"]) or "实拍镜头"
        script = (f"{ap['name']}｜{tier} 脚本 {script_id}\n"
                  f"开场：3 秒钩子，展示核心卖点（素材：{mats}）\n"
                  f"中段：产品特写 + 使用场景交替，节奏跟音乐鼓点\n"
                  f"结尾：行动号召 + 福利信息条幅（{tier} 档字幕样式）")
        return _ok(ctx, "generate_script", args, {
            "script_id": script_id, "approach_id": approach_id, "tier": tier,
            "script": script,
        })
    return generate_script


# ---------------------------------------------------------------- 工具 4：方案保存
def build_save_plan(ctx: dict, task: dict, request_id: str):
    def save_plan(approach_id: str) -> str:
        args = {"approach_id": approach_id}
        estimate = latest_estimate(ctx, approach_id)
        if estimate is None:
            return _fail(ctx, "save_plan", args, "缺少通过的估算：请先补齐 estimate_cost")
        if estimate.get("feasible") is not True:
            return _fail(ctx, "save_plan", args, "预算或截止时间不满足，禁止保存超限方案")
        script = script_done(ctx, approach_id)
        if script is None:
            return _fail(ctx, "save_plan", args, "脚本尚未生成成功：请先完成 generate_script")

        existing = ctx["plans"].get(request_id)
        if existing:
            return json.dumps({
                "ok": True, "duplicate": True, "plan_id": existing["plan_id"],
                "approach_id": existing["approach_id"], "total_cost": existing["total_cost"],
                "note": "该请求已有方案，未重复保存",
            }, ensure_ascii=False)

        fault = _fault(ctx, "save_plan")
        if fault:
            return _fail(ctx, "save_plan", args, fault)

        # 保存前以最新估算复核预算与时间
        feas = feasibility(ctx, task, estimate)
        if feas["feasible"] is not True:
            return _fail(ctx, "save_plan", args,
                         f"保存前复检未通过：超预算 {feas['over_budget_by']} 或超时 {feas['over_deadline_by_min']} 分钟")

        plan = {
            "plan_id": f"P{len(ctx['plans']) + 1001}",
            "request_id": request_id,
            "approach_id": approach_id,
            "approach_name": estimate["approach_name"],
            "material_ids": estimate["material_ids"],
            "cost_lines": estimate["lines"],
            "total_cost": estimate["total_cost"],
            "total_minutes": estimate["total_minutes"],
            "budget": feas["budget"],
            "budget_left": None if feas["budget"] is None else round(feas["budget"] - estimate["total_cost"], 2),
            "script_id": script["script_id"],
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        ctx["plans"][request_id] = plan
        record_step(ctx, "save_plan", args, "succeeded",
                    {"plan_id": plan["plan_id"], "approach_id": plan["approach_id"],
                     "total_cost": plan["total_cost"], "total_minutes": plan["total_minutes"]})
        return json.dumps({"ok": True, "duplicate": False, **plan,
                           "note": "方案已保存（模拟环境，不产生外部动作）"}, ensure_ascii=False)
    return save_plan


# ---------------------------------------------------------------- 协议
SCHEMAS = {
    "search_materials": {"required": ["query"], "optional": []},
    "estimate_cost": {"required": ["approach_id"], "optional": ["material_ids"]},
    "generate_script": {"required": ["approach_id"], "optional": ["tier"]},
    "save_plan": {"required": ["approach_id"], "optional": []},
}


def schema_error(action: str, args: dict) -> str | None:
    schema = SCHEMAS.get(action)
    if schema is None:
        return f"未知动作 {action}"
    if not isinstance(args, dict):
        return f"{action} 的参数必须是 JSON 对象"
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        return f"{action} 缺少参数：{', '.join(missing)}"
    extra = [k for k in args if k not in schema["required"] + schema["optional"]]
    if extra:
        return f"{action} 不接受参数：{', '.join(extra)}"
    return None


def build_registry(ctx: dict, task: dict, request_id: str) -> dict:
    return {
        "search_materials": build_search_materials(ctx),
        "estimate_cost": build_estimate_cost(ctx, task),
        "generate_script": build_generate_script(ctx),
        "save_plan": build_save_plan(ctx, task, request_id),
    }
