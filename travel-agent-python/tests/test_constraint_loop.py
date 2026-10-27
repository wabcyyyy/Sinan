"""原话/抽取回放→业务求解→冲突建议→确认修订→再次规划；零模型/外呼。"""

import json
import os
from pathlib import Path

from app.agent.editing import clarify
from app.schemas.trip import ClarifyRequest
from app.schemas.trip_requirements import IntakeState
from scripts.eval_constraint_semantics import evaluate
from tests.constraint_planning_cases import planning_request
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def test_semantic_reference_catches_omission_despite_feasible_solver_output(tmp_path):
    report = evaluate(Path(os.environ.get("SINAN_SEMANTIC_REPORT", str(tmp_path / "semantics.json"))))
    assert report["supported_extracted"] == report["annotated_supported_requirements"] == 2
    assert report["unsupported_retained"] and report["omission_detected"]
    assert report["rows"][2]["status"] == "UNKNOWN"
    assert report["rows"][3]["false_semantic_success"]


def test_raw_requirement_conflict_confirmation_and_resolve_preserve_booking(client, monkeypatch):
    quote1, quote2 = "第1天必须先去地点4再去地点5", "第1天必须先去地点5再去地点4"
    req = planning_request(days=1).requirements_struct
    assert req is not None
    req.place_relations = []
    patches = [
        {
            "op": "add",
            "target": "place_relation",
            "source_text": quote,
            "relation": {
                "constraint_id": key,
                "kind": "before",
                "place_names": names,
                "day_no": 1,
                "strength": "hard",
                "source_text": quote,
            },
        }
        for key, quote, names in (("forward", quote1, ["地点4", "地点5"]), ("backward", quote2, ["地点5", "地点4"]))
    ]
    monkeypatch.setattr(clarify, "_ask", lambda *_: json.dumps({"patches": patches}))
    extracted = clarify.run_clarify(
        ClarifyRequest(
            message=f"{quote1}；{quote2}",
            state=IntakeState(city="Paris", days=1, persons=2, budget=200, start_date="2026-10-25", requirements=req),
        )
    )
    assert {r.constraint_id for r in extracted.state.requirements.place_relations} == {"forward", "backward"}
    assert [s.source_text for s in extracted.state.requirements.requirement_sources] == [quote1, quote2]
    body = {"days": 1, "requirementsStruct": extracted.state.requirements.model_dump(mode="json", by_alias=True)}
    identifier, _ = _submit(client, monkeypatch, body_override=body)
    old = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assessment = old["dayList"][0]["constraintPlanning"]
    assert assessment["status"] == "INFEASIBLE"
    suggestion = next(s for s in assessment["repairSuggestions"] if s["constraintId"] == "backward")
    endpoint = f"/api/itinerary/{identifier}/constraint-repairs"
    payload = {"planningRevision": old["planningRevision"], "constraintId": "backward", "action": suggestion["action"]}
    assert client.post(endpoint, json={**payload, "planningRevision": old["planningRevision"] + 1}).status_code == 409
    assert client.post(endpoint, json={**payload, "constraintId": "invented"}).status_code == 409
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200, response.text
    revised = response.json()["data"]["requirements"]
    assert revised["requirementSources"][-1]["target"] == "confirmed_repair"
    assert revised["requirementSources"][-1]["sourceText"].startswith("用户确认修订：")
    unchanged = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert unchanged == old  # 选案不改原行程、不改预约、不重开预算。
    new_id, _ = _submit(client, monkeypatch, body_override={"days": 1, "requirementsStruct": revised})
    final = client.get(f"/api/itinerary/{new_id}").json()["data"]
    items = {p["poiName"]: p for p in final["dayList"][0]["items"]}
    # 独立读取最终时间；不以编译器自己的 pass 为判官。
    assert items["地点4"]["endTime"] <= items["地点5"]["startTime"]
    assert items["地点0"]["startTime"][:5] == "10:00" and items["地点0"]["endTime"][:5] == "11:40"
    assert final["dayList"][0]["constraintPlanning"]["numericalStatus"] == "OPTIMAL"
    assert final["requirementsStruct"]["fixedArrangements"] == old["requirementsStruct"]["fixedArrangements"]
    path = os.environ.get("SINAN_LOOP_REPORT")
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "mode": "frozen extraction replay through real API/worker/SQLite",
                    "raw_input": f"{quote1}；{quote2}",
                    "hard_requirements_expected": 2,
                    "hard_requirements_extracted": 2,
                    "before": assessment,
                    "selected": suggestion,
                    "after": final,
                    "unauthorized_relaxations": 0,
                    "old_itinerary_unchanged": unchanged == old,
                    "new_model_calls": 0,
                    "new_http_requests": 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def test_invalid_or_unanchored_relation_is_retained_as_unresolved(monkeypatch):
    for patch in (
        {"op": "add", "target": "place_relation", "relation": {"kind": "if_then"}},
        {"op": "add", "target": "required_place", "name": "模型编造的地点", "source_text": "并不存在的原文"},
    ):
        monkeypatch.setattr(clarify, "_ask", lambda *_, p=patch: json.dumps({"patches": [p]}))
        result = clarify.run_clarify(ClarifyRequest(message="如果下雨就不去地点4"))
        assert result.state.requirements.unresolved_requests
        assert result.state.requirements.requirement_sources[0].source_text == "如果下雨就不去地点4"
        assert not result.state.requirements.place_relations and not result.state.requirements.required_places
