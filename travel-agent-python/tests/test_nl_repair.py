"""Actual NL-01 blockers: one bounded contract repair, source coverage, zero budget, cyclic order."""

import json

from app.agent.editing import clarify
from app.agent.editing.clarify_contract import extraction_issues
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements


def initial(days=2):
    return IntakeState(
        days=days,
        requirements=TripRequirements(
            constraint_planning=ConstraintPlanningOptions(country_code="CN", time_zone="Asia/Shanghai", currency="CNY")
        ),
    )


def test_coordinated_place_source_feedback_identifies_fabricated_prefix():
    message = "必须去A和B。"
    patches = [{"op": "add", "target": "required_place", "name": "B", "source_text": "必须去B"}]
    issues = extraction_issues({"patches": patches}, message, initial(1))
    assert len(issues) == 1 and "required_place/B" in issues[0] and "连续原文" in issues[0]
    patches[0]["source_text"] = "B"
    assert not extraction_issues({"patches": patches}, message, initial(1))


def valid_output(message, days=2):
    patches = [
        {"op": "set", "target": target, "day_no": day, "value": time, "source_text": "每天9点开始到16点结束"}
        for day in range(1, days + 1)
        for target, time in (("day_not_before", "09:00"), ("day_finish_by", "16:00"))
    ]
    patches.append({"op": "set", "target": "budget_policy_mode", "value": "hard_cap", "source_text": "硬上限0元"})
    return {"city": "杭州", "days": days, "persons": 1, "budget": 0, "start_date": "2026-11-02", "patches": patches}


def test_invalid_scalar_add_repairs_once_and_keeps_every_day(monkeypatch):
    message = "杭州2日，每天9点开始到16点结束，门票总额硬上限0元"
    good = valid_output(message)
    bad = json.loads(json.dumps(good))
    bad["patches"][0]["op"] = "add"
    calls = []

    class Client:
        def json_response_format(self, *_):
            return {"type": "json_object"}

        def complete(self, *_a, **_kw):
            calls.append("first")
            return json.dumps(bad)

        def chat(self, messages, **_kw):
            calls.append("repair")
            assert "不能使用add" in messages[-1]["content"]
            return json.dumps(good)

    monkeypatch.setattr(clarify, "get_llm_client", lambda: Client())
    response = clarify.run_clarify(ClarifyRequest(message=message, state=initial()))
    assert calls == ["first", "repair"]
    assert response.state.budget == 0
    assert [(w.day_no, w.not_before, w.finish_by) for w in response.state.requirements.day_windows] == [
        (1, "09:00", "16:00"),
        (2, "09:00", "16:00"),
    ]
    assert not response.state.requirements.unresolved_requests


def test_missing_uniform_days_or_zero_amount_are_detected_without_filling_gold():
    message = "每天9点开始到16点结束，门票硬上限0元"
    data = valid_output(message)
    data["patches"] = data["patches"][:2]
    data["budget"] = None
    issues = extraction_issues(data, message, initial())
    assert any("日期[2]" in issue for issue in issues)
    assert any("预算金额" in issue for issue in issues)
    assert data["budget"] is None and len(data["patches"]) == 2


def test_expressible_cycle_is_not_discarded_as_unresolved_but_conditions_stay_unknown():
    message = "第1天必须先甲再乙，但也必须先乙再甲"
    bad = {"patches": [{"op": "add", "target": "unresolved_request", "value": message}]}
    assert any("before关系" in issue for issue in extraction_issues(bad, message, initial(1)))
    condition = "如果下雨，第1天必须先甲再乙，否则必须先乙再甲"
    assert not extraction_issues(
        {"patches": [{"op": "add", "target": "unresolved_request", "value": condition}]}, condition, initial(1)
    )


def test_failed_content_repair_keeps_missing_requirements_explicit(monkeypatch):
    message = "每天9点开始到16点结束，门票硬上限0元"
    monkeypatch.setattr(clarify, "_ask", lambda *_: json.dumps({"days": 2, "budget": None, "patches": []}))
    monkeypatch.setattr(clarify, "_repair", lambda *_: None)
    response = clarify.run_clarify(ClarifyRequest(message=message, state=initial()))
    assert response.degraded and not response.ready
    assert response.state.requirements.unresolved_requests
