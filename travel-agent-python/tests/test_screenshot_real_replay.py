"""复用已经付费取得的四份真实输出；不重发模型请求，外部地点使用真实观测回放。"""

import json

import pytest

from app.agent.editing import screenshot as vision
from app.api.business.image_intent import router as image_router
from app.api.business.preparation import router as preparation_router
from app.schemas.trip_requirements import TripRequirements
from app.services import screenshot
from tests.screenshot_cases import ROOT, kyoto_entity, requirements, resolved_entity, station_entity
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)
LIVE = json.loads((ROOT / "real_vision_20261024.json").read_text(encoding="utf-8-sig"))


def adopt(client, monkeypatch, name):
    original = next(row["extraction"] for row in LIVE["results"] if row["fixture"] == name)

    class RecordedClient:
        model = "deepseek-flash"
        base_url = "https://api.deepseek.com"

        def chat(self, _messages, **_kwargs):
            return json.dumps(original)

    monkeypatch.setattr(vision, "get_llm_client", lambda _role: RecordedClient())
    japanese = name in {"guide_kyoto", "transport"}
    entity = station_entity() if name == "transport" else kyoto_entity() if japanese else resolved_entity()
    monkeypatch.setattr(screenshot, "resolve_screenshot_place", lambda *_args: [entity])
    doc_response = client.post(
        "/api/image-intent/document",
        files={"file": (f"{name}.png", (ROOT / f"{name}.png").read_bytes())},
        data={"kind": "guide" if name.startswith("guide") else "booking"},
    )
    assert doc_response.status_code == 200, doc_response.text
    doc = doc_response.json()["data"]
    fields = {f["key"]: f["value"] for f in doc["entries"][0]["fields"]}
    city, country = ("京都", "JP") if japanese else ("Paris", "FR")
    resolved = client.post(
        "/api/image-intent/resolve",
        json={
            "documentId": doc["documentId"],
            "recordId": "1",
            "name": fields["name"],
            "city": city,
            "countryCode": country,
        },
    ).json()["data"]
    req = requirements().model_dump(mode="json", by_alias=True)
    if japanese:
        req["constraintPlanning"] = {"countryCode": "JP", "timeZone": "Asia/Tokyo", "currency": "JPY"}
    req["dayWindows"] = [{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "16:00"}]
    body = {
        "documentId": doc["documentId"],
        "recordId": "1",
        "resolutionId": resolved["resolutionId"],
        "entityId": entity.entity_id,
        "requirements": req,
        "days": 1,
        "startDate": "2026-10-25",
        "disposition": "booking" if name == "transport" else "fixed" if name == "booking" else "required",
        "localDate": fields.get("date"),
        "endDate": fields.get("endDate"),
        "startTime": fields.get("startTime"),
        "endTime": fields.get("endTime"),
        "timeZone": fields.get("timeZone"),
        "amount": fields.get("amount"),
        "currency": fields.get("currency"),
        "overridePolarity": True,
    }
    corrections = {}
    if name == "booking" and "endDate" not in fields:
        assert any("结束当地日期" in q for q in doc["entries"][0]["questions"])
        rejected = client.post("/api/image-intent/confirm", json=body)
        assert rejected.status_code == 422 and "日期" in rejected.json()["message"]
        # 显式模拟使用者对照原图录入；不将开始日期自动复制为结束日期。
        body["endDate"] = "2026-10-25"
        corrections["endDate"] = "2026-10-25 (operator checked original sample)"
    adopted = client.post("/api/image-intent/confirm", json=body)
    assert adopted.status_code == 200, adopted.text
    return doc, adopted.json()["data"], corrections


@pytest.mark.parametrize("name", ["guide", "booking", "guide_kyoto", "transport"])
def test_real_response_user_confirmation_and_persisted_result(client, monkeypatch, tmp_path, name):
    client.app.include_router(image_router)
    client.app.include_router(preparation_router)
    doc, adopted, corrections = adopt(client, monkeypatch, name)
    if name == "transport":
        _guide_doc, guide, _ = adopt(client, monkeypatch, "guide_kyoto")
        req = guide["requirements"]
    else:
        req = adopted["requirements"]
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload={"candidates": []},
        body_override={
            "city": "京都" if name in {"guide_kyoto", "transport"} else "Paris",
            "days": 1,
            "persons": 1,
            "requirementsStruct": req,
        },
    )
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    day = detail["dayList"][0]
    assert day["constraintPlanning"]["numericalStatus"] in {"FEASIBLE", "OPTIMAL"}, detail
    assert day["constraintPlanning"]["status"] == "UNKNOWN"
    assert len(day["items"]) == 1
    item = day["items"][0]
    assert "14:00" <= item["startTime"][:5] < item["endTime"][:5] <= "16:00"
    if name == "booking":
        assert item["startTime"][:5] == "14:00" and item["endTime"][:5] == "16:00"
        assert item["costCurrency"] == "EUR" and item["durationMin"] == 120
    if name == "guide_kyoto":
        assert detail["requirementsStruct"]["placeInputs"][0]["localName"] == "清水寺"
    preparation = None
    if adopted["booking"]:
        old = client.get(f"/api/itinerary/{identifier}/preparation").json()["data"]
        old["data"]["bookings"].append(adopted["booking"])
        response = client.put(
            f"/api/itinerary/{identifier}/preparation", json={"revision": old["revision"], "data": old["data"]}
        )
        assert response.status_code == 200, response.text
        preparation = response.json()["data"]
        if name == "transport":
            booking = preparation["data"]["bookings"][0]
            assert booking["startAt"] == "2026-10-25T23:00:00+09:00"
            assert booking["endAt"] == "2026-10-26T06:00:00+09:00"
            assert booking["originalCurrency"] == "JPY" and booking["originalAmount"] == "1200"
        assert client.get(f"/api/itinerary/{identifier}").json()["data"] == detail
    TripRequirements.model_validate(req)
    (tmp_path / f"p3-real-response-{name}-flow.json").write_text(
        json.dumps(
            {
                "method": (
                    "Previously obtained real vision outputs replayed through real API/SQLite/plan_days/solver; "
                    "zero additional model requests"
                ),
                "document": doc,
                "manualCorrections": corrections,
                "adopted": adopted,
                "detail": detail,
                "preparation": preparation,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
