"""最终输出的校验与质量结论。

终检必须针对已经补齐坐标、价格、营业时间的输出，防止格式化阶段的字段变化绕过
反思层；质量结论则把「降级原因 / 阻塞问题 / 事实覆盖率」收敛成一个可交付判定。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, NamedTuple

from app.agent.data.route_service import is_estimated
from app.agent.generation.content.reflect import validate_plans
from app.agent.generation.content.route_matrix import route_matrix_for_plans
from app.agent.generation.output.critic import critique_plans
from app.agent.research.agent_state import UnifiedAgentState
from app.agent.runtime.trace import record_event
from app.common.addons import addons
from app.common.config import settings
from app.schemas.trip import (
    DailyPlan,
    GenerateRequest,
    QualityIssue,
    QualityReport,
)

_EMPTY_CRITIC = {"score": 0.0, "issues": [], "strengths": [], "dimensions": {}}
_KNOWN_DESTINATION_STATUSES = {"knowledge_backed", "researched", "draft_only"}


class FinalCheck(NamedTuple):
    raw_plans: list[dict]
    issues: list[str]
    validation_log: list[str]
    critic_report: dict


class QualityOutcome(NamedTuple):
    """质量判定结果；两个状态字段用契约字面量而非 str——它们直接喂给
    GenerateResponse，收窄可让错字在类型层暴露而不是流到 wire。"""

    validation_log: list[str]
    quality_report: QualityReport
    status: Literal["success", "degraded", "failed"]
    status_reason: str | None
    destination_status: Literal["knowledge_backed", "researched", "draft_only"]


def run_final_validation(
    req: GenerateRequest, daily_plans: list[DailyPlan], schedule_report: dict, consumption: dict | None
) -> FinalCheck:
    """对成品行程再跑一次约束校验与软评审；就地补写 schedule_report 的路线来源。"""
    final_raw_plans = [
        {
            "day_no": plan.day_no,
            "theme": plan.theme,
            "mini_route": plan.mini_route,
            "backup_plan": plan.backup_plan,
            "photo_spots": plan.photo_spots,
            "practical_notes": plan.practical_notes,
            # M3-① 叙事层随行：校验/评审链路按 key 读取、不消费这两个字段，
            # 保留以使最终校验的输入与真实输出形状一致。
            "day_options": plan.day_options,
            "trip_theme": plan.trip_theme,
            "items": [item.model_dump() for item in plan.items],
        }
        for plan in daily_plans
    ]
    final_route_matrix = None
    if settings.route_service_enabled and addons.is_enabled("route_service"):
        final_route_matrix = route_matrix_for_plans(final_raw_plans)
        route_sources = [str(route.get("source") or "unknown") for route in final_route_matrix.values()]
        if route_sources:
            schedule_report.setdefault("route_sources", list(dict.fromkeys(route_sources)))
            schedule_report["degraded"] = bool(schedule_report.get("degraded")) or any(
                is_estimated(source) for source in route_sources
            )
    final_issues, final_log = validate_plans(
        final_raw_plans,
        route_matrix=final_route_matrix,
        budget=req.budget if settings.budget_hard_constraint else None,
        persons=req.persons,
        consumption=consumption,
        budget_overage_ratio=settings.budget_overage_ratio,
        requirements=getattr(req, "requirements_struct", None),
    )
    # critique_plans 是纯内存的轻量软评审，直接同步调用即可。
    # （此前每请求新建 ThreadPoolExecutor 且从不 shutdown，会泄漏常驻线程。）
    try:
        critic_report = critique_plans(final_raw_plans, req.preferences).as_dict()
    except Exception:
        critic_report = dict(_EMPTY_CRITIC)
    record_event(
        "decision",
        "critic_result",
        metadata={
            "score": critic_report["score"],
            "issue_count": len(critic_report.get("issues", [])),
        },
    )
    return FinalCheck(final_raw_plans, list(final_issues), list(final_log), critic_report)


def judge_output(
    state: UnifiedAgentState,
    daily_plans: list[DailyPlan],
    schedule_report: dict,
    check: FinalCheck,
    quality_fallback_reason: str | None,
) -> QualityOutcome:
    """汇总降级原因、质量报告与最终状态。"""
    validation_log = list(state.validation_log or [])
    validation_log.extend(check.validation_log)
    degraded_reasons: list[str] = []
    if state.degraded_reason:
        degraded_reasons.append(state.degraded_reason or "")
    if schedule_report.get("degraded") and settings.route_service_enabled:
        degraded_reasons.append("真实路线服务部分不可用，已使用坐标估算")
    if quality_fallback_reason:
        degraded_reasons.append(quality_fallback_reason)
    if check.issues:
        validation_log.extend(check.issues)
        degraded_reasons.append("达到重试上限后仍存在行程约束问题")
    degraded_reasons = list(dict.fromkeys(reason for reason in degraded_reasons if reason))
    status = "degraded" if degraded_reasons else "success"

    quality_warnings: list[QualityIssue] = []
    item_count = 0
    estimated_count = 0
    evidenced_count = 0
    for day_index, day in enumerate(daily_plans):
        for item_index, item in enumerate(day.items):
            item_count += 1
            if item.verification_status != "verified":
                quality_warnings.append(
                    QualityIssue(
                        code="FACT_REQUIRES_REVIEW",
                        # path 必须是"日索引 + 当日内索引"，此前用跨天累计序号
                        # 会让前端定位指错条目。
                        path=f"trip.daily_plans[{day_index}].items[{item_index}]",
                        message=f"{item.poi_name} 的部分事实需要出发前复核",
                    )
                )
            if item.value_kind == "estimated":
                estimated_count += 1
            # fact_evidence 延迟构建，此处基于 source 字段判断是否有溯源
            if item.source:
                evidenced_count += 1
    blocking = [QualityIssue(code="ROUTE_OR_SCHEDULE_CONFLICT", message=issue) for issue in check.issues]
    if item_count == 0:
        blocking.append(QualityIssue(code="NO_ITINERARY_ITEMS", message="没有可交付的行程地点"))
    elif not any(item.item_type == "attraction" for day in daily_plans for item in day.items):
        # 占位酒店式行程（有 items 但 0 景点）不可作为可交付行程放行。
        blocking.append(QualityIssue(code="NO_ATTRACTION_ITEMS", message="行程中没有安排任何景点"))
    empty_days = [day.day_no for day in daily_plans if not day.items]
    if empty_days:
        blocking.append(
            QualityIssue(
                code="MISSING_DAY_ITEMS",
                message="以下日期没有可交付的行程地点：" + ", ".join(str(day_no) for day_no in empty_days),
            )
        )
    quality_status = "BLOCKED" if blocking else ("READY_WITH_WARNINGS" if quality_warnings else "READY")
    if schedule_report.get("destination_status") == "draft_only" and not blocking:
        quality_status = "READY_WITH_WARNINGS"
    quality_report = QualityReport(
        quality_status=quality_status,
        validated_at=datetime.now(UTC).isoformat(),
        blocking_issues=blocking,
        warnings=quality_warnings,
        metrics={
            "item_count": float(item_count),
            "fact_evidence_coverage": round(evidenced_count / item_count, 4) if item_count else 0.0,
            "estimated_fact_ratio": round(estimated_count / item_count, 4) if item_count else 0.0,
        },
    )
    # 没有任何可交付地点时，质量状态必须为 BLOCKED；这类草案不能被
    # "degraded" 状态误解为可直接执行的行程。
    if item_count == 0:
        status = "failed"
    destination_status = schedule_report.get("destination_status")
    if destination_status not in _KNOWN_DESTINATION_STATUSES:
        destination_status = "knowledge_backed" if state.candidates else "draft_only"
    record_event(
        "decision",
        "run_status",
        metadata={
            "status": status,
            "final_issue_count": len(check.issues),
        },
    )
    return QualityOutcome(
        validation_log=validation_log,
        quality_report=quality_report,
        status=status,
        status_reason="；".join(degraded_reasons) or None,
        destination_status=destination_status,
    )
