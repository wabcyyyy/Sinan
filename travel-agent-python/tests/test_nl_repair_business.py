"""This repair round's actual model state through creation and the formerly blocked cycle."""

import copy
import json
import os
from pathlib import Path

import pytest

from scripts.eval_sinan_nl import context_for
from scripts.run_p4_acceptance import save
from scripts.sinan_nl_check import schedule_checks
from tests.test_constraint_business import _submit
from tests.test_sinan_nl_business import _body, _routes, _schedule

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def _load(base_id):
    root = os.environ.get("SINAN_NL_REPAIR_BUSINESS")
    if not root:
        pytest.skip("explicit saved model output required")
    root = Path(root)
    rows = json.loads((root / "design.json").read_text("utf8"))["regressions"]
    case = next(row for row in rows if row["base_id"] == base_id)
    observed = json.loads((root / "results" / f"{case['id']}-C.json").read_text("utf8"))
    assert observed["evaluation"]["passed"]
    return root, case, observed["result"]["final"]["state"]


def test_repaired_model_state_creates_and_refreshes(client, monkeypatch):
    root, case, state = _load("ct-poi-00")
    _routes(monkeypatch, case)
    identifier, _ = _submit(client, monkeypatch, context_payload=context_for(case), body_override=_body(state))
    first = client.get(f"/api/itinerary/{identifier}").json()["data"]
    second = client.get(f"/api/itinerary/{identifier}").json()["data"]
    check = schedule_checks(case, case["gold"], _schedule(second))
    assert first == second and not check["errors"] and second["genState"] == "COMPLETED"
    save(
        root / "business-create.json",
        {
            "mode": "actual repaired model output replay; API/worker/SQLite",
            "new_model_calls": 0,
            "refresh_equal": True,
            "detail": second,
            "checker": check,
        },
    )


def test_repaired_cycle_conflict_selection_and_resolve_preserves_other_requirements(client, monkeypatch):
    root, case, state = _load("diag-00")
    _routes(monkeypatch, case)
    identifier, _ = _submit(client, monkeypatch, context_payload=context_for(case), body_override=_body(state))
    old = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assessment = old["dayList"][0]["constraintPlanning"]
    assert assessment["status"] == "INFEASIBLE"
    names = [row["name"] for row in case["facts"]["candidates"]]
    reverse = next(r for r in state["requirements"]["place_relations"] if r["place_names"] == names[1::-1])
    suggestion = next(r for r in assessment["repairSuggestions"] if r["constraintId"] == reverse["constraint_id"])
    payload = {
        "planningRevision": old["planningRevision"],
        "constraintId": suggestion["constraintId"],
        "action": "soften_relation",
    }
    endpoint = f"/api/itinerary/{identifier}/constraint-repairs"
    assert client.post(endpoint, json={**payload, "planningRevision": old["planningRevision"] + 1}).status_code == 409
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200, response.text
    revised = response.json()["data"]["requirements"]
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == old
    next_id, _ = _submit(
        client,
        monkeypatch,
        context_payload=context_for(case),
        body_override={**_body(state), "requirementsStruct": revised},
    )
    final = client.get(f"/api/itinerary/{next_id}").json()["data"]
    expected = copy.deepcopy(case["gold"])
    expected["requirements"]["place_relations"][1]["strength"] = "soft"
    check = schedule_checks(case, expected, _schedule(final))
    assert final["genState"] == "COMPLETED" and not check["errors"]
    for field in ("requiredPlaces", "dayWindows", "budgetPolicy", "fixedArrangements", "excludedPlaces"):
        assert revised.get(field) == old["requirementsStruct"].get(field)
    save(
        root / "business-cycle-revision.json",
        {
            "mode": "scripted user confirmation; actual model state replay",
            "new_model_calls": 0,
            "old_unchanged": True,
            "stale_rejected": True,
            "before": old,
            "chosen": suggestion,
            "after": final,
            "checker": check,
            "human_review": "not performed",
        },
    )
