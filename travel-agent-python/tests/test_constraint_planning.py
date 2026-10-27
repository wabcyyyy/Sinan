from datetime import datetime
from decimal import Decimal
from itertools import permutations

import pytest

from app.agent.editing.clarify import _user_line
from app.agent.editing.clarify_policy import _budget_options, _ppd
from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.agent.generation.orchestration import constraint_planning as planner
from app.agent.research.supervisor import decompose
from app.common import clock
from app.schemas.trip import ClarifyRequest, DailyPlan
from app.schemas.trip_requirements import BudgetPolicy, DayWindow, IntakeState, RequiredPlace, TripRequirements
from tests.constraint_planner_oracle import _cost, _earliest, objective, schedule_violations
from tests.constraint_planning_cases import frozen_context, planning_request


def _assessment(plan: DailyPlan):
    assert plan.constraint_planning is not None
    return plan.constraint_planning


def test_typographic_apostrophe_aligns_only_unique_explicit_place_name():
    from app.agent.generation.content.constraint_facts import align_requirement_names, prepare_candidates
    from app.schemas.planning import ConstraintPlanningOptions

    options = ConstraintPlanningOptions(country_code="FR", time_zone="Europe/Paris", currency="EUR")
    req = TripRequirements(required_places=[RequiredPlace(constraint_id="orsay", name="Musée d’Orsay")])
    context: dict = {"candidates": [{"name": "Musée d'Orsay", "id": "museum", "country_code": "FR"}]}
    prepared = prepare_candidates(context, options)
    assert align_requirement_names(req, prepared).required_places[0].name == "Musée d'Orsay"
    assert req.required_places[0].name == "Musée d’Orsay"
    context["candidates"].append(
        {"name": "Other branch", "aliases": ["Musée d'Orsay"], "id": "other", "country_code": "FR"}
    )
    assert align_requirement_names(req, prepare_candidates(context, options)).required_places[0].name == "Musée d’Orsay"


def test_joint_two_day_solver_matches_independent_assignment_and_order_enumeration():
    candidates = tuple(
        PlanningCandidate(str(i), f"P{i}", 60, 100 * (i + 1), "EUR", ((540, 720),), utility=i + 1) for i in range(5)
    )
    matrix = {
        (a.candidate_id, b.candidate_id): {"duration_min": 5 + 2 * int(a.candidate_id), "source": "fixture"}
        for a in candidates
        for b in candidates
        if a != b
    }
    req = TripRequirements(
        required_places=[RequiredPlace(constraint_id="global", name="P0")],
        day_windows=[DayWindow(day_no=day, not_before="09:00", finish_by="12:00") for day in (1, 2)],
        budget_policy=BudgetPolicy(mode="hard_cap"),
    )
    problems = [DayPlanningProblem(req, candidates, matrix, "EUR", day_no=day) for day in (1, 2)]
    best = None
    for size in range(1, 6):
        for order in permutations(candidates, size):
            if "0" not in {c.candidate_id for c in order}:
                continue
            for cut in range(size + 1):
                visits = [_earliest(problems[0], order[:cut]), _earliest(problems[1], order[cut:])]
                if any(v is None or schedule_violations(p, v) for p, v in zip(problems, visits, strict=True)):
                    continue
                if sum(_cost(p, v) for p, v in zip(problems, visits, strict=True)) > 1100:
                    continue
                scores = [objective(p, v) for p, v in zip(problems, visits, strict=True)]
                score = tuple(sum(s[i] for s in scores) for i in range(3))
                best = max(best, score) if best is not None else score
    results = solve_trip(problems, budget=Decimal("11"))
    assert all(result.status == "OPTIMAL" for result in results)
    assert tuple(sum(r.objective[i] for r in results) for i in range(3)) == best
    ids = [v.candidate_id for result in results for v in result.visits]
    assert "0" in ids and len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "currency,country,zone",
    [("CNY", "CN", "Asia/Shanghai"), ("JPY", "JP", "Asia/Tokyo"), ("EUR", "FR", "Europe/Paris")],
)
def test_locale_amounts_and_unknown_routes_remain_separate(currency, country, zone):
    request = planning_request(currency=currency, country=country, zone=zone)
    plans = planner.run_constraint_planning(request, frozen_context(currency, country))
    assert len(plans) == 2
    assert all(_assessment(p).numerical_status == "OPTIMAL" for p in plans)
    assert all(_assessment(p).status == "UNKNOWN" for p in plans)  # 坐标估算不能冒充真实路线。
    assert [_assessment(p).local_date.isoformat() for p in plans] == ["2026-10-25", "2026-10-26"]
    assert {item.cost_currency for plan in plans for item in plan.items} == {currency}
    assert {item.cost for plan in plans for item in plan.items} == {10, 11, 12, 13, 14, 15}
    assert all(_assessment(p).destination.time_zone == zone for p in plans)


def test_foreign_price_is_preserved_but_not_in_target_total():
    context = frozen_context()
    context["candidates"][0]["cost_currency"] = "USD"
    plans = planner.run_constraint_planning(planning_request(), context)
    item = next(item for plan in plans for item in plan.items if item.poi_name == "地点0")
    assert item.cost == 10 and item.cost_currency == "USD"
    assert any(
        f.original_currency == "USD" and f.original_amount == 10 for plan in plans for f in _assessment(plan).facts
    )
    assert all(_assessment(p).status == "UNKNOWN" for p in plans)


def test_retained_day_is_frozen_when_task_resumes():
    request = planning_request()
    first = planner.run_constraint_planning(request, frozen_context())
    again = planner.run_constraint_planning(request, frozen_context(), frozen_days=[first[0]])
    assert [(i.poi_name, i.start_time, i.end_time) for i in again[0].items] == [
        (i.poi_name, i.start_time, i.end_time) for i in first[0].items
    ]
    assert not {i.poi_id for i in again[0].items} & {i.poi_id for i in again[1].items}


def test_wrong_country_and_missing_source_are_not_global_coverage():
    request = planning_request()
    assert all(
        not p.items and _assessment(p).status == "UNKNOWN"
        for p in planner.run_constraint_planning(request, frozen_context(country="JP"))
    )


def test_candidate_matching_keeps_distinct_same_name_branches_unknown():
    context = frozen_context()
    context["candidates"][1]["name"] = "地点0"
    plans = planner.run_constraint_planning(planning_request(), context)
    assert any(
        check.constraint_id.startswith("entity:") for plan in plans for check in _assessment(plan).report.unknown_checks
    )


def test_three_days_share_one_pool_and_existing_delivery_minimum():
    context = frozen_context()
    for i in range(6, 9):
        context["candidates"].append(
            {
                **context["candidates"][i - 6],
                "id": f"p{i}",
                "name": f"地点{i}",
                "latitude": 48.86 + i * 0.001,
                "longitude": 2.33 + i * 0.001,
            }
        )
    request = planning_request(days=3).model_copy(update={"budget": 1000})
    plans = planner.run_constraint_planning(request, context)
    assert len(plans) == 3 and all(_assessment(p).numerical_status == "OPTIMAL" for p in plans)
    assert all(sum(i.duration_min or 0 for i in p.items) >= 240 for p in plans)
    ids = [i.poi_id for p in plans for i in p.items]
    assert len(ids) == 9 == len(set(ids))


def test_foreign_budget_and_relative_dates_use_confirmed_destination(monkeypatch):
    confirmed = planning_request().requirements_struct
    assert confirmed is not None
    state = IntakeState(city="Paris", days=2, persons=2, budget=200, requirements=confirmed)
    assert _ppd(state) is None and any("EUR" in text for text in _budget_options(state))
    assert all(task.budget_currency == "EUR" for task in decompose(planning_request()))
    monkeypatch.setattr(clock, "shanghai_now", lambda: datetime(2026, 10, 25, 0, 30, tzinfo=clock.PRODUCT_ZONE))
    prompt = _user_line(ClarifyRequest(message="确认"), {"budget": 200}, state)
    assert "今天是 2026-10-24" in prompt and "以 EUR 计价" in prompt


def test_alias_shared_by_distinct_entities_is_unknown_instead_of_false_infeasible():
    context = frozen_context()
    context["candidates"][0]["aliases"] = ["同名馆"]
    context["candidates"][1]["aliases"] = ["同名馆"]
    request = planning_request()
    assert request.requirements_struct is not None
    request.requirements_struct.required_places[0].name = "同名馆"
    plans = planner.run_constraint_planning(request, context)
    assert all(_assessment(p).numerical_status == "OPTIMAL" for p in plans)
    assert any(c.constraint_id == "entity:同名馆" for p in plans for c in _assessment(p).report.unknown_checks)
