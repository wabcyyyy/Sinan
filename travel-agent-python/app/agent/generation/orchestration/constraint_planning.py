"""显式选择的约束规划业务入口：研究候选 → 整趟 CP-SAT → DailyPlan。

复用研究/路线/预算/取消通道，不调用额外 LLM；默认生成路径保持原样。
"""

import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise

from app.agent.data.route_service import RouteService, get_route_matrix
from app.agent.generation.content.constraint_candidate_policy import (
    DAY_CANDIDATE_LIMIT,
    TRIP_CANDIDATE_LIMIT,
    TRIP_PLANNING_SECONDS,
    day_pool,
    requested_names,
)
from app.agent.generation.content.constraint_facts import (
    align_requirement_names,
    planning_candidate,
    prepare_candidates,
)
from app.agent.generation.content.constraint_problem import (
    DayPlanningProblem,
    DayPlanningResult,
    ScheduledVisit,
    minute,
    money_minor,
)
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.orchestration import evidence_tools
from app.agent.generation.orchestration.active_evidence import drive_evidence
from app.agent.runtime.run_limits import current_limits
from app.agent.runtime.trace import record_event
from app.common import clock
from app.schemas.constraints import ConstraintCheck, ConstraintReport
from app.schemas.planning import ConstraintPlanningAssessment, ConstraintRepairSuggestion, PlanningRouteFact
from app.schemas.trip import MAX_TRIP_DAYS, DailyPlan, GenerateRequest, TripItem


def _route_facts(result, matrix):
    rows = []
    for first, second in pairwise(result.visits):
        route = matrix.get((first.candidate_id, second.candidate_id)) or {}
        source = str(route.get("source") or "unknown")
        basis = (
            "road_network"
            if source in ("openrouteservice", "osrm")
            else ("coordinate_estimate" if source == "coordinate-estimate" else "unknown")
        )
        rows.append(
            PlanningRouteFact(
                from_id=first.candidate_id,
                to_id=second.candidate_id,
                mode=route.get("mode") or "walking",
                duration_min=route.get("duration_min"),
                source=source,
                source_url=route.get("source_url"),
                attribution=route.get("attribution"),
                fetched_at=route.get("fetched_at"),
                data_date=route.get("data_date"),
                basis=basis,
            )
        )
    return rows


def _candidate_checks(prepared, chosen, day_no, fixed_arrangements=()):
    checks = []
    fixed_names = {f.name for f in fixed_arrangements if f.day_no == day_no}
    for candidate in prepared:
        gaps = [g for g in candidate.gaps if not (candidate.item.poi_name in fixed_names and "时长" in g)]
        if candidate.fact.candidate_id in chosen and gaps:
            checks.append(
                ConstraintCheck(
                    constraint_id=f"facts:{candidate.fact.candidate_id}",
                    kind="planning_evidence",
                    status="unknown",
                    day_no=day_no,
                    item_refs=[candidate.item.poi_name],
                    blocking=True,
                    reason="；".join(gaps),
                )
            )
    return checks


def _numerical_status(result):
    if result.status == "INFEASIBLE":
        return "INFEASIBLE"
    if result.total_tiers > 0 and result.optimized_tiers == result.total_tiers:
        return "OPTIMAL"
    return "FEASIBLE" if result.total_tiers > 0 else "UNKNOWN"


def _assessment(request, day_no, local_date, result, prepared, matrix, stay_nights):
    options = request.requirements_struct.constraint_planning
    chosen = {v.candidate_id for v in result.visits}
    checks = [
        *result.report.checks,
        *_candidate_checks(prepared, chosen, day_no, request.requirements_struct.fixed_arrangements),
    ]
    routes = _route_facts(result, matrix)
    gaps = []
    if any(route.basis != "road_network" for route in routes):
        gaps.append("部分转场缺少真实路网数据，仅按估算/未知下界试排")
    if request.start_date is None:
        gaps.append("未确认出行日期，暂按目的地当地今日试排")
    if stay_nights:
        gaps.append("住宿尚未纳入费用/预约模型")
    if not result.visits and result.status != "INFEASIBLE":
        gaps.append("未产生可用活动安排")
    if gaps:
        checks.append(
            ConstraintCheck(
                constraint_id="trip:missing-facts",
                kind="planning_evidence",
                status="unknown",
                day_no=day_no,
                reason="；".join(gaps),
                blocking=True,
            )
        )
    unknown = any(check.status == "unknown" for check in checks)
    status = "UNKNOWN" if unknown and result.status != "INFEASIBLE" else result.status
    repairs = []
    if result.status == "INFEASIBLE":
        req = request.requirements_struct
        for relation in req.place_relations:
            if relation.strength == "hard" and any(
                c.endswith(f"relation:{relation.constraint_id}") for c in result.conflict_ids
            ):
                repairs.append(
                    ConstraintRepairSuggestion(
                        constraint_id=relation.constraint_id,
                        action="soften_relation",
                        source_text=relation.source_text,
                        label=f"将第{relation.day_no}天「{' → '.join(relation.place_names)}」由硬要求改为偏好",
                    )
                )
        fixed_names = {f.name for f in req.fixed_arrangements}
        for place in req.required_places:
            if place.name not in fixed_names and any(
                c.endswith(f"required:{place.constraint_id}") for c in result.conflict_ids
            ):
                repairs.append(
                    ConstraintRepairSuggestion(
                        constraint_id=place.constraint_id,
                        action="remove_required",
                        label=f"取消「{place.name}」的必去要求，保留为候选",
                    )
                )
    return ConstraintPlanningAssessment(
        status=status,
        solver_status=result.solver_status,
        numerical_status=_numerical_status(result),
        local_date=local_date,
        destination=options,
        report=ConstraintReport(checks=checks),
        conflict_ids=result.conflict_ids,
        conflict_irreducible=result.conflict_irreducible,
        repair_suggestions=repairs,
        routes=routes,
        facts=[c.fact for c in prepared if c.fact.candidate_id in chosen] if chosen else [c.fact for c in prepared],
        known_cost=result.known_cost_minor / 100,
        total_cost=result.total_cost_minor / 100 if result.total_cost_minor is not None else None,
        solve_seconds=result.elapsed_s,
        optimized_tiers=result.optimized_tiers,
        total_tiers=result.total_tiers,
        objective_bound=result.objective_bound,
        reason="当前数值假设下有试排，但仍需补充/核实事实" if unknown and result.visits else result.reason,
    )


def _build_plans(request, start, results, prepared, matrix, stay_nights):
    plans = []
    by_id = {c.fact.candidate_id: c for c in prepared}
    for index, result in enumerate(results):
        day_no, local_date = index + 1, start + timedelta(days=index)
        items = []
        for visit in result.visits:
            item = by_id[visit.candidate_id].item.model_copy(deep=True)
            item.start_time = f"{visit.start_min // 60:02}:{visit.start_min % 60:02}"
            item.end_time = f"{visit.end_min // 60:02}:{visit.end_min % 60:02}"
            item.duration_min = visit.end_min - visit.start_min
            item.why_this = "根据本次时间要求及候选资料安排，出发前核实"
            items.append(item)
        assessment = _assessment(request, day_no, local_date, result, prepared, matrix, stay_nights)
        plans.append(
            DailyPlan(
                day_no=day_no,
                items=items,
                theme=f"{request.city} · 第{day_no}天",
                note="按候选资料与时间要求排程，事实核实状态见规划说明",
                constraint_planning=assessment,
            )
        )
    return plans


def _retain_days(problems, frozen_days, prepared, persons):
    frozen = {}
    for old in frozen_days or []:
        problem = next(p for p in problems if p.day_no == old.day_no)
        candidates = list(problem.candidates)
        visits = []
        for item in old.items:
            matches = [
                i
                for i, c in enumerate(prepared)
                if c.item.poi_name == item.poi_name and c.item.item_type == item.item_type
            ]
            if len(matches) != 1 or not item.start_time or not item.end_time:
                raise ValueError("恢复所需的已交付地点/时间无法与候选唯一对齐，停止覆盖")
            key = prepared[matches[0]].fact.candidate_id
            index = next(i for i, candidate in enumerate(candidates) if candidate.candidate_id == key)
            start, end = minute(item.start_time[:5]), minute(item.end_time[:5])
            visits.append(ScheduledVisit(candidates[index].candidate_id, start, end))
            price = money_minor(Decimal(str(item.cost)) * persons) if item.cost is not None else None
            candidates[index] = replace(
                candidates[index], duration_min=end - start, price_minor=price, currency=item.cost_currency or "UNKNOWN"
            )
        problems[problems.index(problem)] = replace(problem, candidates=tuple(candidates))
        frozen[old.day_no] = visits
    return frozen


def _problem_snapshots(request, prepared, start, saved=None, frozen_days=None):
    options = request.requirements_struct.constraint_planning
    req = align_requirement_names(request.requirements_struct, prepared)
    for fixed in req.fixed_arrangements:
        if fixed.cost is not None:
            fixed.cost *= request.persons
    points = []
    for candidate in prepared:
        item = candidate.item
        address_changed = any(f.name == item.poi_name and f.address is not None for f in req.fixed_arrangements)
        if address_changed:
            item.latitude = item.longitude = None
        points.append(
            {
                "poi_id": candidate.fact.candidate_id,
                "name": item.poi_name,
                "latitude": None if address_changed else item.latitude,
                "longitude": None if address_changed else item.longitude,
            }
        )
    # 主动策略从估算开始，只为选中的瓶颈路段查真实来源，避免先全矩阵外呼。
    route_matrix = RouteService().matrix if options.active_evidence else get_route_matrix
    mode = req.transport_preference or "walking"
    matrix = (
        {(a, b): route for a, b, route in saved["matrix"]}
        if saved and saved["points"] == points and saved["mode"] == mode
        else route_matrix(points, mode=mode)
    )

    def candidate_for_day(candidate, day_no):
        fixed = [
            f
            for f in req.fixed_arrangements
            if f.day_no == day_no and f.name == candidate.item.poi_name and f.item_type == candidate.item.item_type
        ]
        # 用户明确的预约跨度覆盖默认访问时长；不是修改场馆营业资料。
        duration = minute(fixed[0].end_time) - minute(fixed[0].start_time) if len(fixed) == 1 else None
        return planning_candidate(
            candidate, options, start + timedelta(days=day_no - 1), request.persons, duration_min=duration
        )

    problems = []
    for number in range(1, request.days + 1):
        names = requested_names(req, number)
        names.update(item.poi_name for day in frozen_days or [] if day.day_no == number for item in day.items)
        pool = day_pool(prepared, number, DAY_CANDIDATE_LIMIT, names, name=lambda c: c.item.poi_name, stride=6)
        keys = {c.fact.candidate_id for c in pool}
        routes = {edge: route for edge, route in matrix.items() if set(edge) <= keys}
        problems.append(
            DayPlanningProblem(
                req, tuple(candidate_for_day(c, number) for c in pool), routes, options.currency, day_no=number
            )
        )
    return matrix, problems


def run_constraint_planning(
    request: GenerateRequest,
    context: dict,
    *,
    stay_nights: int = 0,
    frozen_days: list[DailyPlan] | None = None,
    on_candidates: Callable[[list[TripItem]], None] | None = None,
    on_snapshot: Callable[[dict], None] | None = None,
) -> list[DailyPlan]:
    if not request.requirements_struct or not request.requirements_struct.constraint_planning:
        raise ValueError("约束规划需要显式目的地计价/时区确认")
    if not 1 <= request.days <= MAX_TRIP_DAYS:
        raise ValueError(f"约束规划支持同城 1–{MAX_TRIP_DAYS} 日")
    deadline = time.monotonic() + TRIP_PLANNING_SECONDS
    if context.get("_constraint_plans"):
        # 同一任务/需求/截止期的已完成求解结果，不恢复 CP 搜索树。
        return [DailyPlan.model_validate(plan) for plan in context["_constraint_plans"]]
    options = request.requirements_struct.constraint_planning
    start = date.fromisoformat(request.start_date) if request.start_date else clock.destination_today(options.time_zone)
    prepared = prepare_candidates(
        context,
        options,
        request.requirements_struct.place_inputs,
        limit=TRIP_CANDIDATE_LIMIT,
        preferred_names=requested_names(request.requirements_struct),
    )
    limits = current_limits()
    if limits:
        limits.check("deadline")
    if not prepared:
        results = [
            DayPlanningResult(
                status="UNKNOWN", solver_status="NOT_RUN", reason="未取得同一国家的有效候选池，未运行求解"
            )
            for _ in range(request.days)
        ]
        return _build_plans(request, start, results, [], {}, stay_nights)
    if on_candidates:
        on_candidates([candidate.item.model_copy(deep=True) for candidate in prepared])
    matrix, problems = _problem_snapshots(request, prepared, start, context.get("_constraint_routes"), frozen_days)
    if on_snapshot:
        points = [
            {
                "poi_id": c.fact.candidate_id,
                "name": c.item.poi_name,
                "latitude": c.item.latitude,
                "longitude": c.item.longitude,
            }
            for c in prepared
        ]
        # 与 _problem_snapshots 的固定地址覆盖语义一致。
        for point in points:
            if any(
                f.name == point["name"] and f.address is not None for f in problems[0].requirements.fixed_arrangements
            ):
                point["latitude"] = point["longitude"] = None
        context["_constraint_routes"] = {
            "points": points,
            "mode": request.requirements_struct.transport_preference or "walking",
            "matrix": [[a, b, route] for (a, b), route in matrix.items()],
        }
        on_snapshot(context)
    frozen = _retain_days(problems, frozen_days, prepared, request.persons)
    remaining = min(deadline - time.monotonic(), limits.remaining() if limits else TRIP_PLANNING_SECONDS)
    results = (
        solve_trip(
            problems,
            budget=Decimal(str(request.budget)) if request.budget is not None else None,
            time_limit_s=max(0.001, remaining),
            frozen_days=frozen,
        )
        if remaining > 0
        else [
            DayPlanningResult(status="UNKNOWN", solver_status="NOT_RUN", reason="整趟30秒预算在快照准备阶段耗尽")
            for _ in problems
        ]
    )
    evidence_report = None
    if options.active_evidence and time.monotonic() < deadline:

        def resolve(snapshot, seconds):
            available = min(seconds, deadline - time.monotonic())
            if available <= 0:
                return results
            return solve_trip(
                snapshot,
                budget=Decimal(str(request.budget)) if request.budget is not None else None,
                time_limit_s=available,
                frozen_days=frozen,
            )

        results, evidence_report = drive_evidence(
            problems,
            prepared,
            request,
            options=options.active_evidence.model_copy(
                update={
                    "time_limit_seconds": min(
                        options.active_evidence.time_limit_seconds, max(0.001, deadline - time.monotonic())
                    )
                }
            ),
            solve=resolve,
            observe=evidence_tools.query_evidence,
            initial=results,
            budget=Decimal(str(request.budget)) if request.budget is not None else None,
        )
        matrix = problems[0].route_matrix
    if limits:
        limits.check("deadline")
    record_event(
        "planning",
        "constraints.solve",
        metadata={
            "days": request.days,
            "candidates": len(prepared),
            "solver_status": results[0].solver_status,
            "currency": options.currency,
            "timezone": options.time_zone,
        },
    )
    plans = _build_plans(request, start, results, prepared, matrix, stay_nights)
    for plan in plans:
        plan.constraint_planning.active_evidence = evidence_report
    if on_snapshot:
        context["_constraint_plans"] = [plan.model_dump(mode="json", by_alias=True) for plan in plans]
        on_snapshot(context)
    return plans
