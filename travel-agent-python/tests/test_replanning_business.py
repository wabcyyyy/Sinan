import json

from app.db.models import ItineraryChatMessage, ItineraryMain, ItineraryVersion
from app.db.session import session_scope
from app.services import itinerary_chat
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def _base(client, monkeypatch):
    identifier, _ = _submit(client, monkeypatch)
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    fixed = {(f["dayNo"], f["name"]) for f in detail["fixedArrangements"]}
    target = next((d, p) for d in detail["dayList"] for p in d["items"] if (d["dayNo"], p["poiName"]) not in fixed)
    return identifier, detail, target


def _compare(client, identifier, detail, target, **kwargs):
    _, item = target
    body = {
        "planningRevision": detail["planningRevision"],
        "completedItemIds": [],
        "optionalItemIds": [],
        "allowCrossDay": True,
        "change": {"kind": "delay", "itemId": item["id"], "delayMinutes": 10},
    }
    body.update(kwargs)
    response = client.post(f"/api/itinerary/{identifier}/replans/compare", json=body)
    assert response.status_code == 200, response.text
    return body, response.json()["data"]


def test_ui_and_natural_operations_share_compare_select_confirm_and_versions(client, monkeypatch, tmp_path):
    identifier, before, target = _base(client, monkeypatch)
    _, item = target
    parsed = client.post(
        f"/api/itinerary/{identifier}/replans/parse",
        json={
            "planningRevision": before["planningRevision"],
            "dayNo": target[0]["dayNo"],
            "itemId": item["id"],
            "message": "推迟10分钟",
        },
    )
    assert parsed.status_code == 200
    body, result = _compare(client, identifier, before, target, change=parsed.json()["data"]["change"])
    assert result["options"], result
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == before
    choice = result["options"][0]["key"]
    selected = client.post(
        f"/api/itinerary/{identifier}/replans/select",
        json={"planningRevision": before["planningRevision"], "comparisonId": result["comparisonId"], "choice": choice},
    )
    assert selected.status_code == 200, selected.text
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == before
    action = selected.json()["data"]
    response = client.post(
        f"/api/itinerary/{identifier}/apply-plans",
        json={"actionMessageId": action["messageId"], "baseRevision": action["baseRevision"]},
    )
    assert response.status_code == 200, response.text
    after = response.json()["data"]
    assert after["planningRevision"] == before["planningRevision"] + 1
    assert all(d["constraintPlanning"]["isCurrent"] for d in after["dayList"])
    assert {p["id"] for d in before["dayList"] for p in d["items"]} == {
        p["id"] for d in after["dayList"] for p in d["items"]
    }
    assert all(p["costCurrency"] == "EUR" for d in after["dayList"] for p in d["items"])
    old_fixed = before["fixedArrangements"][0]
    fixed_item = next(
        p
        for d in after["dayList"]
        if d["dayNo"] == old_fixed["dayNo"]
        for p in d["items"]
        if p["poiName"] == old_fixed["name"]
    )
    assert fixed_item["startTime"][:5] == old_fixed["startTime"]
    restored = client.post(
        f"/api/itinerary/{identifier}/versions/{after['undoVersionId']}/restore",
        json={"planningRevision": after["planningRevision"]},
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["budgetCurrency"] == "EUR"
    (tmp_path / "p2-business-flow.json").write_text(
        json.dumps(
            {
                "method": "real API/SQLite; frozen research/routes; no LLM",
                "request": body,
                "comparison": result,
                "before": before,
                "after": after,
                "restored": restored.json()["data"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_stale_compare_and_apply_do_not_overwrite_or_create_versions(client, monkeypatch):
    identifier, before, target = _base(client, monkeypatch)
    _, result = _compare(client, identifier, before, target)
    selected = client.post(
        f"/api/itinerary/{identifier}/replans/select",
        json={
            "planningRevision": before["planningRevision"],
            "comparisonId": result["comparisonId"],
            "choice": result["options"][0]["key"],
        },
    ).json()["data"]
    with session_scope() as session:
        main = session.get(ItineraryMain, identifier)
        assert main is not None
        main.planning_revision += 1
        count = len(session.query(ItineraryVersion).all())
    response = client.post(
        f"/api/itinerary/{identifier}/apply-plans",
        json={"actionMessageId": selected["messageId"], "baseRevision": selected["baseRevision"]},
    )
    assert response.status_code == 409
    with session_scope() as session:
        assert len(session.query(ItineraryVersion).all()) == count


def test_invalid_operation_and_legacy_move_do_not_write(client, monkeypatch):
    identifier, detail, target = _base(client, monkeypatch)
    response = client.post(
        f"/api/itinerary/{identifier}/replans/compare",
        json={"planningRevision": detail["planningRevision"], "change": {"kind": "close", "itemId": 999999}},
    )
    assert response.status_code == 400
    response = client.post(
        f"/api/itinerary/{identifier}/items/{target[1]['id']}/action",
        json={
            "action": "move",
            "dayNo": target[0]["dayNo"],
            "planningRevision": detail["planningRevision"],
            "targetIndex": 0,
        },
    )
    assert response.status_code == 409
    for action, fields in (
        ("update", {"dayNo": target[0]["dayNo"], "startTime": "08:00", "endTime": "08:30"}),
        ("delete", {}),
    ):
        response = client.post(
            f"/api/itinerary/{identifier}/items/{target[1]['id']}/action",
            json={"action": action, "planningRevision": detail["planningRevision"], **fields},
        )
        assert response.status_code == 409
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == detail


def test_legacy_server_draft_without_replan_proof_cannot_bypass_confirmation_guard(client, monkeypatch):
    identifier, detail, _ = _base(client, monkeypatch)
    with session_scope() as session:
        plans = json.loads(itinerary_chat.canonical_json(itinerary_chat.current_plans(identifier)))
        base = itinerary_chat.plan_revision(itinerary_chat.current_plans(identifier))
        plans = itinerary_chat.attach_base_revision(
            plans, base, camel_case=False, planning_revision=detail["planningRevision"]
        )
        plans[0]["_confirmationThread"] = "p2-legacy-proof-guard"
        message = itinerary_chat.save_chat_message(session, 1, identifier, "ai", "旧草稿", plans, [], True)
        message_id = message.id
        version_count = len(session.query(ItineraryVersion).all())
    response = client.post(
        f"/api/itinerary/{identifier}/apply-plans", json={"actionMessageId": message_id, "baseRevision": base}
    )
    assert response.status_code == 409
    assert "重新比较" in response.json()["message"]
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == detail
    with session_scope() as session:
        assert len(session.query(ItineraryVersion).all()) == version_count


def test_server_draft_rechecked_including_completed_activity(client, monkeypatch):
    identifier, detail, target = _base(client, monkeypatch)
    completed = detail["dayList"][0]["items"][0]["id"]
    _, result = _compare(client, identifier, detail, target, completedItemIds=[completed])
    assert result["options"]
    selected = client.post(
        f"/api/itinerary/{identifier}/replans/select",
        json={
            "planningRevision": detail["planningRevision"],
            "comparisonId": result["comparisonId"],
            "choice": result["options"][0]["key"],
        },
    ).json()["data"]
    with session_scope() as session:
        message = session.get(ItineraryChatMessage, selected["messageId"])
        assert message is not None
        plans = itinerary_chat.read_plans(message)
        item = next(p for d in plans for p in d["items"] if p["id"] == completed)
        item["start_time"] = "23:00"
        message.plans_json = json.dumps(plans)
    response = client.post(
        f"/api/itinerary/{identifier}/apply-plans",
        json={"actionMessageId": selected["messageId"], "baseRevision": selected["baseRevision"]},
    )
    assert response.status_code in {400, 409}
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == detail


def test_time_edit_uses_same_operation_without_creating_permanent_fixed_arrangement(client, monkeypatch):
    identifier, detail, target = _base(client, monkeypatch)
    day, item = target
    end = int(item["endTime"][:2]) * 60 + int(item["endTime"][3:5]) + 10
    new_end = f"{end // 60:02d}:{end % 60:02d}"
    _, result = _compare(
        client,
        identifier,
        detail,
        target,
        change={
            "kind": "move",
            "itemId": item["id"],
            "dayNo": day["dayNo"],
            "startTime": item["startTime"][:5],
            "endTime": new_end,
        },
    )
    assert result["options"]
    selected = client.post(
        f"/api/itinerary/{identifier}/replans/select",
        json={
            "planningRevision": detail["planningRevision"],
            "comparisonId": result["comparisonId"],
            "choice": result["options"][0]["key"],
        },
    ).json()["data"]
    response = client.post(
        f"/api/itinerary/{identifier}/apply-plans",
        json={"actionMessageId": selected["messageId"], "baseRevision": selected["baseRevision"]},
    )
    assert response.status_code == 200, response.text
    after = response.json()["data"]
    updated = next(p for d in after["dayList"] for p in d["items"] if p["id"] == item["id"])
    assert updated["endTime"][:5] == new_end
    assert after["fixedArrangements"] == detail["fixedArrangements"]
