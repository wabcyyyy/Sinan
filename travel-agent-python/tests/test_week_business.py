"""Actual NL artifact to the shared API/worker/SQLite path; never performs a model call."""

import json
import time
from decimal import Decimal

import pytest

from app.schemas.travel_plan import TravelPlan
from app.schemas.trip_requirements import IntakeState
from scripts.chinatravel_human import OUTPUT
from scripts.chinatravel_original_data import save

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def test_actual_week_nl_worker_persistence_and_refresh(client, monkeypatch):
    from app.agent.generation.content import day_prompts
    from app.services import itinerary_generation

    path = OUTPUT / "week7-intake.json"
    if not path.exists():
        pytest.skip("separately authorized actual NL artifact is not available")
    observed = json.loads(path.read_text("utf8"))
    state = IntakeState.model_validate(observed["state"])
    assert state.days == 7 and state.persons == 2 and state.stay_nights == 6
    state.requirements.travel_planner = "cp"

    def forbidden(*args, **kwargs):
        raise AssertionError("weekly proof cannot call another model or online enrichment")

    monkeypatch.setattr(day_prompts, "get_llm_client", forbidden)
    monkeypatch.setattr(itinerary_generation.itinerary_enricher, "enrich_itinerary", forbidden)
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))
    response = client.post(
        "/api/itinerary/generate",
        json={
            "city": state.city,
            "originCity": state.origin_city,
            "days": state.days,
            "persons": state.persons,
            "budget": state.budget,
            "stayNights": state.stay_nights,
            "startDate": None,
            "intent": observed["original_text"],
            "requirementsStruct": state.requirements.model_dump(mode="json", by_alias=True),
        },
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["id"]
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    save(OUTPUT / f"week7-business-attempt-{time.time_ns()}.json", {"detail": detail, "source_intake": str(path)})
    assert detail["genState"] == "COMPLETED", detail
    refresh = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert detail == refresh
    assert [d["dayNo"] for d in detail["dayList"]] == list(range(1, 8))
    assert all(d["relativeDays"] and d["travelDate"] is None for d in detail["dayList"])
    assert all(not d["travelValidationIssues"] for d in detail["dayList"])
    plan = TravelPlan.model_validate(
        {
            **detail["dayList"][0]["travelHeader"],
            "itinerary": [{"day": d["dayNo"], "activities": d["travelActivities"]} for d in detail["dayList"]],
        }
    )
    activities = [a for d in plan.official()["itinerary"] for a in d["activities"]]
    hotels = [a for a in activities if a["type"] == "accommodation"]
    assert len(hotels) == 6 and all(a["rooms"] == 1 and a["room_type"] == 2 for a in hotels)
    assert all(
        any(a["type"] in {"attraction", "lunch", "dinner"} for a in d["activities"])
        for d in plan.official()["itinerary"][1:-1]
    )
    last = plan.official()["itinerary"][-1]["activities"][-1]
    assert last["type"] == "train" and last["end"].startswith("上海") and last["tickets"] == 2
    assert plan.total_cost() <= Decimal(str(state.budget))
    assert sum(Decimal(str(v["amount"])) for v in detail["budgetList"]) == plan.total_cost()
    save(
        OUTPUT / "week7-proof.json",
        {
            "passed": True,
            "measurement": "actual model NL, shared API/worker/WorldEnv/CP/SQLite/refresh",
            "anonymous_self_made": True,
            "frozen_WorldEnv": True,
            "online_trip_or_adoption": False,
            "worker_scheduler": "synchronous test executor; production worker function retained",
            "days": 7,
            "nights": 6,
            "refresh_equal": True,
            "new_model_calls_in_this_test": 0,
            "human_review": "not performed",
            "boundary_day_policy": "arrival/departure may be transport-only; interior days contain activity/meal",
            "total_cost": str(plan.total_cost()),
            "detail": detail,
        },
    )
