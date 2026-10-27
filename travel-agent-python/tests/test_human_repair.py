"""Seen-failure repairs with self-made cases; never rewrite the frozen Human first pass."""

import json
from decimal import Decimal

import pytest

from app.agent.editing import clarify, complete_travel_intake
from app.agent.generation.content import complete_travel_solver as planner
from app.agent.generation.content.complete_travel_checks import request_issues
from app.agent.generation.content.constraint_trip_solver import solve_trip
from app.schemas.travel_plan import TravelPlan, TravelRequirements
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from tests.test_week_planning import independent_reference_cost, weekly_fixture


def test_unknown_fares_are_recorded_before_sort_and_known_alternatives_reach_solver(monkeypatch):
    request, facts, _, _, _ = weekly_fixture(1)
    row = {"TrainID": "known", "From": "origin", "To": "station", "Cost": 100, "BeginTime": "06:00", "EndTime": "07:00"}
    back = {**row, "From": "station", "To": "origin", "BeginTime": "20:00", "EndTime": "21:00"}
    invalid = [None, float("nan"), -1, True, "100"]
    facts["intercity"] = {
        "outbound": {"train": [{**row, "Cost": v} for v in invalid] + [row]},
        "return": {"train": [back]},
    }
    monkeypatch.setattr(planner, "catalog", lambda *_: facts)
    monkeypatch.setattr(
        planner,
        "_route",
        lambda *_: {
            "duration_min": 1,
            "cost_minor": 0,
            "mode": "walk",
            "actual_modes": ["walk"],
            "source": "self-made",
        },
    )

    def reached(problems, **kwargs):
        assert all(c.price_minor >= 0 for p in problems for c in p.candidates)
        raise RuntimeError("known-priced solver reached")

    monkeypatch.setattr(planner, "solve_trip", reached)
    with pytest.raises(RuntimeError, match="known-priced solver reached"):
        planner.generate(request)
    trace = request.context["travel_solver_trace"]
    assert len([r for r in trace if r.get("status") == "unknown-intercity-fare"]) == len(invalid)
    assert next(r for r in trace if r.get("stage") == "timetable-domain")["eligible"] == 1


def test_all_unknown_fares_do_not_become_zero_cost_or_global_infeasible(monkeypatch):
    request, facts, _, _, _ = weekly_fixture(1)
    row = {"Cost": None, "BeginTime": "06:00", "EndTime": "07:00"}
    facts["intercity"] = {"outbound": {"train": [row]}, "return": {"train": [row]}}
    monkeypatch.setattr(planner, "catalog", lambda *_: facts)
    with pytest.raises(ValueError, match="bounded CP"):
        planner.generate(request)
    assert "travel_last_output" not in request.context
    assert len([r for r in request.context["travel_solver_trace"] if r.get("status") == "unknown-intercity-fare"]) == 2


def test_person_conflict_requires_explicit_choice_and_survives_direct_start(monkeypatch):
    monkeypatch.setattr(
        clarify, "_ask", lambda *_: json.dumps({"city": "深圳", "origin_city": "武汉", "days": 3, "persons": 5})
    )
    first = clarify.run_clarify(ClarifyRequest(message="[旅行人数4] 我和4个同学去深圳三天"))
    assert first.blocked and not first.ready and first.state.persons is None
    assert first.state.person_count_options == [4, 5] and first.options == ["4 人", "5 人"]
    pending = clarify.run_clarify(ClarifyRequest(message="直接开始", state=first.state))
    assert pending.blocked and pending.state.persons is None
    resolved = clarify.run_clarify(ClarifyRequest(message="确认4人", state=pending.state))
    assert resolved.state.persons == 4 and not resolved.state.person_count_options and not resolved.blocked


def test_person_options_have_the_same_contract_boundary_as_persons():
    with pytest.raises(ValueError):
        IntakeState(person_count_options=[4, 21])


def public_context():
    return {
        "candidates": [{"name": f"地点{i}", "type": "景点"} for i in range(18)]
        + [{"name": "杭州匿名海洋馆", "type": "景点"}],
        "foods": [],
        "hotels": [],
    }


def test_explicit_public_entity_beyond_first16_is_exposed_without_gold(monkeypatch):
    observed = []

    class Client:
        def complete(self, prompt, **kwargs):
            observed.append(json.loads(prompt))
            return '{"required_names":["杭州匿名海洋馆"]}'

    monkeypatch.setattr(complete_travel_intake, "research", lambda *_: public_context())
    state = IntakeState(city="杭州", origin_city="上海", days=2, persons=2)
    result = complete_travel_intake.extract("必须去匿名海洋馆", state, Client())
    assert result.required_names == ["杭州匿名海洋馆"]
    assert "杭州匿名海洋馆" in observed[0]["names"]["candidates"]
    assert "hard_logic" not in json.dumps(observed)


def test_nightly_hotel_cap_is_extracted_as_a_separate_typed_amount(monkeypatch):
    class Client:
        def complete(self, prompt, **kwargs):
            assert "每人每晚300" in prompt and "max_hotel_cost_per_person_per_night" in kwargs["system_prompt"]
            return '{"max_hotel_cost_per_person_per_night":300}'

    monkeypatch.setattr(complete_travel_intake, "research", lambda *_: public_context())
    state = IntakeState(city="杭州", origin_city="上海", days=2, persons=2, budget=2000)
    result = complete_travel_intake.extract("总预算2000，住宿每人每晚300以内", state, Client())
    assert result.max_hotel_cost_per_person_per_night == Decimal(300) and state.budget == 2000


def test_hotel_cap_is_exact_per_night_and_independent_of_total_budget():
    request, facts, problems, _, _ = weekly_fixture(2)
    cap = request.requirements_struct.travel_constraints
    cap.max_hotel_cost_per_person_per_night = Decimal("49.99")
    # Rebuild actual source candidates; no hotel at CNY100 fits two people x49.99.
    out = {
        "_mode": "train",
        "TrainID": "OUT",
        "From": "origin",
        "To": "station",
        "Cost": 100,
        "BeginTime": "06:00",
        "EndTime": "07:00",
    }
    back = {**out, "TrainID": "BACK", "From": "station", "To": "origin", "BeginTime": "20:00", "EndTime": "21:00"}
    nodes, _, _ = planner._nodes(request, facts, out, back)
    assert not any(c.item_type == "hotel" for day in nodes for c in day)
    from app.agent.generation.content.constraint_problem import DayPlanningProblem

    impossible = [
        DayPlanningProblem(
            p.requirements,
            tuple(cs),
            {edge: r for edge, r in p.route_matrix.items() if set(edge) <= {c.candidate_id for c in cs}},
            "CNY",
            p.day_no,
        )
        for p, cs in zip(problems, nodes, strict=True)
    ]
    assert all(r.status == "INFEASIBLE" for r in solve_trip(impossible, time_limit_s=1, shrink_conflicts=False))
    cap.max_hotel_cost_per_person_per_night = Decimal("50")
    nodes, _, _ = planner._nodes(request, facts, out, back)
    assert any(c.item_type == "hotel" for day in nodes for c in day)
    valid = solve_trip(problems, budget=independent_reference_cost(2), time_limit_s=1, shrink_conflicts=False)
    assert all(r.status in {"FEASIBLE", "OPTIMAL"} for r in valid)


def test_final_gate_rejects_over_cap_even_if_total_budget_and_unit_price_pass():
    from types import SimpleNamespace

    plan = TravelPlan.model_validate(
        {
            "people_number": 2,
            "start_city": "甲",
            "target_city": "乙",
            "itinerary": [
                {
                    "day": 1,
                    "activities": [
                        {
                            "type": "accommodation",
                            "position": "H",
                            "start_time": "20:00",
                            "end_time": "20:30",
                            "price": 100,
                            "cost": 100,
                            "rooms": 1,
                            "room_type": 2,
                            "transports": [],
                        }
                    ],
                }
            ],
        }
    )
    request = SimpleNamespace(
        days=1,
        persons=2,
        city="乙",
        budget=2000,
        needs_hotel=True,
        requirements_struct=TripRequirements(
            travel_constraints=TravelRequirements(max_hotel_cost_per_person_per_night=Decimal("49.99"))
        ),
        context={"origin_city": "甲", "requested_stay_nights": 1, "hotels": [], "foods": [], "candidates": []},
    )
    assert "parsed hotel per-person per-night cost cap exceeded" in request_issues(plan, request)
