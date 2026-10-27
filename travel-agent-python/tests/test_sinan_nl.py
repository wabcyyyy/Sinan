"""The NL experiment's spending, leakage and output-checker boundaries."""

import json
import os
from pathlib import Path

import pytest

from scripts.eval_sinan_nl import execute, reference, solve_state
from scripts.sinan_nl_budget import NIGHT_UPGRADE, NLBudget
from scripts.sinan_nl_cases import public_case
from scripts.sinan_nl_check import runtime_feedback, semantics


def payload():
    return {"model": "deepseek-flash", "thinking": {"type": "disabled"}, "max_tokens": 4096}


def test_nl_budget_is_separate_persistent_and_keeps_unknown(tmp_path):
    path = tmp_path / "budget.json"
    with pytest.raises(ValueError, match="approval"):
        NLBudget(path)
    budget = NLBudget(path, approved=True)
    for number in range(4):
        budget.reserve(f"unknown-{number}", payload())
    resumed = NLBudget(path, approved=True)
    assert resumed.total() > 8 and resumed.state["carried_peak_upper_cny"] == "0"
    with pytest.raises(ValueError, match="already issued"):
        resumed.reserve("unknown-0", payload())
    with pytest.raises(ValueError, match="CNY10"):
        resumed.reserve("unknown-4", payload())
    resumed.settle("unknown-0", {"usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    resumed.reserve("later", payload())
    assert len(resumed.state["records"]) == 5
    with pytest.raises(ValueError, match="scope"):
        resumed.reserve("tool", {**payload(), "tools": []})
    data = json.loads(path.read_text("utf8"))
    data["cap_cny"] = "20"
    path.write_text(json.dumps(data), encoding="utf8")
    with pytest.raises(ValueError, match="changed"):
        NLBudget(path, approved=True)


def test_night_cap_upgrade_preserves_unknown_and_all_original_spending(tmp_path):
    path = tmp_path / "budget.json"
    original = NLBudget(path, approved=True)
    original.reserve("known", payload())
    original.settle("known", {"usage": {"prompt_tokens": 100, "completion_tokens": 50}})
    original.reserve("unknown", payload())
    state = json.loads(path.read_text("utf8"))
    before = original.total()
    state.update(cap_cny="30", approval_upgrade=NIGHT_UPGRADE)
    path.write_text(json.dumps(state), encoding="utf8")
    resumed = NLBudget(path, approved=True)
    assert resumed.total() == before and len(resumed.state["records"]) == 2
    for i in range(5):
        resumed.reserve(f"next-{i}", payload())
    assert 10 < resumed.total() < 30
    assert resumed.state["records"][1]["status"] == "issued_usage_unknown"
    assert resumed.state["records"][0]["status"] == "usage_known"


def test_runtime_feedback_does_not_receive_gold_and_semantics_catches_omission():
    case = {
        "id": "example",
        "nl": "A必须去",
        "facts": {"candidates": [], "routes": []},
        "gold": {"days": 1, "requirements": {"required_places": [{"name": "A"}]}},
    }
    view = public_case(case)
    assert set(view) == {"id", "nl", "facts"}
    output = {"state": {"days": 1, "persons": 1}, "status": "FEASIBLE", "plans": []}
    before = runtime_feedback(view, output)
    case["gold"]["requirements"]["required_places"][0]["name"] = "hidden-B"
    assert runtime_feedback(view, output) == before
    assert not semantics(case["gold"], output["state"])["hard_complete"]
    assert semantics({"budget": 1500}, {"budget": 1500.0, "requirements": {"budget_policy": None}})["recall"] == 1
    relation = {"constraint_id": "r", "kind": "before", "day_no": 1, "place_names": ["A", "B"], "strength": "hard"}
    truth = {"requirements": {"place_relations": [relation]}}
    redundant = {
        "requirements": {
            "place_relations": [relation],
            "required_places": [{"name": "A", "day_no": 1}, {"name": "B", "day_no": 1}],
        }
    }
    assert semantics(truth, redundant)["precision"] == 1
    whole_trip = {"days": 1, "requirements": {"required_places": [{"name": "A", "day_no": None}]}}
    one_day = {"days": 1, "requirements": {"required_places": [{"name": "A", "day_no": 1}]}}
    assert semantics(whole_trip, one_day)["precision"] == semantics(whole_trip, one_day)["recall"] == 1
    assert semantics({**whole_trip, "days": 2}, {**one_day, "days": 2})["recall"] < 1


def test_frozen_nl_reference_witnesses():
    location = os.environ.get("SINAN_NL_REFERENCE_ROOT")
    if not location:
        pytest.skip("explicit experiment output root required; not a CI data download")
    result = reference(Path(location))
    assert result["count"] == 30
    assert sum(result["statuses"].values()) == 30


def test_nl_real_solver_keeps_missing_facts_unknown():
    public = {
        "id": "missing",
        "nl": "A必去",
        "facts": {
            "candidates": [{"id": "cp-0", "name": "A", "duration_min": 90, "price": None, "opening": [[540, 960]]}],
            "routes": [],
        },
    }
    state = {
        "city": "杭州",
        "days": 1,
        "persons": 1,
        "budget": 100,
        "start_date": "2026-11-02",
        "requirements": {
            "required_places": [{"constraint_id": "must", "name": "A"}],
            "day_windows": [{"day_no": 1, "not_before": "09:00", "finish_by": "16:00"}],
            "budget_policy": {"mode": "hard_cap"},
        },
    }
    output = solve_state(public, state)
    assert output["status"] == "UNKNOWN"
    assert output["product_assessments"][0]["status"] == "UNKNOWN"


def test_authorized_nl_batch(monkeypatch):
    location = os.environ.get("SINAN_NL_RUN_ROOT")
    if not location:
        pytest.skip("paid experiment requires its own explicit root/approval")
    # conftest deliberately clears model credentials for offline CI. Only this explicitly
    # authorized experiment restores the configured credential, within this fixture scope.
    from app.common.config import Settings, settings

    assert os.environ.get("SINAN_NL_APPROVED") == "SINAN-NL-01"
    monkeypatch.setattr(settings, "llm_api_key", Settings().llm_api_key)
    result = execute(
        Path(location),
        os.environ["SINAN_NL_SPLIT"],
        int(os.environ["SINAN_NL_START"]),
        int(os.environ["SINAN_NL_STOP"]),
        approved=os.environ.get("SINAN_NL_APPROVED") == "SINAN-NL-01",
        phase=os.environ.get("SINAN_NL_PHASE", "initial"),
        arms=os.environ.get("SINAN_NL_ARMS", "ABC"),
    )
    assert not result.get("stopped_at_budget"), result
