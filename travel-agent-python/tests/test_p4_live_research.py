"""本轮真实研究的费用/范围边界；测试不出网。"""

import json
from decimal import Decimal

import httpx
import pytest

from scripts.p4_acceptance_budget import AcceptanceBudget, model_upper
from scripts.run_p4_live_research import LIMITS, OFFICIAL, LiveRun, candidate


def test_explicit_experiment_budget_preserves_old_default_and_bound(tmp_path):
    assert model_upper(4096) * 6 + Decimal("0.03") == Decimal("12.80952") < 15 < 30
    plan = {"requests": [{"id": "fetch-1", "kind": "fetch", "path": "/v1/tools/fetch", "payload": {"url": OFFICIAL}}]}
    req = httpx.Request("POST", "https://api.deepseek.com/v1/tools/fetch", json={"url": OFFICIAL})
    old = AcceptanceBudget(plan, tmp_path / "old.json", approved=True)
    with pytest.raises(ValueError, match="类型"):
        old.before(req)
    guard = AcceptanceBudget(plan, tmp_path / "new.json", approved=True, hard_budget=Decimal("15"), kind_limits=LIMITS)
    guard.before(req)
    assert guard.reserved == Decimal("0.005")
    with pytest.raises(ValueError):
        guard.before(req)


def test_live_transport_scope_is_blocked_before_network_and_captures_billing(tmp_path):
    issued = []
    run = LiveRun(tmp_path)

    def respond(req):
        issued.append(req.url.path)
        return httpx.Response(200, json={"title": "官方公告", "content": "真实响应的替代，仅测试"})

    with httpx.Client(
        transport=httpx.MockTransport(respond), event_hooks={"request": [run.before], "response": [run.after]}
    ) as client:
        response = client.post("https://api.deepseek.com/v1/tools/fetch", json={"url": OFFICIAL})
        assert response.status_code == 200
        assert run.responses["fetch-1"]["peak_price_upper_cny"] == "0.005"
        with pytest.raises(ValueError):
            client.post("https://example.com/v1/tools/fetch", json={"url": OFFICIAL})
        with pytest.raises(ValueError):
            client.post("https://api.deepseek.com/v1/tools/search", json={"query": "私人资料"})
    assert len(issued) == 1 and len(run.guard.records) == 1
    assert "Authorization" not in json.dumps(run.responses)


def test_missing_or_conditional_price_is_not_free():
    assert "cost" not in candidate([])
    row = {
        "url": OFFICIAL,
        "title": "中国丝绸博物馆",
        "retrieved_at": "2026-10-24T08:00:00Z",
        "content": "中国丝绸博物馆部分展厅限时免费开放",
    }
    assert "cost" not in candidate([row])
    row["content"] = "中国丝绸博物馆免费开放"
    assert candidate([row])["cost"] == 0


def test_insufficient_research_stays_degraded_after_bounded_refinement():
    from app.agent.research.agent_state import UnifiedAgentState
    from app.agent.research.evidence import ResearchTask
    from app.agent.research.factory import _finalize_pack

    state = UnifiedAgentState(
        task=ResearchTask(domain="attraction", city="杭州", limit=1),
        items=[{"name": "中国丝绸博物馆"}],
        round=2,
        sufficient=False,
        quota_note="研究额度已用完",
    )
    pack = _finalize_pack(state)["pack"]
    assert pack.degraded and any("证据不足" in gap for gap in pack.gaps)
    state.sufficient = True
    assert not _finalize_pack(state)["pack"].degraded


def test_single_requested_candidate_does_not_trigger_three_candidate_supplement(monkeypatch):
    from app.agent.data import web_search
    from app.agent.research import factory
    from app.agent.research.agent_state import UnifiedAgentState
    from app.agent.research.evidence import ResearchTask

    monkeypatch.setattr(factory.registry, "invoke", lambda *_a: [{"name": "中国丝绸博物馆"}])
    monkeypatch.setattr(web_search, "web_search_enabled", lambda: True)

    def forbidden(*_a, **_k):
        raise AssertionError("已经满足 limit=1，不应为凑三条而额外补查")

    monkeypatch.setattr(web_search, "search_places_via_web", forbidden)
    state = UnifiedAgentState(task=ResearchTask(domain="attraction", city="杭州", limit=1))
    assert len(factory._run_search(state)["items"]) == 1


def test_fixed_user_time_only_removes_duration_unknown_not_price_or_opening():
    from types import SimpleNamespace

    from app.agent.generation.orchestration.constraint_planning import _candidate_checks
    from app.schemas.trip_requirements import FixedArrangement

    item = SimpleNamespace(
        fact=SimpleNamespace(candidate_id="a"),
        item=SimpleNamespace(poi_name="中国丝绸博物馆"),
        gaps=["访问时长为规划假设，尚未核实", "费用尚未核实", "营业资料尚未核实"],
    )
    fixed = [
        FixedArrangement(day_no=1, name="中国丝绸博物馆", item_type="attraction", start_time="09:00", end_time="13:00")
    ]
    assert "时长" in _candidate_checks([item], {"a"}, 1)[0].reason
    retained = _candidate_checks([item], {"a"}, 1, fixed)[0]
    assert "时长" not in retained.reason and "费用" in retained.reason and "营业" in retained.reason
    item.gaps = ["访问时长为规划假设，尚未核实"]
    assert _candidate_checks([item], {"a"}, 1, fixed) == []
