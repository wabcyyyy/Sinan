"""新增真实边界输出复用到业务；不重发视觉或实体请求。"""

import json

import pytest

from app.agent.editing import screenshot as vision
from app.api.business.image_intent import router as image_router
from tests import test_screenshot_real_replay as replay
from tests.screenshot_cases import ROOT
from tests.test_constraint_business import _submit

pytest_plugins = ("tests.test_m1b_requirements_pipeline",)
RECORDED = json.loads((ROOT / "night_vision_20261025.json").read_text("utf-8"))


def test_updated_booking_needs_no_end_date_correction(client, monkeypatch, tmp_path):
    booking = next(row for row in RECORDED["results"] if row["case"] == "booking")
    monkeypatch.setattr(replay, "LIVE", {"results": [{"fixture": "booking", "extraction": booking["extraction"]}]})
    replay.test_real_response_user_confirmation_and_persisted_result(client, monkeypatch, tmp_path, "booking")
    trace = json.loads((tmp_path / "p3-real-response-booking-flow.json").read_text("utf-8"))
    assert trace["manualCorrections"] == {}


@pytest.mark.parametrize("name", ["ambiguous", "blurred"])
def test_unknown_visual_fields_remain_unknown_at_api(client, monkeypatch, name):
    row = next(row for row in RECORDED["results"] if row["case"] == name)

    class Recorded:
        model, base_url = "deepseek-flash", "https://api.deepseek.com"

        def chat(self, *_args, **_kwargs):
            return json.dumps(row["extraction"])

    monkeypatch.setattr(vision, "get_llm_client", lambda _role: Recorded())
    client.app.include_router(image_router)
    response = client.post(
        "/api/image-intent/document",
        files={"file": (f"{name}.png", (ROOT / f"night-boundaries/{name}.png").read_bytes())},
        data={"kind": "guide" if name == "ambiguous" else "booking"},
    )
    assert response.status_code == 200, response.text
    document = response.json()["data"]
    fields = {f["key"]: f["value"] for e in document["entries"] for f in e["fields"]}
    assert document["questions"] and not (set(fields) & {"date", "endDate", "timeZone", "currency"})
    if name == "ambiguous":
        assert fields["name"] == "Louvre" and fields["amount"] == "25"
    else:
        assert "name" not in fields
        rejected = client.post(
            "/api/image-intent/resolve",
            json={
                "documentId": document["documentId"],
                "recordId": "1",
                "name": "",
                "city": "Paris",
                "countryCode": "FR",
            },
        )
        assert rejected.status_code == 400  # 项目将请求字段校验错误统一映射为400。


@pytest.mark.parametrize("name", ["guide", "booking"])
def test_import_then_manual_move_keeps_required_and_booking_boundaries(client, monkeypatch, name):
    if name == "booking":
        booking = next(row for row in RECORDED["results"] if row["case"] == "booking")
        monkeypatch.setattr(replay, "LIVE", {"results": [{"fixture": name, "extraction": booking["extraction"]}]})
    client.app.include_router(image_router)
    _, adopted, _ = replay.adopt(client, monkeypatch, name)
    req = adopted["requirements"]
    # 手写控制器确认14–18到达日，给非固定的必去安排留下可移动空间。
    req["dayWindows"] = [{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "18:00"}]
    identifier, _ = _submit(
        client,
        monkeypatch,
        context_payload={"candidates": []},
        body_override={"city": "Paris", "days": 1, "persons": 1, "requirementsStruct": req},
    )
    before = client.get(f"/api/itinerary/{identifier}").json()["data"]
    target = before["dayList"][0]["items"][0]
    compared = client.post(
        f"/api/itinerary/{identifier}/replans/compare",
        json={
            "planningRevision": before["planningRevision"],
            "change": {"kind": "move", "itemId": target["id"], "dayNo": 1, "startTime": "16:00"},
        },
    )
    assert client.get(f"/api/itinerary/{identifier}").json()["data"] == before
    if name == "booking":
        assert compared.status_code == 200, compared.text
        rejected = compared.json()["data"]
        assert rejected["options"] == [] and rejected["conflicts"]
        assert all(a["status"] == "INFEASIBLE" for a in rejected["attempts"])
        return
    assert compared.status_code == 200 and compared.json()["data"]["options"], compared.text
    comparison = compared.json()["data"]
    selected = client.post(
        f"/api/itinerary/{identifier}/replans/select",
        json={
            "planningRevision": before["planningRevision"],
            "comparisonId": comparison["comparisonId"],
            "choice": comparison["options"][0]["key"],
        },
    )
    assert selected.status_code == 200, selected.text
    action = selected.json()["data"]
    applied = client.post(
        f"/api/itinerary/{identifier}/apply-plans",
        json={
            "actionMessageId": action["messageId"],
            "baseRevision": action["baseRevision"],
        },
    )
    assert applied.status_code == 200, applied.text
    after = applied.json()["data"]
    assert after["dayList"][0]["items"][0]["startTime"][:5] == "16:00"
    assert after["requirementsStruct"]["requiredPlaces"] == before["requirementsStruct"]["requiredPlaces"]
    assert after["requirementsStruct"]["placeInputs"] == before["requirementsStruct"]["placeInputs"]
    assert client.get(f"/api/itinerary/{identifier}").json()["data"]["dayList"] == after["dayList"]
