"""1–7 日共享 CP 模型：每日路径 + 全程不重复、全程必去与同币费用上限。"""

from dataclasses import replace
from decimal import Decimal

from ortools.sat.python import cp_model

from app.agent.generation.content.constraint_compile import CompiledDay, compile_day
from app.agent.generation.content.constraint_problem import DayPlanningProblem, money_minor
from app.agent.generation.content.constraint_replan_model import apply_replan
from app.agent.generation.rules.day_policy import day_policy_for
from app.schemas.trip_requirements import MAX_TRIP_DAYS


def _delivery_shape(day: CompiledDay) -> None:
    if not day.problem.requirements.constraint_planning:
        return
    policy = day_policy_for(day.problem.requirements, day.problem.day_no)
    active = [(i, c) for i, c in enumerate(day.problem.candidates) if c.item_type in ("attraction", "food")]
    guard = day.hard(
        f"delivery:active:{day.problem.day_no}",
        "day_window",
        f"现有交付规则：本日有效活动至少 {policy.min_active_minutes} 分钟",
        evidence=["DayPolicy.min_active_minutes"],
    )
    day.model.add(
        sum(c.duration_min * day.selected[i] for i, c in active) >= policy.min_active_minutes
    ).only_enforce_if(guard)
    if policy.playable:
        guard = day.hard(
            f"delivery:attraction:{day.problem.day_no}",
            "attraction_limit",
            "现有交付规则：可游玩日须至少有一处景点",
            evidence=["DayPolicy.playable"],
        )
        day.model.add(sum(day.selected[i] for i, c in active if c.item_type == "attraction") >= 1).only_enforce_if(
            guard
        )


def compile_trip(
    problems: list[DayPlanningProblem], budget: Decimal | None, frozen_days=None, repair=None
) -> list[CompiledDay]:
    if not 1 <= len(problems) <= MAX_TRIP_DAYS or len({p.day_no for p in problems}) != len(problems):
        raise ValueError(f"约束规划支持单城 1–{MAX_TRIP_DAYS} 日，每日编号唯一")
    if len({p.currency for p in problems}) != 1:
        raise ValueError("同一行程只能使用一个计价币种")
    model = cp_model.CpModel()
    compiled = []
    for problem in problems:
        req = problem.requirements.model_copy(deep=True)
        # 全程必去和总预算在共享模型上编译，不错当作每天都必须/各自拥有整趟预算。
        req.required_places = [p for p in req.required_places if p.day_no is not None]
        req.budget_policy = None
        compiled.append(compile_day(replace(problem, requirements=req, budget=None), model=model))
        _delivery_shape(compiled[-1])
    identifiers = {c.candidate_id for problem in problems for c in problem.candidates}
    for key in identifiers:
        variables = [
            day.selected[i] for day in compiled for i, c in enumerate(day.problem.candidates) if c.candidate_id == key
        ]
        model.add(sum(variables) <= 1)
    first = compiled[0]
    for day in compiled:
        if not frozen_days or day.problem.day_no not in frozen_days:
            continue
        frozen = {v.candidate_id: v for v in frozen_days[day.problem.day_no]}
        guard = day.hard(f"retained-day:{day.problem.day_no}", "locked_item", "恢复任务保留已交付日，不重新改写")
        for i, candidate in enumerate(day.problem.candidates):
            visit = frozen.get(candidate.candidate_id)
            model.add(day.selected[i] == int(visit is not None)).only_enforce_if(guard)
            if visit:
                model.add(day.starts[i] == visit.start_min).only_enforce_if(guard)
    for place in problems[0].requirements.required_places:
        if place.day_no is not None:
            continue
        guard = first.hard(
            f"required:{place.constraint_id}",
            "required_place",
            f"全程必去「{place.name}」",
            [place.name],
            [f"turn:{place.source_turn_id}"] if place.source_turn_id else [],
        )
        variables = [day.selected[i] for day in compiled for i in day.matches(place.name)]
        model.add(sum(variables) >= 1).only_enforce_if(guard)
    policy = problems[0].requirements.budget_policy
    if policy and (policy.mode == "target" or policy.include_intercity_transport):
        first.gap("budget:uncompiled-policy", "目标预算或城际费用口径尚未编译，不能视为已满足")
    if policy and policy.mode == "hard_cap":
        if budget is None:
            first.gap("budget:missing", "缺少全程硬预算金额")
        else:
            guard = first.hard("budget:trip-hard-cap", "budget_strict", f"同币已列活动费用上限 {budget}")
            model.add(
                sum((price or 0) * day.selected[i] for day in compiled for i, price in enumerate(day.prices))
                <= money_minor(budget)
            ).only_enforce_if(guard)
    if repair is not None:
        apply_replan(compiled, repair)
    if problems[0].requirements.fact_source == "chinatravel":
        from app.agent.generation.content.complete_travel_compile import compile_complete_trip

        compile_complete_trip(compiled, budget)
    return compiled


def trip_objectives(days: list[CompiledDay]) -> list:
    priorities = sorted(
        {r.priority for day in days for r in day.problem.requirements.place_relations if r.strength == "soft"},
        reverse=True,
    )
    tiers: dict[int, list] = {priority: [] for priority in priorities}
    for day in days:
        own = sorted(
            {
                r.priority
                for r in day.problem.requirements.place_relations
                if r.strength == "soft" and r.day_no == day.problem.day_no
            },
            reverse=True,
        )
        for index, priority in enumerate(own):
            tiers[priority].append(day.objectives[index])
    # 每天最后三层的语义一致：收益、交通、开始时刻；在全程上分别求和。
    return [sum(tiers[p]) for p in priorities] + [sum(day.objectives[index] for day in days) for index in (-3, -2, -1)]
