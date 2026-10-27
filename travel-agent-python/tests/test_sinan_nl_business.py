"""Replay this round's actual parsed state through API/worker/SQLite; no new model call."""

import copy
import json
import os
from pathlib import Path

import pytest

from app.agent.generation.orchestration import constraint_planning as planner
from scripts.eval_sinan_nl import context_for
from scripts.run_p4_acceptance import save
from scripts.sinan_nl_check import schedule_checks
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def _root():
    value = os.environ.get("SINAN_NL_BUSINESS_ROOT")
    if not value:
        pytest.skip("explicit local experiment output root required")
    return Path(value)


def _body(state):
    return {
        "city": state["city"],
        "days": state["days"],
        "persons": state["persons"],
        "budget": state["budget"],
        "startDate": state["start_date"],
        "stayNights": 0,
        "requirementsStruct": state["requirements"],
    }


def _routes(monkeypatch, case):
    matrix = {
        (a, b): {"duration_min": duration, "source": "benchmark-synthetic", "mode": "walking"}
        for a, b, duration in case["facts"]["routes"]
    }
    monkeypatch.setattr(planner, "get_route_matrix", lambda *_a, **_kw: matrix)


def _schedule(detail):
    return [
        {
            "day_no": d["dayNo"],
            "items": [{"id": p["poiId"], "start": p["startTime"][:5], "end": p["endTime"][:5]} for p in d["items"]],
        }
        for d in detail["dayList"]
    ]


def test_real_nl_output_creates_and_refreshes_through_business(client, monkeypatch):
    root = _root()
    case = json.loads((root / "design.json").read_text("utf8"))["rows"][0]
    observed = json.loads((root / "dev-calibrated2/ct-poi-00-C.json").read_text("utf8"))
    assert observed["evaluation"]["passed"]
    state = observed["result"]["final"]["state"]
    _routes(monkeypatch, case)
    identifier, _ = _submit(client, monkeypatch, context_payload=context_for(case), body_override=_body(state))
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    refresh = client.get(f"/api/itinerary/{identifier}").json()["data"]
    check = schedule_checks(case, case["gold"], _schedule(refresh))
    assert detail == refresh and not check["errors"]
    assert refresh["genState"] == "COMPLETED"
    assert refresh["dayList"][0]["constraintPlanning"]["status"] == "UNKNOWN"  # synthetic routes remain explicit
    save(
        root / "business-create.json",
        {
            "mode": "actual NL output replay; frozen facts; API/worker/SQLite",
            "new_model_calls": 0,
            "new_external_requests": 0,
            "detail": refresh,
            "independent_check": check,
        },
    )


def test_real_conflict_output_revision_recovery_preserves_other_requirements(client, monkeypatch):
    root = _root()
    # The predeclared cycle failed intake in C and stays a failed diagnostic. Use the
    # other predeclared actual conflict for API consumption, without changing its score.
    case = json.loads((root / "diagnostic-design.json").read_text("utf8"))["rows"][1]
    observed = json.loads((root / "results/diag-01-C.json").read_text("utf8"))
    assert observed["evaluation"]["status"] == "INFEASIBLE"
    state = observed["result"]["final"]["state"]
    _routes(monkeypatch, case)
    identifier, _ = _submit(client, monkeypatch, context_payload=context_for(case), body_override=_body(state))
    old = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assessment = old["dayList"][0]["constraintPlanning"]
    assert assessment["status"] == "INFEASIBLE"
    target = state["requirements"]["required_places"][0]
    suggestion = next(s for s in assessment["repairSuggestions"] if s["constraintId"] == target["constraint_id"])
    endpoint = f"/api/itinerary/{identifier}/constraint-repairs"
    body = {
        "planningRevision": old["planningRevision"],
        "constraintId": suggestion["constraintId"],
        "action": "remove_required",
    }
    assert client.post(endpoint, json={**body, "planningRevision": old["planningRevision"] + 1}).status_code == 409
    revised_response = client.post(endpoint, json=body)
    assert revised_response.status_code == 200, revised_response.text
    revised = revised_response.json()["data"]["requirements"]
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == old
    new_id, _ = _submit(
        client,
        monkeypatch,
        context_payload=context_for(case),
        body_override={**_body(state), "requirementsStruct": revised},
    )
    final = client.get(f"/api/itinerary/{new_id}").json()["data"]
    expected = copy.deepcopy(case["gold"])
    expected["requirements"]["required_places"] = []
    check = schedule_checks(case, expected, _schedule(final))
    assert not check["errors"] and final["genState"] == "COMPLETED"
    for field in ("dayWindows", "budgetPolicy", "excludedPlaces", "fixedArrangements", "placeRelations"):
        assert revised.get(field) == old["requirementsStruct"].get(field)
    save(
        root / "business-revision.json",
        {
            "mode": "scripted confirmation; actual NL output replay; API/worker/SQLite",
            "new_model_calls": 0,
            "new_external_requests": 0,
            "old_unchanged": True,
            "rejected_stale_revision": True,
            "before": old,
            "selected": suggestion,
            "after": final,
            "independent_check": check,
            "human_review": "not performed",
        },
    )
