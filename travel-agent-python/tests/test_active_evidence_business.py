"""真实 API/plan_days/SQLite/P2 草稿确认；只有研究与取证来源冻结。"""

import json

from app.agent.generation.orchestration import evidence_tools
from tests.active_evidence_cases import environment, observation
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def test_create_refresh_replan_select_apply_preserves_facts_cas_and_locks(client, monkeypatch, tmp_path):
    request, prepared, _, truth = environment({"hidden_truth": {"forward_route": 15, "closed": None, "price_b": 20}})
    context = {"candidates": [p.raw for p in prepared]}
    phase = ["create"]

    def query(gap, problems, candidates, req, options):
        assert {p.fact.source_entity_id for p in candidates} == {"entity-A", "entity-B"}
        obs = observation(gap, candidates, truth)
        if phase[0] == "replan" and gap.kind == "price" and gap.names == ("A",):
            obs.value = 15
        return obs

    monkeypatch.setattr(evidence_tools, "query_evidence", query)
    assert request.requirements_struct is not None
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload=context,
        body_override={
            "days": 1,
            "persons": 1,
            "budget": 100,
            "requirementsStruct": request.requirements_struct.model_dump(mode="json", by_alias=True),
        },
    )
    before = client.get(f"/api/itinerary/{identifier}").json()["data"]
    active = before["dayList"][0]["constraintPlanning"]["activeEvidence"]
    assert active["queries"] and active["spentUnits"] <= 2
    assert {p["poiName"] for d in before["dayList"] for p in d["items"]} == {"A", "B"}
    phase[0] = "replan"
    response = client.post(
        f"/api/itinerary/{identifier}/replans/compare",
        json={
            "planningRevision": before["planningRevision"],
            "completedItemIds": [],
            "change": {"kind": "finish_by", "dayNo": 1, "endTime": "13:45"},
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()["data"]
    assert result["activeEvidence"] and result["options"], result
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == before
    selected = client.post(
        f"/api/itinerary/{identifier}/replans/select",
        json={
            "planningRevision": before["planningRevision"],
            "comparisonId": result["comparisonId"],
            "choice": result["options"][0]["key"],
        },
    )
    assert selected.status_code == 200, selected.text
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == before
    action = selected.json()["data"]
    applied = client.post(
        f"/api/itinerary/{identifier}/apply-plans",
        json={"actionMessageId": action["messageId"], "baseRevision": action["baseRevision"]},
    )
    assert applied.status_code == 200, applied.text
    after = applied.json()["data"]
    assert after["planningRevision"] == before["planningRevision"] + 1
    assert after["dayList"][0]["constraintPlanning"]["activeEvidence"] == result["activeEvidence"]
    item_a = next(p for d in after["dayList"] for p in d["items"] if p["poiName"] == "A")
    assert item_a["cost"] == 15
    assert item_a["factEvidence"]["cost"]["source_url"] == "https://example.com/frozen-p4"
    assert item_a["startTime"].startswith("09:00") and item_a["endTime"].startswith("11:00")
    repeated = client.post(
        f"/api/itinerary/{identifier}/replans/compare",
        json={
            "planningRevision": after["planningRevision"],
            "change": {"kind": "finish_by", "dayNo": 1, "endTime": "13:45"},
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["data"]["activeEvidence"]["queries"]
    stale = client.post(
        f"/api/itinerary/{identifier}/replans/compare",
        json={
            "planningRevision": before["planningRevision"],
            "change": {"kind": "finish_by", "dayNo": 1, "endTime": "13:45"},
        },
    )
    assert stale.status_code == 409
    (tmp_path / "p4-business-flow.json").write_text(
        json.dumps(
            {
                "method": (
                    "real API/SQLite/worker planning + P2 compare/select/CAS; "
                    "frozen research/evidence, zero model/network"
                ),
                "before": before,
                "comparison": result,
                "after": after,
                "stale_status": stale.status_code,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
