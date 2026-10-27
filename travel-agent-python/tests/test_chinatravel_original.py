"""Original-query isolation and complete business storage; fixtures are not model/official-score evidence."""

from decimal import Decimal

import pytest

from app.agent.editing.clarify import _user_line
from app.agent.generation.content.complete_travel import bind_routes, project
from app.schemas.travel_plan import TravelPlan
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from scripts.chinatravel_original_data import freeze, public_query, queries

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def sample():
    return {
        "people_number": 2,
        "start_city": "上海",
        "target_city": "杭州",
        "itinerary": [
            {
                "day": 1,
                "activities": [
                    {
                        "type": "train",
                        "TrainID": "fixture-go",
                        "start": "上海站",
                        "end": "杭州站",
                        "start_time": "07:00",
                        "end_time": "08:00",
                        "price": 50,
                        "cost": 100,
                        "tickets": 2,
                        "transports": [],
                    },
                    {
                        "type": "attraction",
                        "position": "fixture-POI",
                        "start_time": "09:00",
                        "end_time": "10:00",
                        "price": 20,
                        "cost": 40,
                        "tickets": 2,
                        "transports": [],
                    },
                ],
            }
        ],
    }


def test_original_containers_cohorts_and_oracle_isolation():
    from scripts.chinatravel_original_data import SOURCE

    if not (SOURCE / "upstream-data/easy.csv").exists():
        pytest.skip("official downloaded dataset is a separate local integration artifact")
    rows = queries()
    manifest = freeze()
    assert len(rows) == 300 and len(manifest["all"]) == 300
    assert set(manifest["dev"]).isdisjoint(manifest["confirm"])
    assert set(manifest["seen"]) <= set(manifest["dev"])
    assert all(isinstance(row["hard_logic_py"], list) for row in rows)
    assert public_query(rows[0]) == {"uid": rows[0]["uid"], "nature_language": rows[0]["nature_language"]}


def test_source_specific_intake_never_invents_a_calendar_date():
    state = IntakeState(requirements=TripRequirements(fact_source="chinatravel"))
    prompt = _user_line(ClarifyRequest(message="上海到杭州一天", state=state), {}, state)
    assert "今天是" not in prompt and "start_date保持null" in prompt


def test_business_research_projection_keeps_frozen_source_and_timetable(monkeypatch):
    from app.agent.generation.orchestration import plan_context
    from app.schemas.trip import GenerateRequest

    context = {
        "fact_source": "chinatravel",
        "relative_days": True,
        "intercity": {"outbound": []},
        "candidates": [],
        "foods": [],
        "hotels": [],
    }
    monkeypatch.setattr(plan_context, "run_research_context", lambda request: context)
    result = plan_context.run_plan_context(GenerateRequest(city="杭州", days=1))
    assert result is context


def test_complete_contract_preserves_units_and_roundtrip():
    raw = sample()
    plan = TravelPlan.model_validate(raw)
    assert plan.official() == raw
    assert TravelPlan.model_validate(plan.model_dump(by_alias=True)).official() == raw
    assert plan.total_cost() == Decimal(140)
    assert project(plan)[0].travel_activities[0].tickets == 2


def test_original_cost_guard_keeps_unknown_reserve_and_refuses_over_cap(tmp_path, monkeypatch):
    import json

    from app.common.config import settings
    from scripts.chinatravel_original_client import OriginalBudget
    from scripts.sinan_nl_budget import APPROVAL, NIGHT_UPGRADE

    monkeypatch.setattr(settings, "llm_enable_thinking", True)
    monkeypatch.setattr(settings, "llm_reasoning_effort", "low")

    # Synthetic unit fixture only: not a copy of the real ledger and never passed to an HTTP client.
    state = {
        "approval": APPROVAL,
        "approval_upgrade": NIGHT_UPGRADE,
        "cap_cny": "30",
        "carried_peak_upper_cny": "0",
        "records": [{"id": "test-only-unknown", "reserved_cny": "29", "status": "issued_usage_unknown"}],
    }
    path = tmp_path / "nonspendable-fixture.json"
    path.write_text(json.dumps(state), encoding="utf8")
    budget = OriginalBudget(path, approved=True)
    with pytest.raises(ValueError, match="upper bound"):
        budget.reserve(
            "not-issued",
            {
                "model": "deepseek-flash",
                "thinking": {"type": "enabled"},
                "reasoning_effort": "low",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": "fixture"}],
            },
        )
    assert json.loads(path.read_text("utf8")) == state and budget.total() == 29


def test_attribute_binding_uses_only_original_text_and_public_taxonomy(monkeypatch):
    from app.agent.editing import complete_travel_intake

    observed = []

    class Client:
        def complete(self, prompt, **kwargs):
            observed.append(prompt)
            return '{"restaurant_cuisines":["火锅"]}'

    monkeypatch.setattr(
        complete_travel_intake,
        "research",
        lambda request: {"candidates": [], "foods": [{"name": "公开火锅", "cuisine": "火锅"}], "hotels": []},
    )
    state = IntakeState(
        city="重庆", origin_city="武汉", days=2, persons=1, requirements=TripRequirements(fact_source="chinatravel")
    )
    result = complete_travel_intake.extract("想去重庆吃火锅", state, Client())
    assert result.restaurant_cuisines == ["火锅"]
    assert "hard_logic" not in observed[0] and "想去重庆吃火锅" in observed[0]


def test_complete_cp_links_overnight_boundaries_and_exact_transport_cost():
    from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate
    from app.agent.generation.content.constraint_trip_solver import solve_trip
    from app.schemas.trip_requirements import RequiredPlace

    req = TripRequirements(
        fact_source="chinatravel", required_places=[RequiredPlace(constraint_id="fixture-must", name="fixture-POI")]
    )
    nodes = [
        [
            PlanningCandidate("out:1", "station", 60, 10000, "CNY", ((480, 540),), item_type="transport"),
            PlanningCandidate("attraction:1", "fixture-POI", 60, 1000, "CNY", ((540, 1000),)),
            PlanningCandidate("stay:1:h", "hotel", 30, 5000, "CNY", ((0, 1440),), item_type="hotel"),
        ],
        [
            PlanningCandidate("origin:2:h", "hotel", 0, 0, "CNY", ((0, 1440),), item_type="anchor"),
            PlanningCandidate("back:1", "station", 60, 10000, "CNY", ((240, 300),), item_type="transport"),
        ],
    ]
    problems = []
    for day, candidates in enumerate(nodes, 1):
        matrix = {
            (a.candidate_id, b.candidate_id): {"duration_min": 10, "source": "fixture", "cost_minor": 1000}
            for a in candidates
            for b in candidates
            if a != b
        }
        problems.append(DayPlanningProblem(req, tuple(candidates), matrix, "CNY", day))
    failed = solve_trip(problems, budget=Decimal(280), time_limit_s=0.5, shrink_conflicts=False)
    assert all(result.status == "INFEASIBLE" for result in failed)
    passed = solve_trip(problems, budget=Decimal(290), time_limit_s=0.5, shrink_conflicts=False)
    assert all(result.status in {"OPTIMAL", "FEASIBLE"} for result in passed)
    assert passed[0].visits[-1].candidate_id == "stay:1:h"
    assert passed[1].visits[0].candidate_id == "origin:2:h"
    assert passed[1].visits[0].start_min == 230
    assert passed[1].visits[0].end_min == 230


def test_zero_duration_is_only_allowed_for_internal_source_origin():
    from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate

    for requirements, identity, kind in (
        (TripRequirements(), "origin:2:h", "anchor"),
        (TripRequirements(fact_source="chinatravel"), "attraction:1", "attraction"),
    ):
        with pytest.raises(ValueError, match="访问时长"):
            DayPlanningProblem(
                requirements,
                (PlanningCandidate(identity, "fixture", 0, 0, "CNY", ((0, 1440),), item_type=kind),),
                {},
                "CNY",
            )


def test_overnight_opening_and_same_entity_meal_roles_keep_original_semantics():
    from app.agent.generation.content.complete_travel_solver import _closing
    from app.agent.generation.content.constraint_compile import compile_day
    from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate
    from app.schemas.trip_requirements import RequiredPlace

    assert _closing({"opentime": "18:00", "endtime": "01:00"}) == 1440
    assert _closing({"opentime": "11:00", "endtime": "14:00"}) == 840
    requirements = TripRequirements(
        fact_source="chinatravel",
        required_places=[RequiredPlace(constraint_id="restaurant", name="fixture-restaurant", day_no=1)],
    )
    candidates = tuple(
        PlanningCandidate(f"{meal}:7", "fixture-restaurant", 45, 1000, "CNY", ((660, 1200),), item_type="food")
        for meal in ("lunch", "dinner")
    )
    day = compile_day(DayPlanningProblem(requirements, candidates, {}, "CNY"))
    assert not any(check.constraint_id.startswith("entity:") for check in day.gaps)
    assert day.matches("fixture-restaurant") == [0, 1]


def test_runtime_preferences_and_quantities_are_checked_without_gold():
    from types import SimpleNamespace

    from app.agent.generation.content.complete_travel_checks import request_issues
    from app.schemas.travel_plan import TravelRequirements

    request = SimpleNamespace(
        city="杭州",
        days=1,
        persons=2,
        budget=1500,
        needs_hotel=False,
        requirements_struct=TripRequirements(travel_constraints=TravelRequirements(restaurant_cuisines=["火锅"])),
        context={
            "origin_city": "上海",
            "candidates": [{"name": "fixture-POI", "type": "景点"}],
            "foods": [],
            "hotels": [],
        },
    )
    issues = request_issues(TravelPlan.model_validate(sample()), request)
    assert issues == ["parsed restaurant_cuisines missing: 火锅"]


def test_complete_cp_selects_per_edge_modes_with_presence_and_total_cap():
    from app.agent.generation.content.constraint_problem import DayPlanningProblem, PlanningCandidate
    from app.agent.generation.content.constraint_trip_solver import solve_trip
    from app.schemas.travel_plan import TravelRequirements
    from app.schemas.trip_requirements import RequiredPlace

    req = TripRequirements(
        fact_source="chinatravel",
        travel_constraints=TravelRequirements(required_innercity_modes=["metro"]),
        required_places=[RequiredPlace(constraint_id="must", name="POI")],
    )
    candidates = (
        PlanningCandidate("out:1", "station", 60, 10000, "CNY", ((480, 540),), item_type="transport"),
        PlanningCandidate("attraction:1", "POI", 60, 1000, "CNY", ((540, 1000),)),
        PlanningCandidate("back:1", "station", 60, 10000, "CNY", ((1080, 1140),), item_type="transport"),
    )
    options = [
        {"mode": "taxi", "actual_modes": ["taxi"], "duration_min": 1, "cost_minor": 1000},
        {"mode": "metro", "actual_modes": ["walk", "metro"], "duration_min": 20, "cost_minor": 500},
        {"mode": "walk", "actual_modes": ["walk"], "duration_min": 60, "cost_minor": 0},
    ]
    matrix = {
        (a.candidate_id, b.candidate_id): {
            "source": "fixture",
            "duration_min": 1,
            "cost_minor": 1000,
            "options": options,
        }
        for a in candidates
        for b in candidates
        if a != b
    }
    problem = DayPlanningProblem(req, candidates, matrix, "CNY")
    failed = solve_trip([problem], budget=Decimal(214), time_limit_s=0.5, shrink_conflicts=False)
    assert failed[0].status == "INFEASIBLE"
    result = solve_trip([problem], budget=Decimal(215), time_limit_s=0.5, shrink_conflicts=False)[0]
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert {r["mode"] for r in result.selected_routes} == {"walk", "metro"}
    assert result.total_cost_minor == 21500 and result.travel_min == 80


def test_route_grounding_executes_selected_quantity_without_time_repair(monkeypatch):
    from app.agent.data import chinatravel

    raw = sample()
    activity = raw["itinerary"][0]["activities"][1]
    activity["transfer_request"] = {"mode": "taxi", "departure": "08:30", "cars": 2}
    monkeypatch.setattr(
        chinatravel,
        "route",
        lambda *args: [
            {
                "start": args[1],
                "end": args[2],
                "mode": "taxi",
                "start_time": "08:30",
                "end_time": "09:15",
                "cost": 15.5,
                "distance": 7,
            }
        ],
    )
    bound, receipts = bind_routes(raw)
    result = bound["itinerary"][0]["activities"][1]
    assert result["start_time"] == "09:00"  # Arrives too late: keep the real failure for the checker.
    assert result["transports"][0]["cost"] == 31 and result["transports"][0]["cars"] == 2
    assert len(receipts) == 1


def test_complete_plan_api_worker_sqlite_refresh_preserves_full_fields(client, monkeypatch):
    from app.services import complete_travel
    from tests.test_constraint_business import _submit

    plan = TravelPlan.model_validate(sample())
    monkeypatch.setattr(complete_travel, "plan_complete_travel", lambda request: (project(plan), ["fixture-failure"]))
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload={},
        body_override={
            "city": "杭州",
            "originCity": "上海",
            "days": 1,
            "persons": 2,
            "startDate": None,
            "requirementsStruct": {"factSource": "chinatravel"},
            "budget": 1500,
        },
    )
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    refreshed = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert detail == refreshed
    day = detail["dayList"][0]
    restored = TravelPlan.model_validate(
        {**day["travelHeader"], "itinerary": [{"day": day["dayNo"], "activities": day["travelActivities"]}]}
    )
    assert restored.official() == sample()
    assert day["relativeDays"] and day["travelValidationIssues"] == ["fixture-failure"]
    assert detail["genState"] == "PARTIAL"
    assert sum(Decimal(str(v["amount"])) for v in detail["budgetList"]) == 140


@pytest.mark.parametrize(
    "phase,uid",
    [("dev-v2", "e20241028160248698752"), ("dev-v6", "m20241028164641561951"), ("dev-v5", "m20241028164646493412")],
)
def test_actual_nl_cp_output_business_save_readback(client, monkeypatch, phase, uid):
    import json

    from app.services import complete_travel, itinerary_generation
    from scripts.chinatravel_original_data import OUTPUT, save
    from tests.test_constraint_business import _submit

    path = OUTPUT / f"{phase}/results/{uid}-C.json"
    if not path.exists():
        pytest.skip("separate local real-NL output artifact is unavailable")
    observed = json.loads(path.read_text("utf8"))
    assert not observed["issues"]
    plan = TravelPlan.model_validate(observed["plan"])

    def forbidden_enrichment(*args, **kwargs):
        raise AssertionError("frozen-source business replay must not call online/model enrichment")

    monkeypatch.setattr(itinerary_generation.itinerary_enricher, "enrich_itinerary", forbidden_enrichment)
    monkeypatch.setattr(complete_travel, "plan_complete_travel", lambda request: (project(plan), []))
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload={},
        body_override={
            "city": plan.target_city,
            "originCity": plan.start_city,
            "days": len(plan.itinerary),
            "persons": plan.people_number,
            "startDate": None,
            "stayNights": max(len(plan.itinerary) - 1, 0),
            "requirementsStruct": {"factSource": "chinatravel", "travelPlanner": "cp"},
            "budget": None,
        },
    )
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    refreshed = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert detail == refreshed and detail["genState"] == "COMPLETED"
    restored = TravelPlan.model_validate(
        {
            **detail["dayList"][0]["travelHeader"],
            "itinerary": [{"day": day["dayNo"], "activities": day["travelActivities"]} for day in detail["dayList"]],
        }
    )
    assert restored.official() == plan.official()
    assert all(day["relativeDays"] and day["travelDate"] is None for day in detail["dayList"])
    assert sum(Decimal(str(v["amount"])) for v in detail["budgetList"]) == plan.total_cost()
    save(
        OUTPUT / f"business-readback/{uid}.json",
        {
            "measurement": "actual NL-to-CP result replay into API/worker/SQLite; no new model or route calls",
            "source": str(path.relative_to(OUTPUT)),
            "canonical_output": restored.official(),
            "detail": detail,
            "refresh_equal": True,
            "total_cost": str(plan.total_cost()),
            "human_review": "not performed",
        },
    )


def test_business_full_frozen_provider_cp_without_new_model_calls(client, monkeypatch):
    import json

    from app.agent.generation.content import day_prompts
    from app.services import itinerary_generation
    from scripts.chinatravel_original_data import OUTPUT, save

    intake_file = OUTPUT / "intake-v3/e20241028160248698752.json"
    if not intake_file.exists():
        pytest.skip("local real NL intake artifact unavailable")
    observed = json.loads(intake_file.read_text("utf8"))
    state = IntakeState.model_validate(observed["response"]["state"])
    state.requirements.travel_planner = "cp"

    def forbidden(*args, **kwargs):
        raise AssertionError("free frozen-provider CP business check cannot call a model or online enrichment")

    monkeypatch.setattr(day_prompts, "get_llm_client", forbidden)
    monkeypatch.setattr(itinerary_generation.itinerary_enricher, "enrich_itinerary", forbidden)
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))
    # Keep the actual run_plan_context, WorldEnv provider, compiler, solver, persistence and detail reader.
    response = client.post(
        "/api/itinerary/generate",
        json={
            "city": state.city,
            "originCity": state.origin_city,
            "days": state.days,
            "persons": state.persons,
            "budget": state.budget,
            "startDate": None,
            "intent": observed["original_text"],
            "requirementsStruct": state.requirements.model_dump(mode="json", by_alias=True),
        },
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["id"]
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert detail["genState"] == "COMPLETED"
    assert detail == client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert all(day["travelDate"] is None and day["travelActivities"] for day in detail["dayList"])
    save(
        OUTPUT / "business-full-source-free-cp.json",
        {
            "measurement": "real NL intake reused; actual API/worker/WorldEnv/CP/SQLite/refresh, new model calls=0",
            "source_intake": str(intake_file.relative_to(OUTPUT)),
            "detail": detail,
            "human_review": "not performed",
        },
    )
