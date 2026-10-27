"""P4 重要边界：错实体/过期/未知、成本、取消及更新后重求解。"""

import pytest

from app.agent.generation.content.constraint_problem import DayPlanningResult
from app.agent.generation.content.evidence_gaps import collect_gaps, decision_signature
from app.agent.generation.content.evidence_observations import apply_observation
from app.agent.generation.orchestration import evidence_tools
from app.agent.generation.orchestration.active_evidence import drive_evidence
from app.agent.runtime.run_limits import RunLimitExceeded, TaskCancelled
from tests.active_evidence_cases import environment, observation, solve

CASE = {"hidden_truth": {"forward_route": 45, "closed": None, "price_b": 10}}


def test_equal_budget_frozen_development_and_holdout(tmp_path):
    import json

    from scripts.eval_active_evidence import evaluate

    report = evaluate()
    assert len(report["rows"]) == 54
    assert all(r["queries"] <= 2 and r["spent_units"] <= 2 for r in report["rows"])
    assert {r["strategy"] for r in report["rows"]} == {"fixed", "random", "confidence", "bottleneck"}
    (tmp_path / "p4-frozen-comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_bottleneck_queries_directed_route_then_rejects_infeasible_schedule():
    request, prepared, problems, truth = environment(CASE)
    assert request.requirements_struct is not None
    assert request.requirements_struct.constraint_planning is not None
    assert request.requirements_struct.constraint_planning.active_evidence is not None
    calls = []

    def observe(gap, current, candidates, req, options):
        calls.append(gap)
        return observation(gap, candidates, truth)

    results, report = drive_evidence(
        problems,
        prepared,
        request,
        options=request.requirements_struct.constraint_planning.active_evidence,
        solve=solve,
        observe=observe,
    )
    assert calls[0].kind == "route" and calls[0].names == ("A", "B")
    assert decision_signature(results)[0] == "INFEASIBLE"
    assert report.queries[0].decision_changed
    assert prepared[0].item.cost == 10  # 没有整项覆盖。


@pytest.mark.parametrize("invalid", ["identity", "expired", "naive_time", "wrong_direction"])
def test_invalid_evidence_cannot_upgrade_unknown(invalid):
    request, prepared, problems, truth = environment(CASE)
    assert request.requirements_struct is not None
    assert request.requirements_struct.constraint_planning is not None
    assert request.requirements_struct.constraint_planning.active_evidence is not None
    gap = next(g for g in collect_gaps(problems, solve(problems, 0.25), prepared) if g.kind == "route")
    obs = observation(gap, prepared, truth)
    assert obs.fact is not None and obs.route is not None
    if invalid == "identity":
        obs.entity_ids = ("same-name-other-branch", "other")
    elif invalid == "expired":
        obs.fact.expires_at = "2026-10-23T00:00:00Z"
    elif invalid == "naive_time":
        obs.fact.retrieved_at = "2026-10-24T08:00:00"
    else:
        obs.route["from_poi_id"], obs.route["to_poi_id"] = gap.second_id, gap.first_id
    original = dict(problems[0].route_matrix)
    assert not apply_observation(problems, prepared, gap, obs, request)[0]
    assert problems[0].route_matrix == original


def test_foreign_price_keeps_original_currency_without_conversion():
    request, prepared, problems, truth = environment(CASE)
    assert request.requirements_struct is not None
    assert request.requirements_struct.constraint_planning is not None
    assert request.requirements_struct.constraint_planning.active_evidence is not None
    gap = next(g for g in collect_gaps(problems, solve(problems, 0.25), prepared) if g.kind == "price")
    obs = observation(gap, prepared, truth)
    obs.currency = "JPY"
    assert apply_observation(problems, prepared, gap, obs, request)[0]
    assert next(c for c in problems[0].candidates if c.candidate_id == gap.first_id).currency == "JPY"
    assert solve(problems, 0.25)[0].status == "UNKNOWN"


def test_expired_snapshot_is_removed_before_probing_and_original_value_preserved():
    from app.agent.generation.content.evidence_gaps import invalidate_expired

    request, prepared, problems, truth = environment(CASE)
    assert request.requirements_struct is not None
    assert request.requirements_struct.constraint_planning is not None
    assert request.requirements_struct.constraint_planning.active_evidence is not None
    gap = next(g for g in collect_gaps(problems, solve(problems, 0.25), prepared) if g.kind == "price")
    obs = observation(gap, prepared, truth)
    assert apply_observation(problems, prepared, gap, obs, request)[0]
    target = next(p for p in prepared if p.fact.candidate_id == gap.first_id)
    target.item.fact_evidence["cost"].expires_at = "2026-10-23T00:00:00Z"
    assert invalidate_expired(problems, prepared)
    assert target.item.cost == 10
    assert next(c for c in problems[0].candidates if c.candidate_id == gap.first_id).price_minor is None


def test_unknown_probe_does_not_become_infeasible_or_zero_value():
    request, prepared, problems, _ = environment(CASE)
    assert request.requirements_struct is not None
    assert request.requirements_struct.constraint_planning is not None
    assert request.requirements_struct.constraint_planning.active_evidence is not None

    def forbidden(*args):
        raise AssertionError("未知试解不应启动取证")

    results, report = drive_evidence(
        problems,
        prepared,
        request,
        options=request.requirements_struct.constraint_planning.active_evidence,
        solve=lambda p, s: [DayPlanningResult(status="UNKNOWN", solver_status="UNKNOWN")],
        observe=forbidden,
    )
    assert results[0].status == "UNKNOWN" and report.stop_reason == "probe_unknown"
    assert not report.queries


def test_cancel_propagates_and_updated_fact_never_reuses_old_feasible_result():
    request, prepared, problems, truth = environment(CASE)
    assert request.requirements_struct is not None
    assert request.requirements_struct.constraint_planning is not None
    assert request.requirements_struct.constraint_planning.active_evidence is not None
    initial = solve(problems, 0.25)

    def cancelled(*args):
        raise TaskCancelled("用户取消")

    with pytest.raises(TaskCancelled):
        drive_evidence(
            problems,
            prepared,
            request,
            options=request.requirements_struct.constraint_planning.active_evidence,
            solve=solve,
            observe=cancelled,
            initial=initial,
            strategy="random",
        )

    def exhausted(*args):
        raise RunLimitExceeded("已用完")

    results, report = drive_evidence(
        problems,
        prepared,
        request,
        options=request.requirements_struct.constraint_planning.active_evidence,
        initial=initial,
        solve=exhausted,
        observe=lambda g, p, c, r, o: observation(g, c, truth),
        strategy="fixed",
    )
    assert results[0].status == "UNKNOWN" and not results[0].visits
    assert report.stop_reason == "time_or_run_budget" and report.queries[0].outcome == "updated"


def test_default_tool_asks_user_without_model_search(monkeypatch):
    request, prepared, problems, _ = environment(CASE)
    assert request.requirements_struct is not None
    assert request.requirements_struct.constraint_planning is not None
    assert request.requirements_struct.constraint_planning.active_evidence is not None
    gap = next(g for g in collect_gaps(problems, solve(problems, 0.25), prepared) if g.kind == "opening")
    monkeypatch.setattr(evidence_tools, "fetch_official_page", lambda url: None)

    def forbidden(*args):
        raise AssertionError("未授权来源搜索")

    monkeypatch.setattr(evidence_tools, "opening_evidence", forbidden)
    observed = evidence_tools.query_evidence(
        gap, problems, prepared, request, request.requirements_struct.constraint_planning.active_evidence
    )
    assert observed.question and observed.tool == "ask_user"


def test_replan_price_update_requires_matching_server_evidence_and_rechecks_expiry():
    from copy import deepcopy

    from app.agent.generation.content.constraint_replan_check import validate_replan_schedule
    from app.schemas.replanning import ReplanChange
    from app.schemas.trip import FactEvidence
    from tests.replanning_cases import case

    _, before, _ = case(count=1)
    before[0]["items"][0]["source_entity_id"] = "entity-A"
    after = deepcopy(before)
    fact = FactEvidence(
        source_url="https://example.com/source",
        retrieved_at="2026-10-24T08:00:00Z",
        verification_status="partially_verified",
        value_kind="observed",
    ).model_dump(mode="json")
    current = after[0]["items"][0]
    current.update(cost=20, fact_evidence={"cost": fact})
    change = ReplanChange(kind="prefer", item_id=1)
    update = {
        "itemId": 1,
        "entityId": "entity-A",
        "oldAmount": 10,
        "oldCurrency": "EUR",
        "amount": 20,
        "currency": "EUR",
        "evidence": fact,
    }
    assert validate_replan_schedule(before, after, change, [1], {})
    assert not validate_replan_schedule(before, after, change, [1], {}, fact_updates=[update])
    current["cost"] = 21
    assert validate_replan_schedule(before, after, change, [1], {}, fact_updates=[update])
    current["cost"] = 20
    fact["expires_at"] = "2026-10-23T00:00:00Z"
    assert validate_replan_schedule(before, after, change, [1], {}, fact_updates=[update])
