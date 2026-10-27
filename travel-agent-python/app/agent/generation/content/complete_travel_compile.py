"""Extend the existing shared CP circuit with source-accurate whole-trip semantics, not a second planner."""

from collections import defaultdict
from decimal import Decimal

from app.agent.generation.content.constraint_problem import money_minor


def compile_complete_trip(days, budget):
    model = days[0].model
    food_groups, stays, origins = defaultdict(list), {}, {}
    total = []
    for day in days:
        groups = defaultdict(list)
        for i, candidate in enumerate(day.problem.candidates):
            role = candidate.candidate_id.split(":")[0]
            literal = day.selected[i]
            groups[role].append(literal)
            total.append((candidate.price_minor or 0) * literal)
            if role in {"lunch", "dinner"}:
                food_groups[candidate.name].append(literal)
            if role == "stay":
                stays[day.problem.day_no, candidate.name] = literal
            if role == "origin":
                origins[day.problem.day_no, candidate.name] = literal
            if role in {"out", "back", "origin", "stay"}:
                if role in {"out", "back"}:
                    model.add(literal == 1)
                for j, other in enumerate(day.problem.candidates):
                    if j == i:
                        continue
                    if role in {"out", "origin"}:
                        model.add(day.starts[j] >= day.starts[i] + candidate.duration_min).only_enforce_if(
                            [literal, day.selected[j]]
                        )
                    else:
                        model.add(day.starts[j] + other.duration_min <= day.starts[i]).only_enforce_if(
                            [literal, day.selected[j]]
                        )
        for role in ("lunch", "dinner"):
            model.add(sum(groups[role]) <= 1)
        if day.problem.day_no < max(d.problem.day_no for d in days):
            model.add(sum(groups["stay"]) == 1)
        if day.problem.day_no > min(d.problem.day_no for d in days):
            model.add(sum(groups["origin"]) == 1)
        if min(d.problem.day_no for d in days) < day.problem.day_no < max(d.problem.day_no for d in days):
            model.add(sum(groups["attraction"] + groups["lunch"] + groups["dinner"]) >= 1)
        for (i, j), arc in day.arcs.items():
            route = day.problem.route_matrix.get(
                (day.problem.candidates[i].candidate_id, day.problem.candidates[j].candidate_id)
            )
            if route is None:
                model.add(arc == 0)  # Unknown routes cannot be free zero-time edges in this environment.
            elif route.get("options"):
                for k, option in enumerate(route["options"]):
                    total.append(option["cost_minor"] * day.route_choices[i, j, k])
                    if day.problem.candidates[i].candidate_id.startswith("origin:"):
                        model.add(day.starts[j] == day.starts[i] + option["duration_min"]).only_enforce_if(
                            day.route_choices[i, j, k]
                        )
            else:
                total.append(route["cost_minor"] * arc)
                if day.problem.candidates[i].candidate_id.startswith("origin:"):
                    model.add(day.starts[j] == day.starts[i] + route["duration_min"]).only_enforce_if(arc)
    for variables in food_groups.values():
        model.add(sum(variables) <= 1)
    constraints = days[0].problem.requirements.travel_constraints
    if constraints:
        candidates = [(day, i, c) for day in days for i, c in enumerate(day.problem.candidates)]
        for mode in constraints.required_innercity_modes:
            choices = []
            for day in days:
                for (i, j, k), variable in day.route_choices.items():
                    route = day.problem.route_matrix[
                        day.problem.candidates[i].candidate_id, day.problem.candidates[j].candidate_id
                    ]
                    if mode in route["options"][k]["actual_modes"]:
                        choices.append(variable)
            model.add(sum(choices) >= 1)
        for role, attribute in (
            ("attraction", "attraction_types"),
            ("lunch", "restaurant_cuisines"),
            ("stay", "hotel_features"),
        ):
            for value in getattr(constraints, attribute):
                roles = {"lunch", "dinner"} if role == "lunch" else {role}
                model.add(
                    sum(
                        day.selected[i]
                        for day, i, c in candidates
                        if c.candidate_id.split(":")[0] in roles and value in (c.categories or ())
                    )
                    >= 1
                )
        for name in constraints.required_names:
            model.add(sum(day.selected[i] for day, i, c in candidates if c.name == name) >= 1)
        for name in constraints.excluded_names:
            for day, i, c in candidates:
                if c.name == name:
                    model.add(day.selected[i] == 0)
    for (number, name), literal in origins.items():
        model.add(literal == stays[number - 1, name])
    if budget is not None:
        guard = days[0].hard("travel:all-costs", "budget_strict", "全部活动与逐段交通的全程精确总价")
        model.add(sum(total) <= money_minor(Decimal(str(budget)))).only_enforce_if(guard)
