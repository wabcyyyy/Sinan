"""统一图（整段）的节点实现（G-2.4 自 workflow 拆出）。

节点清单与职责：parse_requirements（解析）→ research_pois（研究）→
generate_itinerary（生成/重试编排）→ reflect（校验）→ needs_fix（路由）→
research_refill（补查）→ format（装配见 formatting.assembly）。

编排图在 trip_graph.unified_agent_graph（整段/逐日共用）；本模块只提供节点
函数，`workflow.py` 作为薄门面导出它们（trip_graph 经 `workflow.X` 调用，
测试据此 monkeypatch——**勿改访问路径**）。

依赖：open_plans、suggestions、formatting.assembly、generators 下层面。
"""

import logging
from typing import cast

from app.agent.generation.content.reflect import build_feedback, validate_plans
from app.agent.generation.content.route_matrix import route_matrix_for_plans
from app.agent.generation.orchestration.open_plans import draft_state, generate_open_plans
from app.agent.generation.output.assembly import (
    format_output as format_output,  # 门面转发（trip_graph 经 workflow.format_output 调用）
)
from app.agent.generation.output.assembly import (
    generation_attempt_limit,
)
from app.agent.generation.rules.generation_core import MAX_GENERATION_ATTEMPTS, MAX_REFILLS
from app.agent.research import merge_candidates, run_refill, run_research_context
from app.agent.research.agent_state import UnifiedAgentState
from app.agent.research.evidence import ResearchDomain
from app.agent.runtime.observability import metrics
from app.agent.runtime.trace import record_event, traced
from app.common.addons import addons
from app.common.config import settings
from app.common.model_registry import configured
from app.schemas.trip import GenerateRequest

logger = logging.getLogger(__name__)


@traced("node", "parse")
def parse_requirements(state: UnifiedAgentState) -> dict:
    req: GenerateRequest = cast("GenerateRequest", state.request)
    requirements = {
        "city": req.city,
        "days": req.days,
        "persons": req.persons,
        "budget": req.budget,
        "preferences": req.preferences,
        "start_date": req.start_date,
    }
    return {"request": req, "requirements": requirements}


@traced("node", "research")
def research_pois(state: UnifiedAgentState) -> dict:
    """研究阶段：Supervisor 并行派发酒店/景点/美食研究 Agent，整合证据上下文。

    多 Agent 化后，检索不再是单一工具调用，而是三个领域研究 Agent 各自产出
    证据包（EvidencePack），由 Supervisor 并行执行并整合回旧上下文契约
    {candidates, foods, hotels, consumption}；research_report 随状态流转，
    由 format_output 写入 schedule_report 供观测与演示。
    """
    req: GenerateRequest = cast("GenerateRequest", state.request)
    context = run_research_context(req)
    return {
        "candidates": context["candidates"],
        "foods": context["foods"],
        "hotels": context["hotels"],
        "consumption": context["consumption"],
        "weather": context.get("weather"),
        "research_report": context["research_report"],
    }


@traced("node", "generate")
def generate_itinerary(state: UnifiedAgentState) -> dict:
    req: GenerateRequest = cast("GenerateRequest", state.request)
    attempts = state.attempts
    feedback = state.feedback
    schedule_report: dict = {}

    # LLM-only 生成：开放模式（LLM 知识 + 权威参考资料注入 + 存在性解析落坐标）
    # 是唯一的行程内容来源；知识库只作为证据引导与事实校准，绝不直接
    # 拼装行程。开放失败时记 error 走 fix 循环重试一次（与 days 无关），
    # 重试耗尽或未配置 LLM 则返回结构化待研究草案——如实降级，不冒充生成结果。
    if configured("main") and attempts < MAX_GENERATION_ATTEMPTS:
        open_state, research_errors = generate_open_plans(
            req,
            feedback,
            state.hotels,
            candidates=state.candidates,
            foods=state.foods,
            weather=state.weather,
        )
        if open_state is not None:
            return open_state
        # 降级原因取自研究阶段真实抛出的**第一宗罪**——整段失败是根因（Deadline /
        # 配额 / 解析失败各是各的病），而不是写死的"重试耗尽"；PR-4 截断逐日兜底后
        # per-day 错误是次生、排在队尾，根因在首（旧链每次 run 至多一条错误，
        # [-1] 与 [0] 等价）。错误串自带"开放研究失败："/"第N天开放研究失败："
        # 前缀，这里不再叠一层。
        detail = research_errors[0] if research_errors else "开放研究失败：所有天均未产出草案"
        if attempts + 1 >= MAX_GENERATION_ATTEMPTS:
            # 重试耗尽：草案必须可达（多日行程此前因 attempts 预算与 days
            # 挂钩而直接落到空 plans，草案分支成为死代码）。
            return draft_state(req, detail, schedule_report)
        record_event("route", "retry", metadata={"reason": "open_research_failed", "error": detail[:160]})
        return {"error": detail, "attempts": attempts + 1, "schedule_report": schedule_report}

    reason = (
        "未配置 LLM，无法生成行程内容"
        if not configured("main")
        else str(state.error or "开放研究重试耗尽，已返回待研究草案")
    )
    return draft_state(req, reason, schedule_report)


@traced("node", "reflect")
def reflect(state: UnifiedAgentState) -> dict:
    plans = state.daily_plans or []
    issues: list[str] = []
    log: list[str] = []
    if state.error:
        log.append("生成失败，跳过校验")
        return {"validation_issues": issues, "validation_log": log, "fix_count": state.fix_count}
    if plans:
        route_matrix = None
        if settings.route_service_enabled and addons.is_enabled("route_service"):
            route_matrix = route_matrix_for_plans(plans)
        req = state.request
        budget = getattr(req, "budget", None) if req is not None else None
        persons = getattr(req, "persons", 1) or 1 if req is not None else 1
        issues, log = validate_plans(
            plans,
            route_matrix=route_matrix,
            budget=budget if settings.budget_hard_constraint else None,
            persons=persons,
            consumption=state.consumption,
            budget_overage_ratio=settings.budget_overage_ratio,
        )
    record_event(
        "decision",
        "reflect_result",
        metadata={
            "issue_count": len(issues),
            "needs_fix": bool(issues),
        },
    )
    return {
        "validation_issues": issues,
        "validation_log": log,
        "feedback": build_feedback(issues),
        "fix_count": state.fix_count + (1 if issues else 0),
    }


def needs_fix(state: UnifiedAgentState) -> str:
    attempt_limit = generation_attempt_limit(state)
    # 生成失败的重试次数与 days 无关（定稿口径：失败→重试一次→草案）；
    # 校验修复次数才按 attempt_limit 收窄。
    if state.error and state.attempts < MAX_GENERATION_ATTEMPTS:
        route = "fix"
        record_event("route", route, metadata={"reason": "generation_error"})
        return route
    if state.validation_issues and state.fix_count <= attempt_limit:
        # Supervisor 缺口补查优先于整体重生成：仅当校验问题属于"证据型缺口"
        # （如未安排任何景点）且补查预算未耗尽时，重派发对应研究 Agent。
        if _refillable(state) and state.refill_count < MAX_REFILLS:
            route = "refill"
            record_event("route", route, metadata={"reason": "evidence_gap"})
            return route
        route = "fix"
        record_event("route", route, metadata={"reason": "validation_issue"})
        return route
    record_event("route", "pass", metadata={"reason": "validation_pass"})
    return "pass"


def _refill_domain(issues: list[str]) -> ResearchDomain | None:
    """校验问题 → 证据缺口域：目前只有"未安排任何景点"是明确的证据型缺口。"""
    if any("未安排任何景点" in issue for issue in issues or []):
        return "attraction"
    return None


def _refillable(state: UnifiedAgentState) -> bool:
    return _refill_domain(state.validation_issues or []) is not None


@traced("node", "refill_research")
def research_refill(state: UnifiedAgentState) -> dict:
    """Supervisor 缺口补查：重派发缺口域研究 Agent，合并补查证据后重新生成。

    只补证据不生成内容（LLM-only）：补查结果并入 candidates，随
    research_report 记录补查域统计，供观测与演示。
    """
    domain = _refill_domain(state.validation_issues or [])
    if domain is None:
        return {"refill_count": state.refill_count}
    req: GenerateRequest = cast("GenerateRequest", state.request)
    pack = run_refill(domain, req)
    candidates = merge_candidates(state.candidates or [], pack.items)
    research_report = dict(state.research_report or {})
    agents = dict(research_report.get("agents") or {})
    agents[domain] = {**pack.to_dict(), "refilled": True}
    research_report["agents"] = agents
    record_event(
        "decision", "research_refill", metadata={"domain": domain, "count": len(pack.items), "rounds": pack.rounds}
    )
    metrics.record_research_refill()
    return {
        "candidates": candidates,
        "research_report": research_report,
        "refill_count": state.refill_count + 1,
    }
