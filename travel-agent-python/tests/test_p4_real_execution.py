"""本轮唯一执行器的付费边界和结果判官；全部 HTTP 为 MockTransport。"""

import time
from decimal import Decimal

import httpx
import pytest

from app.schemas.trip_requirements import BudgetPolicy, DayWindow
from scripts.align_p4_scenario import local_only, research_scope
from scripts.analyze_p4_acceptance import billing
from scripts.p4_acceptance_results import frozen_reference, policy_comparison
from scripts.prepare_p4_acceptance import build_plan
from scripts.run_p4_acceptance import PaidRun, intake_request, save


@pytest.fixture
def plan(monkeypatch):
    from app.common.config import settings

    monkeypatch.setattr(settings, "llm_base_url", "https://api.deepseek.com")
    monkeypatch.setattr(settings, "llm_api_key", "example-only-p4")
    monkeypatch.setattr(settings, "llm_model", "deepseek-flash")
    for role in ("main", "fast", "search"):
        monkeypatch.setattr(settings, f"llm_role_{role}", "deepseek-flash")
    return build_plan()


def test_http_capture_retains_usage_and_blocks_duplicate_and_expired_requests(plan, tmp_path):
    issued = []

    def respond(request):
        issued.append(request.url.path)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 12, "completion_tokens": 4}})

    run = PaidRun(plan, tmp_path, approved=True)
    with httpx.Client(
        transport=httpx.MockTransport(respond), event_hooks={"request": [run.before], "response": [run.after]}
    ) as client:
        row = plan["requests"][0]
        response = client.post("https://api.deepseek.com" + row["path"], json=row["payload"])
        assert response.json()["usage"]["completion_tokens"] == 4
        assert run.responses[row["id"]]["usage"]["prompt_tokens"] == 12
        with pytest.raises(ValueError):
            client.post("https://api.deepseek.com" + row["path"], json=row["payload"])
        run.deadline = time.monotonic() - 1
        with pytest.raises(TimeoutError):
            client.post("https://api.deepseek.com/v1/tools/search", json=plan["requests"][2]["payload"])
    assert len(issued) == 1 and len(run.guard.records) == 1


def test_frozen_oracle_accepts_complete_state_and_preserves_missing_constraints_as_failures(plan, tmp_path):
    state = intake_request(plan).state
    assert state is not None
    req = state.requirements
    req.budget_policy = BudgetPolicy(mode="hard_cap")
    req.max_attractions_per_day = 1
    req.excluded_places = ["地点C"]
    req.day_windows = [DayWindow(day_no=1, kind="arrival", not_before="09:00", finish_by="13:00")]
    comparison = policy_comparison(state, {"sufficient": True})
    assert all(comparison["extracted_checks"].values())
    assert all(row["passed"] and row["selected"] == ["地点B"] for row in comparison["rows"])
    save(tmp_path / "comparison.json", comparison)
    req.budget_policy = None
    failed = policy_comparison(state, {"sufficient": None})
    assert not failed["extracted_checks"]["hard_budget"]
    assert not any(row["passed"] for row in failed["rows"])


def test_usage_estimate_is_distinct_from_reservation_and_unknown_usage_is_explicit():
    result = billing(
        {
            "M1": {"usage": {"prompt_tokens": 1406, "completion_tokens": 296}},
            "M2": {"usage": {"prompt_tokens": 256, "completion_tokens": 66}},
            "S1": {"nonempty_results": True},
            "S2": {"nonempty_results": True},
        }
    )
    assert result["all_usage_known"] and Decimal(result["usage_priced_upper_cny"]) == Decimal("0.01622")
    unknown = billing({"M1": {"usage": None}})
    assert not unknown["all_usage_known"] and unknown["rows"][0]["peak_price_upper_cny"] is None


@pytest.mark.parametrize("cause", ["exception", "expired"])
def test_opening_rejection_distinguishes_calendar_exception_from_expired_dates(cause):
    from app.agent.generation.content.evidence_gaps import collect_gaps
    from app.agent.generation.content.evidence_observations import apply_observation
    from tests.active_evidence_cases import environment, observation, solve

    request, prepared, problems, truth = environment(
        {"hidden_truth": {"forward_route": 45, "closed": None, "price_b": 10}}
    )
    gap = next(g for g in collect_gaps(problems, solve(problems, 0.25), prepared) if g.kind == "opening")
    obs = observation(gap, prepared, truth)
    assert obs.fact is not None and obs.fact.opening_hours is not None
    if cause == "exception":
        obs.fact.opening_hours.rules[0].exceptions = ["法定节假日除外"]
    else:
        from datetime import date

        obs.fact.opening_hours.valid_to = date(2026, 10, 24)
    original = problems[0]
    updated, reason = apply_observation(problems, prepared, gap, obs, request)
    assert not updated and problems[0] == original
    assert ("例外" in reason) if cause == "exception" else ("不覆盖" in reason)


@pytest.fixture
def full_day_state(plan):
    state = intake_request(plan).state
    assert state is not None
    req = state.requirements
    req.budget_policy = BudgetPolicy(mode="hard_cap")
    req.max_attractions_per_day = 1
    req.excluded_places = ["地点C"]
    req.day_windows = [DayWindow(day_no=1, kind="full", not_before="09:00", finish_by="13:00")]
    return state


def test_independent_reference_preserves_full_day_minimum_and_window(full_day_state):
    short = frozen_reference(full_day_state, 180)
    exact = frozen_reference(full_day_state, 240)
    overflow = frozen_reference(full_day_state, 241)
    assert short["day_kind"] == "full" and short["min_active_minutes"] == 240
    assert short["expected_status"] == overflow["expected_status"] == "INFEASIBLE"
    assert exact["expected_status"] == "FEASIBLE" and exact["optimal_names"] == ["地点B"]


def test_aligned_frozen_plan_passes_without_overriding_recorded_research_false(full_day_state):
    before = full_day_state.model_dump(mode="json")
    original_evaluation = {"sufficient": False, "extra_keywords": ["缺真实资料"]}
    comparison = policy_comparison(full_day_state, original_evaluation, duration_min=240)
    assert comparison["research_gate"] is False
    assert full_day_state.model_dump(mode="json") == before
    for row in comparison["rows"]:
        assert row["conditional_planning_passed"] and row["reference_agrees"] and not row["passed"]
        assert row["selected"] == ["地点B"]
        assert len(row["report"]["queries"]) == 1 and row["report"]["spent_units"] <= 2
        assert row["report"]["queries"][0]["decision_changed"]
        assert row["results"][0]["visits"] == [{"candidate_id": "cp-1", "start_min": 540, "end_min": 780}]
    scope = research_scope({"sufficient": False, "reason": "合成资料不足以验证真实景点"}, original_evaluation)
    assert not scope["real_research_gate_passed"] and not scope["frozen_scope"]["gate_override"]
    assert scope["recorded_model_sufficient"] is False and "合成资料" in scope["recorded_model_reason"]
    with pytest.raises(ValueError, match="不一致"):
        research_scope({"sufficient": True}, original_evaluation)


def test_alignment_scope_blocks_new_http_and_model_calls():
    from app.common.llm_client import LLMClient

    with local_only():
        with pytest.raises(RuntimeError, match="HTTP"):
            httpx.get("https://api.deepseek.com")
        client = LLMClient(base_url="https://api.deepseek.com", api_key="example-only-p4", model="deepseek-flash")
        with pytest.raises(RuntimeError, match="模型"):
            client.complete("must not issue")
