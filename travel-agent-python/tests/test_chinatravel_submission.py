"""Failed candidate plans must never become final official submissions."""

import json

from app.schemas.travel_plan import TravelRequirements
from app.schemas.trip import GenerateDayRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from scripts import eval_chinatravel_original as runner


def test_failed_producer_preserves_candidate_but_submits_empty(monkeypatch, tmp_path):
    uid = "fixture-submission-boundary"
    candidate = {"people_number": 1, "sentinel": "internal candidate, not delivered"}
    requirements = TripRequirements(fact_source="chinatravel", travel_constraints=TravelRequirements())
    state = IntakeState(city="杭州", origin_city="上海", days=1, persons=1, requirements=requirements)
    request = GenerateDayRequest(
        city="杭州",
        day_no=1,
        days=1,
        persons=1,
        requirements_struct=requirements,
        context={"travel_last_output": candidate},
    )
    monkeypatch.setattr(runner, "OUTPUT", tmp_path)
    monkeypatch.setattr(runner, "prepare_evaluator", lambda: tmp_path / "official")
    monkeypatch.setattr(runner, "queries", lambda: [{"uid": uid, "nature_language": "匿名fixture"}])
    monkeypatch.setattr(runner, "intake", lambda *_args: state)
    monkeypatch.setattr(runner, "request_for", lambda *_args: request)
    (tmp_path / "manifest.json").write_text(json.dumps({"dev": [uid]}), encoding="utf8")

    def failed(_request):
        raise ValueError("fixture final generation failed")

    monkeypatch.setattr(runner, "plan_complete_travel", failed)
    result = runner.one(uid, "dev-submission-guard", "C")[0]
    assert result["plan"] == {}
    assert result["business_plans"] == []
    assert result["failed_candidate"] == candidate
    assert "fixture final generation failed" in result["issues"][0]
    submitted = tmp_path / f"official/results/SinanOriginal-dev-submission-guard-C/{uid}.json"
    assert json.loads(submitted.read_text("utf8")) == {}
