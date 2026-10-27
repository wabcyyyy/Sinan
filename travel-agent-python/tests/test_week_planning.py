"""Bounded weekly joint planning checked against an independent, explicitly feasible reference."""

from decimal import Decimal
from itertools import pairwise

import pytest

from app.agent.generation.content.complete_travel_solver import _nodes
from app.agent.generation.content.constraint_problem import DayPlanningProblem
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.schemas.travel_plan import TravelRequirements
from app.schemas.trip import GenerateDayRequest
from app.schemas.trip_requirements import FixedArrangement, LodgingRequirements, RequiredPlace, TripRequirements


def weekly_fixture(days):
    attractions = [
        {
            "id": f"a{d}",
            "name": f"A{d}",
            "price": 10,
            "type": "fixture",
            "opentime": "08:00",
            "endtime": "17:00",
            "recommendmintime": 1,
        }
        for d in range(1, days + 1)
    ]
    foods, fixed = [], []
    for d in range(1, days + 1):
        for meal, start, end in (("lunch", "12:00", "12:45"), ("dinner", "18:00", "18:45")):
            name = f"{meal}{d}"
            foods.append(
                {
                    "id": name,
                    "name": name,
                    "price": 20,
                    "cuisine": "fixture",
                    "opentime": "11:00" if meal == "lunch" else "17:00",
                    "endtime": "14:00" if meal == "lunch" else "21:00",
                }
            )
            fixed.append(FixedArrangement(day_no=d, name=name, item_type="food", start_time=start, end_time=end))
    requirements = TripRequirements(
        fact_source="chinatravel",
        travel_planner="cp",
        fixed_arrangements=fixed,
        required_places=[
            RequiredPlace(constraint_id=f"required{d}", name=f"A{d}", day_no=d) for d in range(1, days + 1)
        ],
        lodging=LodgingRequirements(rooms=1, locked_hotel_identity="H"),
        travel_constraints=TravelRequirements(rooms=1, room_beds=2),
    )
    request = GenerateDayRequest(
        city="fixture-city", day_no=1, days=days, persons=2, requirements_struct=requirements, needs_hotel=days > 1
    )
    facts = {
        "candidates": attractions,
        "foods": foods,
        "hotels": [{"id": "h", "name": "H", "price": 100, "numbed": 2, "capacity": 2}],
    }
    outbound = {
        "_mode": "train",
        "TrainID": "OUT",
        "From": "origin",
        "To": "station",
        "BeginTime": "06:00",
        "EndTime": "07:00",
        "Cost": 100,
    }
    inbound = {
        "_mode": "train",
        "TrainID": "BACK",
        "From": "station",
        "To": "origin",
        "BeginTime": "20:00",
        "EndTime": "21:00",
        "Cost": 100,
    }
    nodes, templates, positions = _nodes(request, facts, outbound, inbound)
    problems = []
    for d, candidates in enumerate(nodes, 1):
        routes = {
            (a.candidate_id, b.candidate_id): {
                "duration_min": 0 if a.name == b.name else 10,
                "source": "independent-reference",
                "cost_minor": 0 if a.name == b.name else 1000,
            }
            for a in candidates
            for b in candidates
            if a != b
        }
        problems.append(DayPlanningProblem(requirements, tuple(candidates), routes, "CNY", d))
    return request, facts, problems, templates, positions


def independent_reference_cost(days):
    # Two rail tickets per direction; one admission and two meals per day; one room per overnight;
    # four distinct-position transfers per day. This reference is feasible without consulting CP output.
    reference = [(600, 660), (720, 765), (1080, 1125)]
    assert reference[0][1] + 10 <= reference[1][0] and reference[1][1] + 10 <= reference[2][0]
    assert reference[0][0] >= 420 + 10 and reference[-1][1] + 10 <= 1200
    return Decimal(2 * 100 * 2 + days * (10 * 2 + 20 * 2 * 2) + (days - 1) * 100 + days * 4 * 10)


def check_schedule(results, problems, days):
    assert len(results) == days and all(r.status in {"FEASIBLE", "OPTIMAL"} for r in results)
    used_names, cost, nights = set(), 0, 0
    for result, problem in zip(results, problems, strict=True):
        lookup = {c.candidate_id: c for c in problem.candidates}
        visits = result.visits
        # Independent checks do not use the compiler's validation report.
        for visit in visits:
            c = lookup[visit.candidate_id]
            assert visit.end_min - visit.start_min == c.duration_min
            assert any(low <= visit.start_min and visit.end_min <= high for low, high in c.opening_windows)
            if c.item_type in {"attraction", "food"}:
                assert c.name not in used_names
                used_names.add(c.name)
            nights += c.item_type == "hotel"
            cost += c.price_minor
        for a, b in pairwise(visits):
            edge = problem.route_matrix[a.candidate_id, b.candidate_id]
            assert a.end_min + edge["duration_min"] <= b.start_min
            cost += edge["cost_minor"]
        for fixed in problem.requirements.fixed_arrangements:
            if fixed.day_no == problem.day_no:
                assert any(
                    lookup[v.candidate_id].name == fixed.name
                    and v.start_min == int(fixed.start_time[:2]) * 60
                    and v.end_min == int(fixed.end_time[:2]) * 60 + 45
                    for v in visits
                )
    assert nights == days - 1
    assert sum(r.total_cost_minor for r in results) == cost
    assert result.visits[-1].candidate_id.startswith("back:")
    return Decimal(cost) / 100


@pytest.mark.parametrize("days", [1, 3, 4, 5, 6, 7])
def test_joint_week_matches_independent_feasible_reference(days):
    _, _, problems, _, _ = weekly_fixture(days)
    budget = independent_reference_cost(days)
    results = solve_trip(problems, budget=budget, time_limit_s=2, shrink_conflicts=False)
    assert check_schedule(results, problems, days) == budget


def test_week_exact_budget_proves_infeasible_and_preserves_frozen_day():
    _, _, problems, _, _ = weekly_fixture(7)
    budget = independent_reference_cost(7)
    failed = solve_trip(problems, budget=budget - Decimal("0.01"), time_limit_s=2, shrink_conflicts=False)
    assert all(r.status == "INFEASIBLE" for r in failed)
    feasible = solve_trip(problems, budget=budget, time_limit_s=2, shrink_conflicts=False)
    locked = feasible[0].visits
    again = solve_trip(problems, budget=budget, frozen_days={1: locked}, time_limit_s=2, shrink_conflicts=False)
    assert again[0].visits == locked
    assert check_schedule(again, problems, 7) == budget


def test_late_day_seven_parse_and_contract_boundary():
    from app.agent.editing.replan_intent import parse_replan_change
    from app.schemas.replanning import ReplanParseBody

    change = parse_replan_change("第7天18:00前结束", [])
    assert change.change.day_no == 7 and change.change.end_time == "18:00"
    ReplanParseBody(planning_revision=0, message="fixture", day_no=7)
    with pytest.raises(ValueError):
        ReplanParseBody(planning_revision=0, message="fixture", day_no=8)


def test_week_pool_has_last_day_foods_and_enforces_room_capacity():
    request, facts, problems, _, _ = weekly_fixture(7)
    assert any(c.name == "lunch7" for c in problems[-1].candidates)
    assert all(len(p.candidates) <= 20 for p in problems)
    facts["hotels"][0]["capacity"] = 1
    out = {
        "_mode": "train",
        "TrainID": "OUT",
        "From": "origin",
        "To": "station",
        "BeginTime": "06:00",
        "EndTime": "07:00",
        "Cost": 100,
    }
    back = {**out, "TrainID": "BACK", "From": "station", "To": "origin", "BeginTime": "20:00", "EndTime": "21:00"}
    nodes, _, _ = _nodes(request, facts, out, back)
    assert not any(c.item_type == "hotel" for day in nodes for c in day)
    impossible = []
    for previous, candidates in zip(problems, nodes, strict=True):
        ids = {c.candidate_id for c in candidates}
        routes = {edge: value for edge, value in previous.route_matrix.items() if set(edge) <= ids}
        impossible.append(DayPlanningProblem(previous.requirements, tuple(candidates), routes, "CNY", previous.day_no))
    assert all(r.status == "INFEASIBLE" for r in solve_trip(impossible, time_limit_s=2, shrink_conflicts=False))


def test_seventh_day_hard_order_with_earlier_day_frozen():
    from app.schemas.trip_requirements import PlaceRelation

    _, _, problems, _, _ = weekly_fixture(7)
    requirements = problems[0].requirements
    requirements.place_relations = [
        PlaceRelation(constraint_id="late-order", kind="before", place_names=["A7", "lunch7"], day_no=7)
    ]
    initial = solve_trip(problems, budget=independent_reference_cost(7), time_limit_s=2, shrink_conflicts=False)
    locked = initial[0].visits
    results = solve_trip(
        problems,
        budget=independent_reference_cost(7),
        frozen_days={1: locked},
        time_limit_s=2,
        shrink_conflicts=False,
    )
    check_schedule(results, problems, 7)
    assert results[0].visits == locked
    by_name = {c.candidate_id: c.name for c in problems[-1].candidates}
    visits = {by_name[v.candidate_id]: v for v in results[-1].visits}
    assert visits["A7"].end_min <= visits["lunch7"].start_min


def test_generic_week_snapshot_rotates_bounded_pool_and_retains_late_constraints(monkeypatch):
    from datetime import date

    from app.agent.generation.content.constraint_facts import prepare_candidates
    from app.agent.generation.orchestration import constraint_planning as planner
    from app.schemas.trip_requirements import RequiredPlace
    from tests.constraint_planning_cases import frozen_context, planning_request

    request = planning_request(days=7)
    request.requirements_struct.required_places.append(RequiredPlace(constraint_id="last", name="地点39", day_no=7))
    context = frozen_context()
    prototype = context["candidates"][0]
    context["candidates"] = [{**prototype, "name": f"地点{i}", "id": f"p{i}"} for i in range(40)]
    prepared = prepare_candidates(context, request.requirements_struct.constraint_planning, limit=40)
    monkeypatch.setattr(planner, "get_route_matrix", lambda *args, **kwargs: {})
    _, problems = planner._problem_snapshots(request, prepared, date(2026, 10, 26))
    assert len(problems) == 7 and all(len(p.candidates) == 20 for p in problems)
    assert "地点39" in {c.name for c in problems[-1].candidates}
    assert {c.name for c in problems[0].candidates} != {c.name for c in problems[-1].candidates}
