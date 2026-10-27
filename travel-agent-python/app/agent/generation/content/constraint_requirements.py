"""既有需求到单日模型的编译规则；不保存第二份用户需求。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.agent.generation.content.constraint_problem import minute, money_minor

if TYPE_CHECKING:
    from app.agent.generation.content.constraint_compile import CompiledDay


def compile_places(compiled: CompiledDay) -> None:
    problem, model, selected = compiled.problem, compiled.model, compiled.selected
    req = problem.requirements
    for place in req.required_places:
        if place.day_no is None:
            compiled.gap(f"scope:{place.constraint_id}", "全程必去尚未分配日期，P0 单日不替用户决定", [place.name])
        elif place.day_no == problem.day_no:
            found = compiled.matches(place.name)
            guard = compiled.hard(
                f"required:{place.constraint_id}",
                "required_place",
                f"指定日必去「{place.name}」" if found else f"候选池缺少必去「{place.name}」",
                [place.name],
                [f"turn:{place.source_turn_id}"] if place.source_turn_id else [],
            )
            model.add(sum(selected[i] for i in found) >= 1).only_enforce_if(guard)
    for name in req.excluded_places:
        guard = compiled.hard(f"excluded:{name}", "excluded_place", f"用户排除「{name}」", [name])
        for i in compiled.matches(name):
            model.add(selected[i] == 0).only_enforce_if(guard)
    for category in req.excluded_categories:
        guard = compiled.hard(f"excluded-category:{category}", "excluded_category", f"用户排除类别 {category}")
        for i, candidate in enumerate(problem.candidates):
            if candidate.categories is None:
                compiled.gap(f"category:{candidate.candidate_id}", "排除类别所需的候选分类未知", [candidate.name])
            elif category in candidate.categories:
                model.add(selected[i] == 0).only_enforce_if(guard)
    if req.max_attractions_per_day is not None:
        guard = compiled.hard("attraction-limit", "attraction_limit", "显式每日景点上限")
        model.add(
            sum(selected[i] for i, c in enumerate(problem.candidates) if c.item_type == "attraction")
            <= req.max_attractions_per_day
        ).only_enforce_if(guard)


def compile_fixed(compiled: CompiledDay) -> None:
    problem, model, selected = compiled.problem, compiled.model, compiled.selected
    fixed_costs: dict[int, int | None] = {}
    for k, fixed in enumerate(problem.requirements.fixed_arrangements):
        if fixed.day_no != problem.day_no:
            continue
        found = [i for i in compiled.matches(fixed.name) if problem.candidates[i].item_type == fixed.item_type]
        guard = compiled.hard(
            f"fixed:{problem.day_no}:{k}",
            "locked_item",
            f"固定安排「{fixed.name}」{fixed.start_time}–{fixed.end_time}",
            [fixed.name],
        )
        model.add(sum(selected[i] for i in found) == 1).only_enforce_if(guard)
        for i in found:
            model.add(compiled.starts[i] == minute(fixed.start_time)).only_enforce_if([selected[i], guard])
            model.add(
                compiled.starts[i] + problem.candidates[i].duration_min == minute(fixed.end_time)
            ).only_enforce_if([selected[i], guard])
            if fixed.cost is not None:
                price = money_minor(fixed.cost)
                if i in fixed_costs and fixed_costs[i] != price:
                    compiled.gap(
                        f"price-conflict:{problem.candidates[i].candidate_id}", "同一固定安排有冲突金额", [fixed.name]
                    )
                    fixed_costs[i] = None
                else:
                    fixed_costs[i] = price
    compiled.prices = [
        fixed_costs.get(i, c.price_minor if c.currency == problem.currency else None)
        for i, c in enumerate(problem.candidates)
    ]


def compile_budget(compiled: CompiledDay) -> None:
    problem, req = compiled.problem, compiled.problem.requirements
    if req.budget_policy and req.budget_policy.mode == "hard_cap":
        if problem.budget is None:
            compiled.gap("budget:missing", "确认硬上限前需要既有 budget 金额")
        else:
            guard = compiled.hard(
                "budget:hard-cap", "budget_strict", f"单日快照硬上限 {problem.budget} {problem.currency}"
            )
            compiled.model.add(
                sum((price or 0) * compiled.selected[i] for i, price in enumerate(compiled.prices))
                <= money_minor(problem.budget)
            ).only_enforce_if(guard)


def compile_relations(compiled: CompiledDay) -> list:
    model, problem, selected = compiled.model, compiled.problem, compiled.selected
    soft: dict[int, list] = {}
    for relation in problem.requirements.place_relations:
        if relation.day_no != problem.day_no:
            continue
        groups = [compiled.matches(name) for name in relation.place_names]
        evidence = [f"turn:{relation.source_turn_id}"] if relation.source_turn_id else []
        if relation.source_text:
            evidence.append(relation.source_text)
        guard = (
            compiled.hard(
                f"relation:{relation.constraint_id}",
                "place_relation",
                f"用户关系 {relation.kind}",
                relation.place_names,
                evidence,
            )
            if relation.strength == "hard"
            else model.new_bool_var(f"soft:{relation.constraint_id}")
        )
        if relation.kind == "one_of":
            model.add(sum(selected[i] for group in groups for i in group) >= 1).only_enforce_if(guard)
        elif all(len(group) == 1 for group in groups):
            first, second = groups[0][0], groups[1][0]
            model.add(selected[first] == 1).only_enforce_if(guard)
            model.add(selected[second] == 1).only_enforce_if(guard)
            model.add(
                compiled.starts[first] + problem.candidates[first].duration_min <= compiled.starts[second]
            ).only_enforce_if(guard)
        elif any(not group for group in groups):
            model.add(guard == 0)
        else:
            compiled.gap(f"relation-entity:{relation.constraint_id}", "先后关系地点未消歧")
        if relation.strength == "soft":
            soft.setdefault(relation.priority, []).append(guard)
    return [sum(soft[priority]) for priority in sorted(soft, reverse=True)]


def unsupported_requirements(compiled: CompiledDay) -> None:
    policy = compiled.problem.requirements.budget_policy
    if policy and (policy.mode == "target" or policy.include_intercity_transport):
        compiled.gap("unsupported:budget-policy", "P0 不编译目标预算或城际费用口径，需后续接线")
    for name in (
        "pace",
        "dietary_requirement",
        "dining_evidence_policy",
        "transport_preference",
        "max_walk_minutes_per_leg",
        "lodging",
        "unresolved_requests",
    ):
        req = compiled.problem.requirements
        if (
            name == "transport_preference"
            and req.fact_source == "chinatravel"
            and (
                req.transport_preference == "unspecified"
                or (req.travel_constraints and req.travel_constraints.required_innercity_modes)
            )
        ):
            continue  # Official mode choices/presence are compiled by the complete-travel branch.
        if (
            name == "lodging"
            and compiled.problem.requirements.fact_source == "chinatravel"
            and compiled.problem.requirements.lodging is not None
        ):
            continue  # Complete travel compiles room counts, capacity policy and exact locked hotel identity.
        if getattr(compiled.problem.requirements, name):
            compiled.gap(f"unsupported:{name}", f"P0 尚不编译需求字段 {name}，不能视为满足")
