"""真实 API→plan_days→CP-SAT→SQLite；仅视觉与外部地点事实冻结。"""

import json

import pytest

from app.api.business.image_intent import router as image_router
from app.api.business.preparation import router as preparation_router
from app.common.envelope import ApiError
from app.db.models import ItineraryMain
from app.db.session import session_scope
from app.schemas.trip_requirements import TripRequirements
from app.services import screenshot, travel_preparation
from tests.screenshot_cases import ROOT, extraction, kyoto_entity, requirements, resolved_entity
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)


def confirmed(client, monkeypatch, kind, disposition):
    japanese = kind == "guide_kyoto"
    entity = kyoto_entity() if japanese else resolved_entity()
    city, country, name = ("京都", "JP", "Kiyomizu-dera") if japanese else ("Paris", "FR", "Musée du Louvre")
    client.app.include_router(image_router)
    client.app.include_router(preparation_router)
    monkeypatch.setattr(screenshot, "extract_screenshot", lambda _uri, _kind: (extraction(kind), "frozen-reference"))
    monkeypatch.setattr(screenshot, "resolve_screenshot_place", lambda *_args: [entity])
    doc = client.post(
        "/api/image-intent/document",
        files={"file": (f"{kind}.png", (ROOT / f"{kind}.png").read_bytes())},
        data={"kind": "guide" if kind.startswith("guide") else "booking"},
    ).json()["data"]
    result = client.post(
        "/api/image-intent/resolve",
        json={
            "documentId": doc["documentId"],
            "recordId": "1",
            "name": name,
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
        "resolutionId": result["resolutionId"],
        "entityId": entity.entity_id,
        "disposition": disposition,
        "requirements": req,
        "days": 1,
        "startDate": "2026-10-25",
        "localDate": "2026-10-25",
        "endDate": "2026-10-25",
        "startTime": "14:00",
        "endTime": "16:00",
        "timeZone": "Asia/Tokyo" if japanese else "Europe/Paris",
        "amount": "22.00",
        "currency": "JPY" if japanese else "EUR",
    }
    response = client.post("/api/image-intent/confirm", json=body)
    assert response.status_code == 200, response.text
    # 确认输入时还没有任何正式行程。
    with session_scope() as session:
        assert session.query(ItineraryMain).count() == 0
    return doc, response.json()["data"]


@pytest.mark.parametrize("kind,disposition", [("guide", "required"), ("booking", "fixed"), ("guide_kyoto", "required")])
def test_screenshot_confirmed_entity_reaches_database_with_independent_checks(
    client, monkeypatch, tmp_path, kind, disposition
):
    doc, adopted = confirmed(client, monkeypatch, kind, disposition)
    identifier, captured = _submit(
        client,
        monkeypatch,
        context_payload={"candidates": []},
        body_override={
            "city": "京都" if kind == "guide_kyoto" else "Paris",
            "days": 1,
            "persons": 1,
            "requirementsStruct": adopted["requirements"],
        },
    )
    detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
    assert len(captured) == 1 and captured[0].requirements_struct.place_inputs
    day = detail["dayList"][0]
    assert len(day["items"]) == 1, detail
    item = day["items"][0]
    assert item["poiName"] == ("Kiyomizu-dera" if kind == "guide_kyoto" else "Musée du Louvre")
    assert "14:00" <= item["startTime"][:5] < item["endTime"][:5] <= "16:00"
    if disposition == "fixed":
        assert item["startTime"][:5] == "14:00" and item["endTime"][:5] == "16:00"
        assert item["durationMin"] == 120
    else:
        assert not detail["fixedArrangements"]
    assert item["costCurrency"] == ("JPY" if kind == "guide_kyoto" else "EUR")
    report = day["constraintPlanning"]
    assert report["numericalStatus"] in ("FEASIBLE", "OPTIMAL") and report["status"] == "UNKNOWN"
    assert (
        report["facts"][0]["sourceEntityId"]
        == (kyoto_entity() if kind == "guide_kyoto" else resolved_entity()).entity_id
    )
    assert not any(check["status"] == "violation" and check["blocking"] for check in report["report"]["checks"])
    with session_scope() as session:
        main = session.get(ItineraryMain, identifier)
        assert main is not None
        saved = main.requirements_json
        assert TripRequirements.model_validate(saved).place_inputs[0].evidence.record_id == "1"
    output = {
        "method": (
            "real API/plan_days/CP-SAT/SQLite; frozen vision references and recorded OSM entity; no model/online query"
        ),
        "document": doc,
        "adopted": adopted,
        "detail": detail,
        "independent_checks": {
            "one_selected_entity": True,
            "time_within_user_window": True,
            "fixed_span_kept": disposition == "fixed",
            "facts_not_upgraded": True,
        },
    }
    (tmp_path / f"p3-{kind}-business.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def test_booking_reuses_private_preparation_cas_without_rewriting_formal_plan(client, monkeypatch):
    _doc, adopted = confirmed(client, monkeypatch, "booking", "fixed")
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload={"candidates": []},
        body_override={
            "days": 1,
            "persons": 1,
            "requirementsStruct": adopted["requirements"],
        },
    )
    before = client.get(f"/api/itinerary/{identifier}").json()["data"]
    state = client.get(f"/api/itinerary/{identifier}/preparation").json()["data"]
    state["data"]["bookings"].append(adopted["booking"])
    response = client.put(
        f"/api/itinerary/{identifier}/preparation", json={"revision": state["revision"], "data": state["data"]}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["data"]["bookings"][0]["inputEvidence"]["recordId"] == "1"
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == before
    assert (
        client.put(
            f"/api/itinerary/{identifier}/preparation", json={"revision": state["revision"], "data": state["data"]}
        ).status_code
        == 409
    )
    with pytest.raises(ApiError):
        travel_preparation.get(2, identifier)
