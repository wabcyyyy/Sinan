"""冻结实体排序与真实 API 手工兜底，不把地理编码当营业核验。"""

import json
import os
from pathlib import Path

from app.agent.generation.content.constraint_facts import prepare_candidates
from app.agent.grounding import screenshot_entities
from app.schemas.trip_requirements import TripRequirements
from app.services import screenshot
from tests.screenshot_cases import requirements
from tests.test_screenshot import client as _screenshot_fixture
from tests.test_screenshot import upload

poi_client = _screenshot_fixture


def test_ranked_entities_and_manual_unlocated_confirmation(poi_client, monkeypatch):
    client = poi_client
    rows = [
        {
            "place_id": "osm:node:1",
            "name": "Louvre",
            "display_name": "Lyon",
            "country_code": "FR",
            "latitude": 45.7,
            "longitude": 4.8,
            "source_url": "https://www.openstreetmap.org/node/1",
            "type": "museum",
            "aliases": ["卢浮宫"],
        },
        {
            "place_id": "osm:node:2",
            "name": "Musée du Louvre",
            "display_name": "Paris 75001",
            "country_code": "FR",
            "latitude": 48.8,
            "longitude": 2.3,
            "source_url": "https://www.openstreetmap.org/node/2",
            "type": "museum",
            "aliases": ["卢浮宫"],
        },
    ]
    monkeypatch.setattr(screenshot_entities, "geocode_place_rows", lambda *a, **k: rows)
    ranked = screenshot_entities.resolve_screenshot_place("卢浮宫", "Paris", "FR")
    assert [c.entity_id for c in ranked] == ["osm:node:2", "osm:node:1"]
    assert "跨语言别名一致" in ranked[0].match_reasons and "地址包含查询城市" in ranked[0].match_reasons
    transport = [dict(rows[0], type="museum"), dict(rows[1], type="station")]
    monkeypatch.setattr(screenshot_entities, "geocode_place_rows", lambda *a, **k: transport)
    assert (
        screenshot_entities.resolve_screenshot_place("卢浮宫", "Paris", "FR", "transport")[0].entity_type == "station"
    )
    assert screenshot_entities.screenshot_entity_choices(rows, "JP") == []

    doc = upload(client)
    monkeypatch.setattr(screenshot, "resolve_screenshot_place", lambda *_: [])
    query = {
        "documentId": doc["documentId"],
        "recordId": "1",
        "name": "未收录小馆",
        "city": "Paris",
        "countryCode": "FR",
    }
    assert client.post("/api/image-intent/resolve", json=query).json()["data"]["status"] == "not_found"
    assert (
        client.post(
            "/api/image-intent/manual", json={**query, "address": "用户核对的街区地址", "latitude": 1}
        ).status_code
        == 400
    )
    manual = client.post("/api/image-intent/manual", json={**query, "address": "用户核对的街区地址"}).json()["data"]
    entity = manual["choices"][0]
    assert entity["identitySource"] == "user_provided" and entity["sourceUrl"] is None and entity["latitude"] is None
    body = {
        "documentId": doc["documentId"],
        "recordId": "1",
        "resolutionId": manual["resolutionId"],
        "entityId": entity["entityId"],
        "disposition": "candidate",
        "days": 1,
        "requirements": requirements().model_dump(mode="json", by_alias=True),
    }
    response = client.post("/api/image-intent/confirm", json=body)
    assert response.status_code == 200, response.text
    place = response.json()["data"]["requirements"]["placeInputs"][0]
    assert place["address"] == "用户核对的街区地址"
    assert place["identitySource"] == "user_provided" and place["longitude"] is None and place["sourceUrl"] is None
    assert place["evidence"]["confirmedFields"]["identitySource"] == "user_provided"
    confirmed = TripRequirements.model_validate(response.json()["data"]["requirements"])
    prepared = prepare_candidates({}, confirmed.constraint_planning, confirmed.place_inputs)
    assert prepared[0].item.address == place["address"]
    assert prepared[0].item.source == "user_provided" and prepared[0].item.latitude is None
    monkeypatch.setattr(screenshot, "resolve_screenshot_place", lambda *_: None)
    assert client.post("/api/image-intent/resolve", json=query).json()["data"]["status"] == "unavailable"
    path = os.environ.get("SINAN_P1_POI_REPORT")
    if path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "mode": "frozen rank cases and real API",
                    "checks": {
                        "alias_city_top1": True,
                        "transport_type_top1": True,
                        "country_mismatch_filtered": True,
                        "not_found_preserved": True,
                        "unavailable_preserved": True,
                        "manual_unlocated_adopted": True,
                    },
                    "manual_input": place,
                    "new_model_calls": 0,
                    "external_http": 0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
