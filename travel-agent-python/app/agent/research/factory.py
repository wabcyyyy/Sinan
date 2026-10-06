"""研究 Agent 工厂：一个子图，三个域实例（阶段二：带 LLM 推理循环）。

职责：
- build_research_graph(domain)：为单个研究域构建 LangGraph 子图
  规划(LLM) → 检索(工具) → 评估(LLM) → 不足则补查一轮 → 评估收尾；
- run_research(task)：执行一次研究任务，返回 EvidencePack。

实现要点：
- 三域共用同一张图结构，差异全部收在 DomainConfig（检索工具/参数/缺口判定）；
- 检索经工具注册表统一派发（G-1.4），registry handler 调用期解析 tools 模块属性，
  单测 patch.object(tools, ...) 持续生效；
- LLM 规划/评估在 research.reasoning 模块级函数内实现，测试直接 patch 该模块；
- 补查轮次上限 RESEARCH_LLM_ROUNDS=2：规划+检索为第 1 轮，评估不足时再补 1 轮；
- 研究取数另受 `run_limits` 的 research 道（`settings.research_call_limit`）约束：
  主检索照记但不停，补池/兜底在额度用完后停下并把原因写进证据包 gaps——研究
  只受全 run 检索上限约束时，三域补池能把生成与落地的额度整个饿死（实测）；
- 补充检索词走联网搜索（POI 库退役）并按名称去重并入证据，不改变权威主路径。
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from app.agent.research import reasoning
from app.agent.research.agent_state import UnifiedAgentState
from app.agent.research.domains import DOMAINS
from app.agent.research.evidence import (
    RESEARCH_DOMAINS,
    EvidencePack,
    ResearchDomain,
    ResearchTask,
)
from app.agent.runtime.run_limits import RunLimitExceeded, current_limits
from app.agent.runtime.trace import record_event, trace_span
from app.agent.tools.registry import registry

logger = logging.getLogger(__name__)

# 研究 LLM 推理的检索轮次上限：第 1 轮规划+检索，评估不足时再补 1 轮。
RESEARCH_LLM_ROUNDS = 2

# 无任何来源标注（如离线 fixture / 纯本地检索透传）的整包视为证据可信；
# 只有显式缺失标注或为空时才拉低置信度。
_DEFAULT_CONFIDENCE = 1.0


def _require_task(state: UnifiedAgentState) -> ResearchTask:
    """研究子图必须携带 task（run_research 入口注入）；缺失是编程错误，响亮失败。

    沿用双轨错误边界：抛 ValueError 由 Supervisor 按域降级接住（run_research 的
    未知域报错同款语义），不静默空跑。
    """
    task = state.task
    if task is None:
        raise ValueError("research 子图缺少 task（应由 run_research 入口注入）")
    return task


def _spend_research(limits, domain: ResearchDomain) -> bool:
    """扣一次研究取数额度。

    返回 False 表示额度已用完——**必须响亮**：静默停止补池会让"研究到额度为止"
    和"这个城市就是没有更多点"在证据包里长成同一个样子（与 PLAN-A1 P4 同源的那类
    谎）。用完不是失败：已有的证据照样交付，剩下的额度留给生成与落地。
    """
    if not limits:
        return True
    try:
        limits.check("research")
        limits.record_research(1)
        return True
    except RunLimitExceeded as exc:
        record_event(
            "decision",
            f"research.{domain}.quota_exhausted",
            status="error",
            metadata={"error": str(exc)[:120], "research_calls": limits.research_calls},
        )
        return False


def _plan_node(state: UnifiedAgentState) -> dict:
    """规划节点：LLM 决定检索策略；失败/未配置返回空计划（退化确定性检索）。"""
    # 运行时经 reasoning 模块属性解析，保证单测 patch 注入生效（与 tools 同一约定）。
    return {"research_plan": reasoning.plan_research(_require_task(state))}


def _merge_supplement(items: list[dict], remote: list[dict]) -> list[dict]:
    """按名称去重并入补充检索结果（高德实时 POI），不覆盖已收集证据。"""
    known = {str(p.get("name")) for p in items if p.get("name")}
    merged = list(items)
    for poi in remote or []:
        name = str(poi.get("name") or "").strip()
        if name and name not in known:
            known.add(name)
            merged.append(poi)
    return merged


def _run_search(state: UnifiedAgentState) -> dict:
    """检索节点：按领域配置调用检索工具，并执行规划/评估给出的补充关键词。"""
    task = _require_task(state)
    config = DOMAINS[task.domain]
    limits = current_limits()
    plan = state.research_plan or {}
    params = config.params(task)
    if task.domain == "attraction" and plan.get("preferences"):
        prefs = list(dict.fromkeys((task.preferences or []) + [str(p) for p in plan["preferences"]]))
        params = {**params, "preferences": prefs}
    if plan.get("limit"):
        params = {**params, "limit": int(plan["limit"])}
    # 统一经注册表派发（G-1.4）：预算/审计/校验全覆盖；handler 调用期读
    # tools 模块属性，patch.object(tools, "search_*") 注入持续生效。
    # 主检索一定执行（它是这个域的最小可用证据），但照样记账——研究额度
    # 记的就是"研究一共取了多少次数"，超了之后停的是补池，不是主检索。
    _spend_research(limits, task.domain)
    items = registry.invoke(config.tool_name, params) or []
    if limits:
        limits.record_retrieval(1)
    extras = list(dict.fromkeys((plan.get("extra_keywords") or []) + (state.extra_keywords or [])))
    # M3-②（AD5）最小增量：任务卡携带的意图关键词并入补池链（新旧行为兼容，仅追加）
    extras = list(dict.fromkeys(extras + list(task.intent_keywords or [])))
    from app.agent.data.web_search import search_places_via_web, web_search_enabled

    quota_note = state.quota_note or ""
    # 联网入口关着时补池必然返回空（search_places_via_web 自己就早退），所以这里
    # 直接不进循环：既省一次无用的记账，也让"离线评测不占研究额度"成为结构事实。
    if extras and web_search_enabled():
        for keyword in extras:
            if not _spend_research(limits, task.domain):
                quota_note = f"研究额度已用完，补池停在关键词「{keyword}」之前"
                break
            if limits:
                limits.check("retrieval")
            # 关键词补池走联网搜索（POI 库退役）：意图词补真实地点名，不保证坐标
            supplement = search_places_via_web(task.city, task.domain, limit=4, intent_keywords=[keyword])
            if limits:
                limits.record_retrieval(1)
            items = _merge_supplement(items, supplement)
    # 外部池仍偏少时：联网搜索补真实地点名（池空/海外城市的证据缺口）
    if len(items) < 3 and web_search_enabled() and _spend_research(limits, task.domain):
        web_rows = search_places_via_web(
            task.city,
            task.domain,
            limit=max(4, 6 - len(items)),
            intent_keywords=task.intent_keywords,
        )
        items = _merge_supplement(items, web_rows)
    return {"items": items, "round": state.round + 1, "quota_note": quota_note}


def _evaluate_node(state: UnifiedAgentState) -> dict:
    """评估节点：LLM 判定证据充分性；不足时给出补充检索词。"""
    task = _require_task(state)
    items = state.items or []
    round_no = state.round
    verdict = reasoning.evaluate_research(task, items, round_no)
    return {
        "sufficient": bool(verdict.get("sufficient", True)),
        "extra_keywords": [str(x) for x in (verdict.get("extra_keywords") or []) if x],
    }


def _refine_node(state: UnifiedAgentState) -> dict:
    """补查节点：把评估给出的补充词并入检索计划，进入下一轮检索。"""
    plan = dict(state.research_plan or {})
    plan["extra_keywords"] = list(dict.fromkeys((plan.get("extra_keywords") or []) + (state.extra_keywords or [])))
    return {"research_plan": plan}


def _route_after_search(state: UnifiedAgentState) -> str:
    """第 1 轮检索后评估充分性；补查轮（已达轮次上限）直接收尾，不再重复评估。"""
    if state.round < RESEARCH_LLM_ROUNDS:
        return "evaluate"
    return "finalize"


def _route_after_evaluate(state: UnifiedAgentState) -> str:
    if state.sufficient:
        return "finalize"
    record_event("decision", f"research.{_require_task(state).domain}.refine", metadata={"round": state.round})
    return "refine"


def _finalize_pack(state: UnifiedAgentState) -> dict:
    """评估收尾：计算置信度/缺口/降级标记，产出证据包。"""
    task = _require_task(state)
    items = state.items or []
    config = DOMAINS[task.domain]
    flagged = [p for p in items if p.get("_authoritative") is not None]
    if flagged:
        evidenced = sum(1 for p in flagged if p.get("_authoritative"))
        confidence = round(evidenced / len(flagged), 2)
    else:
        confidence = _DEFAULT_CONFIDENCE if items else 0.0
    gap = config.gap_hint(items)
    quota_note = state.quota_note or ""
    pack = EvidencePack(
        domain=task.domain,
        items=items,
        confidence=confidence,
        rounds=state.round,
        gaps=[note for note in (gap, quota_note) if note],
        degraded=not items,
    )
    record_event("decision", f"research.{task.domain}", metadata=pack.to_dict())
    return {"pack": pack}


def build_research_graph(domain: ResearchDomain):
    graph = StateGraph(UnifiedAgentState)
    graph.add_node("plan_query", _plan_node)
    graph.add_node("search", _run_search)
    graph.add_node("evaluate", _evaluate_node)
    graph.add_node("refine", _refine_node)
    graph.add_node("finalize", _finalize_pack)
    graph.set_entry_point("plan_query")
    graph.add_edge("plan_query", "search")
    graph.add_conditional_edges(
        "search",
        _route_after_search,
        {"evaluate": "evaluate", "finalize": "finalize"},
    )
    graph.add_conditional_edges(
        "evaluate",
        _route_after_evaluate,
        {"finalize": "finalize", "refine": "refine"},
    )
    graph.add_edge("refine", "search")
    graph.add_edge("finalize", END)
    return graph.compile()


_research_graphs: dict[ResearchDomain, object] = {domain: build_research_graph(domain) for domain in RESEARCH_DOMAINS}


def run_research(task: ResearchTask) -> EvidencePack:
    """执行一次研究任务；异常向上抛，由 Supervisor 做单域降级。"""
    if task.domain not in _research_graphs:
        raise ValueError(f"未知研究域：{task.domain}")
    with trace_span("agent", f"research.{task.domain}", metadata={"city": task.city, "limit": task.limit}):
        result = _research_graphs[task.domain].invoke(
            {
                "task": task,
                "research_plan": {},
                "items": [],
                "round": 0,
                "sufficient": True,
                "extra_keywords": [],
                "quota_note": "",
                "pack": None,
            }
        )
    pack = result.get("pack")
    if pack is None:
        raise RuntimeError(f"研究域 {task.domain} 未产出证据包")
    return pack
