"""模型网关：真实 LLM（OpenAI 兼容）与离线 Mock 决策器（内容任务规划域）。

MockLLM 是确定性决策器：每一步只根据任务字段、已有步骤状态与本轮 Trace 决策，
体现“超预算 → 改选替代方案”“失败 → 重试/停止”“重复 → 不重复保存”等重规划行为。
边界：只验证流程与防御逻辑，不代表真实模型的语义能力。
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date


def load_dotenv(path: str = ".env") -> None:
    import os
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


@dataclass
class GatewayConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.0
    timeout: float = 60.0

    @staticmethod
    def from_env() -> "GatewayConfig":
        import os
        return GatewayConfig(
            base_url=os.environ.get("MODEL_BASE_URL", "https://api.deepseek.com").strip(),
            model=os.environ.get("MODEL_NAME", "deepseek-chat").strip(),
            api_key_env=os.environ.get("MODEL_API_KEY_ENV", "DEEPSEEK_API_KEY").strip(),
            temperature=float(os.environ.get("MODEL_TEMPERATURE", "0") or 0),
        )


SYSTEM_PROMPT = """你是内容团队的「任务规划助手」，在给定预算与截止时间内完成一份内容活动方案。

# 可用工具（每次只调用一个动作）
1. search_materials{"query":"秋季新品"}                     检索合成素材
2. estimate_cost{"approach_id":"AP-A"}                      按费用与耗时表估算方案（含是否在预算/截止内）
3. generate_script{"approach_id":"AP-B","tier":"standard"}  为“估算已通过”的方案生成脚本
4. save_plan{"approach_id":"AP-B"}                          保存通过校验的方案（模拟，不产生外部动作）

# 硬性约束
- 预算与截止时间是硬性要求：不得保存超预算/超时方案，也不得擅自修改预算或截止时间。
- 若任务缺少预算或截止时间（任一为空）：最多调用一次 search_materials 了解素材，然后**立即 finish** 并报告缺口，
  不得调用 estimate_cost/generate_script/save_plan（缺约束无法评估，继续调用只会浪费步骤）。
- 候选方案列表**按质量从高到低排序**（见下方 candidates）：先检索素材，再从第一个候选开始评估，
  超预算或超时则降级评估下一个候选，选中第一个可行方案后继续生成脚本与保存；
  这样在预算内尽量保住质量，也避免只挑最便宜方案。
- 必须按顺序：估算是脚本与保存的前置依赖；脚本成功是保存的前置依赖。
- 估算未通过（feasible=false 或 over_budget_by>0）时，改选其他方案重新估算；
  全部方案都不可行时，停止并报告缺口（最低可行成本与超出金额）。
- 工具返回 error 时：先原地重试一次；仍失败则保留已完成步骤，报告未完成原因并结束。
- 不得虚报：保存失败/未保存时，不得声称已保存；未完成时，不得声称完成。
- 若提供 prior_steps（上次运行的步骤状态）：已成功的步骤不得重复执行，从未成功的步骤继续。

# 输出格式：只输出一个 JSON 对象，不要使用 markdown 代码块
{"thought":"一句话判断","action":"search_materials|estimate_cost|generate_script|save_plan|finish","arguments":{...}}
结束时：{"thought":"...","action":"finish","arguments":{"summary":"<按工具结果如实总结>"}}
"""


def render_user_prompt(request: dict, trace: list[dict]) -> str:
    task = request["task"]
    lines = [
        "## 任务",
        f"主题：{task.get('theme')}｜预算：{task.get('budget')} 模拟单位｜截止：{task.get('deadline_hours')} 小时",
        f"要求：{request.get('request', '')}",
        "",
        "## 候选方案（方案目录）",
    ]
    for c in request.get("candidates", []):
        mats = "、".join(c.get("use_materials") or []) or "实拍"
        lines.append(f"- {c['id']}《{c['name']}》：{c['desc']}；素材：{mats}")
    lines += ["", "## 费用与耗时表摘要"]
    table = request.get("cost_table", {})
    for name, spec in table.items():
        tiers = "、".join(f"{k}({v['cost']}单位/{v['minutes']}分钟)" for k, v in spec.get("tiers", {}).items())
        lines.append(f"- {name}：{tiers}")
    lines += ["", "## 已有步骤（prior_steps）"]
    prior = request.get("prior_steps") or []
    if prior:
        for st in prior:
            lines.append(f"[{st['seq']}] {st['tool']} 参数={json.dumps(st['args'], ensure_ascii=False)} → {st['status']}")
    else:
        lines.append("（无）")
    lines += ["", "## 本轮执行轨迹"]
    for st in trace[-30:]:
        lines.append(f"[{st['step']}] {st['action']} 参数={json.dumps(st['arguments'], ensure_ascii=False)} → {st['observation_summary']}")
    if not trace:
        lines.append("（尚无动作，请给出第一步）")
    lines += ["", "请输出下一个动作（JSON）。"]
    return "\n".join(lines)


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg: GatewayConfig):
        self.cfg = cfg

    def _key(self) -> str:
        import os
        key = os.environ.get(self.cfg.api_key_env, "").strip()
        if not key:
            raise LLMError(f"缺少环境变量 {self.cfg.api_key_env}（或在 .env 中配置）")
        return key

    def complete(self, system: str, user: str) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {"model": self.cfg.model, "temperature": self.cfg.temperature,
                   "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key()}"})
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                return body["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                snippet = e.read().decode("utf-8", "ignore")[:300]
                last = LLMError(f"HTTP {e.code}: {snippet}")
                if e.code in (400, 401, 403, 404):
                    break
            except (urllib.error.URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as e:
                last = LLMError(str(e))
            time.sleep(1.5 * (attempt + 1))
        raise last or LLMError("模型调用失败")


def extract_json(text: str) -> dict:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.S)
    if fence:
        stripped = fence.group(1).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("模型输出中没有 JSON 对象")
    return json.loads(stripped[start:end + 1])


def build_gateway(mode: str):
    if mode == "mock":
        return MockLLM()
    return LLMClient(GatewayConfig.from_env())


# ---------------------------------------------------------------- 离线 Mock 决策器
class MockLLM:
    """确定性决策器：按任务字段与工具结果自适应，体现重规划。

    规则（与真实提示词一致）：
    - 缺预算或截止 → 只检索不做规划，结束并报告缺口；
    - 依次估算候选方案，查到可行方案为止；超预算的观察会触发“改选下一个方案”；
    - 脚本/保存失败 → 原地重试一次，仍失败则停止并保留已完成步骤；
    - prior_steps 中已成功的步骤不重复执行（续跑）；
    - 方案已保存（repeat）→ 不重复保存。
    """

    TOOLS = ("search_materials", "estimate_cost", "generate_script", "save_plan")

    def decide(self, request: dict, trace: list[dict]) -> dict:
        task = request["task"]
        budget = task.get("budget")
        deadline = task.get("deadline_hours")
        candidates = [c["id"] for c in request.get("candidates", [])]
        prior = request.get("prior_steps") or []

        # 合并本轮与历史步骤（续跑时历史动作也参与判断）
        def steps_of(tool):
            got = [t for t in trace if t["action"] == tool]
            return got

        def succeeded_in(history_tool, approach=None):
            obs = []
            for st in prior:
                if st["tool"] == history_tool and st["status"] == "succeeded":
                    if approach is None or (st["result"] or {}).get("approach_id") == approach:
                        obs.append(st["result"])
            for t in trace:
                if t["action"] == history_tool and (t.get("observation") or {}).get("ok"):
                    if approach is None or t["observation"].get("approach_id") == approach:
                        obs.append(t["observation"])
            return obs

        def attempts(history_tool, approach=None):
            """本次运行内的尝试次数（重试预算按本轮计算；历史失败只表示从哪里继续）。"""
            n = 0
            for t in trace:
                if t["action"] == history_tool and (
                        approach is None or (t.get("arguments") or {}).get("approach_id") == approach):
                    n += 1
            return n

        # 1) 任务输入缺失：检索素材后结束，报告缺口
        if budget in (None, "") or deadline in (None, ""):
            if not steps_of("search_materials") and not succeeded_in("search_materials"):
                return self._act("任务缺少预算或截止时间，先检索相关素材供参考。",
                                 "search_materials", {"query": task.get("theme", "")})
            return self._finish_task_missing(task)

        # 2) 已有方案（重复执行）：不再重复保存
        saved = request.get("existing_plan") or {}
        if saved:
            return self._finish_saved(dict(saved, plan_id=saved.get("plan_id", "已有方案")))

        # 3) 依次尝试候选方案
        feasible = None
        for ap in candidates:
            est_ok = [e for e in succeeded_in("estimate_cost", ap) if e.get("feasible") is True]
            if est_ok:
                feasible = ap
                break
            tried = attempts("estimate_cost", ap)
            if tried == 0:
                return self._act(f"先评估候选方案 {ap} 的成本与耗时是否在预算内。",
                                 "estimate_cost", {"approach_id": ap})
            # 该方案估算过且不可行 → 尝试下一个候选
        if feasible is None:
            return self._finish_no_feasible(candidates, prior, trace)

        # 4) 脚本（依赖估算通过）
        script_ok = succeeded_in("generate_script", feasible)
        if not script_ok:
            tier = self._script_tier(request, feasible)
            script_tries = attempts("generate_script", feasible)
            if script_tries == 0:
                return self._act(f"方案 {feasible} 估算可行，生成脚本。", "generate_script",
                                 {"approach_id": feasible, "tier": tier})
            if script_tries < 2:
                return self._act("脚本生成失败，原地重试一次；已完成步骤保持不变。",
                                 "generate_script", {"approach_id": feasible, "tier": tier})
            return self._finish_failed(prior, trace, "generate_script")

        # 5) 保存（依赖脚本成功）
        save_ok = succeeded_in("save_plan", feasible)
        if not save_ok:
            save_tries = attempts("save_plan")
            if save_tries == 0:
                return self._act(f"脚本已生成，方案 {feasible} 满足预算与截止时间，保存方案。",
                                 "save_plan", {"approach_id": feasible})
            if save_tries < 2:
                return self._act("保存失败，原地重试一次；此前成功的步骤全部保留。",
                                 "save_plan", {"approach_id": feasible})
            return self._finish_failed(prior, trace, "save_plan")

        last_save = next((t["observation"] for t in reversed(trace)
                          if t["action"] == "save_plan" and (t.get("observation") or {}).get("ok")), {})
        return self._finish_saved({"plan_id": last_save.get("plan_id", "未知"),
                                   "approach_id": feasible,
                                   "total_cost": last_save.get("total_cost"),
                                   "budget": budget})

    # ---- 辅助
    def _script_tier(self, request, approach_id):
        for c in request.get("candidates", []):
            if c["id"] == approach_id:
                for item in c.get("items", []):
                    if item.get("name") == "generate_script":
                        return item.get("tier", "standard")
        return "standard"

    def _act(self, thought, action, args):
        return {"thought": thought, "action": action, "arguments": args}

    def _finish_task_missing(self, task):
        missing = []
        if task.get("budget") in (None, ""):
            missing.append("预算")
        if task.get("deadline_hours") in (None, ""):
            missing.append("截止时间")
        return {"thought": "缺少硬性输入，无法完成规划。", "action": "finish",
                "arguments": {"summary": f"缺少 {'、'.join(missing)}，无法在预算与截止时间内完成规划；请补充后继续。"}}

    def _finish_no_feasible(self, candidates, prior, trace):
        infos = []
        for st in prior + [{"tool": t["action"], "result": t.get("observation") or {}} for t in trace]:
            if st["tool"] == "estimate_cost" and (st.get("result") or {}).get("total_cost") is not None:
                infos.append(st["result"])
        best = min(infos, key=lambda r: r["total_cost"]) if infos else None
        if best:
            gap = best.get("over_budget_by")
            over_time = best.get("over_deadline_by_min")
            reasons = []
            if gap:
                reasons.append(f"超预算 {gap} 单位")
            if over_time:
                reasons.append(f"超时 {over_time} 分钟")
            msg = (f"所有候选方案都不可行：最低成本方案 {best['approach_id']} 需 {best['total_cost']} 单位"
                   f"（{'、'.join(reasons) or '条件不满足'}）；未保存任何方案，需调整预算或截止时间。")
        else:
            msg = "所有候选方案都不可行，未保存任何方案。"
        return {"thought": "无可行方案，停止并报告缺口。", "action": "finish", "arguments": {"summary": msg}}

    def _finish_failed(self, prior, trace, failed_tool):
        done = [st["tool"] for st in prior if st["status"] == "succeeded"] + \
               [t["action"] for t in trace if (t.get("observation") or {}).get("ok")]
        return {"thought": f"{failed_tool} 重试后仍失败，保留已完成步骤并停止。", "action": "finish",
                "arguments": {"summary": f"{failed_tool} 连续失败，任务未完成；已完成步骤：{done or '无'}，修复后可续跑。"}}

    def _finish_saved(self, saved):
        cost = saved.get("total_cost")
        cost_txt = f"成本 {cost} 单位" if cost is not None else ""
        budget = saved.get("budget")
        left = (f"，预算剩余 {round(budget - cost, 2)} 单位" if (budget is not None and cost is not None) else "")
        return {"thought": "方案已保存，任务完成。", "action": "finish",
                "arguments": {"summary": f"方案已保存（计划号 {saved.get('plan_id')}，方案 {saved.get('approach_id')}"
                                         f"{('，' + cost_txt) if cost_txt else ''}{left}），未超出预算与截止时间。"}}
