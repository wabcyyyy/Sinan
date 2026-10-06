"""M0 契约单测：四个辅助端点（butler-note / poi-intros / poi-nearby / city-guide）。

覆盖三层：
1. 线级校验：非法 payload → 422，合法 payload → 200 且 wire 键与改造前一致；
2. 模型级兼容：str/list 双形态、camel/snake 双键、extra 忽略、dump 键与业务函数读取键一致；
3. §4.4.4 核对表种子：基线契约字段存在性断言（M3 加字段时在旁边补新字段断言）。
"""

from fastapi.testclient import TestClient

from app.schemas.agent_ops import ButlerNoteRequest, CityGuideRequest, PoiNearbyRequest


def _client():
    from main import app

    return TestClient(app)


# ---------- 1. 线级校验：非法 payload → 422 ----------


def test_butler_note_rejects_non_int_days():
    with _client() as client:
        r = client.post("/api/agent/v1/butler-note", json={"city": "杭州", "days": "abc"})
    assert r.status_code == 422


def test_poi_intros_rejects_non_list_names():
    with _client() as client:
        r = client.post("/api/agent/v1/poi-intros", json={"city": "杭州", "names": "不是列表"})
    assert r.status_code == 422


def test_poi_nearby_rejects_non_float_latitude():
    with _client() as client:
        r = client.post("/api/agent/v1/poi-nearby", json={"city": "杭州", "latitude": "abc"})
    assert r.status_code == 422


def test_city_guide_rejects_non_list_history():
    with _client() as client:
        r = client.post("/api/agent/v1/city-guide", json={"input": "想去玩", "history": "x"})
    assert r.status_code == 422


# ---------- 2. 合法 payload → 200 且 wire 键不变 ----------


def test_butler_note_wire_key_and_camel_request(monkeypatch):
    from app.api import agent

    captured = {}

    def fake_run_butler_note(req: dict) -> str:
        captured.update(req)
        return "你好"

    monkeypatch.setattr(agent, "run_butler_note", fake_run_butler_note)
    with _client() as client:
        r = client.post(
            "/api/agent/v1/butler-note",
            json={
                "city": "杭州",
                "hotelTier": "高端",
                "preferences": ["人文"],
            },
        )
    body = r.json()
    assert r.status_code == 200
    assert body["code"] == 200
    assert list(body["data"].keys()) == ["note"]
    assert body["data"]["note"] == "你好"
    # camelCase wire 键经 WireModel 收下后，dump 成业务函数读取的 snake_case 键
    assert captured["hotel_tier"] == "高端"
    assert captured["city"] == "杭州"


def test_butler_note_degrades_to_empty_note_on_error(monkeypatch):
    from app.api import agent

    def _fail(_req: dict) -> str:
        raise RuntimeError("offline")

    monkeypatch.setattr(agent, "run_butler_note", _fail)
    with _client() as client:
        r = client.post("/api/agent/v1/butler-note", json={"city": "杭州"})
    assert r.status_code == 200
    assert r.json()["data"] == {"note": ""}


def test_poi_intros_wire_key(monkeypatch):
    from app.api import agent

    captured = {}
    monkeypatch.setattr(
        agent,
        "run_poi_intros",
        lambda city, names, intent=None: captured.update(city=city, names=names, intent=intent) or {"西湖": "介绍"},
    )
    with _client() as client:
        r = client.post("/api/agent/v1/poi-intros", json={"city": "杭州", "names": ["西湖", ""]})
    body = r.json()
    assert r.status_code == 200
    assert list(body["data"].keys()) == ["intros"]
    assert body["data"]["intros"] == {"西湖": "介绍"}
    # 空名沿用历史行为被过滤；M3-② 起 intent 透传（未传时为 None）
    assert captured == {"city": "杭州", "names": ["西湖"], "intent": None}


def test_poi_intros_degrades_to_empty_on_error(monkeypatch):
    from app.api import agent

    def _fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(agent, "run_poi_intros", _fail)
    with _client() as client:
        r = client.post("/api/agent/v1/poi-intros", json={"city": "杭州", "names": ["西湖"]})
    assert r.status_code == 200
    assert r.json()["data"] == {"intros": {}}


def test_poi_nearby_keeps_distance_m_wire_key_and_extra_fields(monkeypatch):
    from app.api import agent

    captured = {}

    def fake_find(*_args, **kwargs):
        captured.update(kwargs)
        return [
            {
                "id": 2,
                "name": "苏堤",
                "category": "attraction",
                "rating": 4.7,
                "address": "杭州",
                "latitude": 30.241,
                "longitude": 120.151,
                "_distance_m": 130,
            }
        ]

    monkeypatch.setattr(agent, "find_nearby_pois", fake_find)
    with _client() as client:
        r = client.post("/api/agent/v1/poi-nearby", json={"city": "杭州", "name": "西湖", "limit": 5, "radiusM": 1000})
    body = r.json()
    assert r.status_code == 200
    assert list(body["data"].keys()) == ["items"]
    item = body["data"]["items"][0]
    # Java @JsonProperty 依赖 _distance_m 键；未知键（id/经纬度）保留
    assert item["_distance_m"] == 130
    assert item["name"] == "苏堤"
    assert item["latitude"] == 30.241
    # 参数换算与改造前一致：radiusM（camel）收下；name/limit 原值
    assert captured["name"] == "西湖"
    assert captured["limit"] == 5
    assert captured["radius_m"] == 1000
    assert captured["latitude"] is None


def test_poi_nearby_degrades_to_empty_on_error(monkeypatch):
    from app.api import agent

    def _fail(*_args, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(agent, "find_nearby_pois", _fail)
    with _client() as client:
        r = client.post("/api/agent/v1/poi-nearby", json={"city": "杭州"})
    assert r.status_code == 200
    assert r.json()["data"] == {"items": []}


def test_city_guide_wire_keys(monkeypatch):
    from app.api import agent

    captured = {}

    def fake_run_city_guide(req: dict) -> dict:
        captured.update(req)
        return {
            "kind": "city",
            "city": "杭州",
            "message": "好选择",
            "suggestions": [{"name": "绍兴", "reason": "水乡"}],
        }

    monkeypatch.setattr(agent, "run_city_guide", fake_run_city_guide)
    with _client() as client:
        r = client.post(
            "/api/agent/v1/city-guide", json={"input": "想去江南", "history": [{"role": "user", "content": "嗨"}]}
        )
    body = r.json()
    assert r.status_code == 200
    assert set(body["data"].keys()) == {"kind", "city", "message", "suggestions"}
    assert body["data"]["city"] == "杭州"
    assert body["data"]["suggestions"][0]["name"] == "绍兴"
    # dump(by_alias=True)：业务函数读到的键是 "input"（而非字段名 user_input）
    assert captured["input"] == "想去江南"
    assert captured["history"] == [{"role": "user", "content": "嗨"}]


def test_city_guide_degrades_on_error(monkeypatch):
    from app.api import agent

    def _fail(_req: dict) -> dict:
        raise RuntimeError("offline")

    monkeypatch.setattr(agent, "run_city_guide", _fail)
    with _client() as client:
        r = client.post("/api/agent/v1/city-guide", json={"input": "想去江南"})
    assert r.status_code == 200
    body = r.json()
    assert body["code"] == 500
    assert body["message"] == "城市引导服务暂不可用"


# ---------- 3. 模型级兼容性（不起 HTTP） ----------


def test_butler_note_accepts_preferences_str_and_list():
    assert ButlerNoteRequest(preferences="人文历史").preferences == "人文历史"
    assert ButlerNoteRequest(preferences=["人文历史"]).preferences == ["人文历史"]


def test_butler_note_ignores_unknown_future_keys():
    m = ButlerNoteRequest.model_validate({"city": "杭州", "unknown_future_key": 1})
    assert m.city == "杭州"
    assert "unknown_future_key" not in m.model_dump()


def test_poi_nearby_accepts_camel_and_snake_keys():
    assert PoiNearbyRequest.model_validate({"city": "杭州", "radiusM": 1000}).radius_m == 1000
    assert PoiNearbyRequest.model_validate({"city": "杭州", "radius_m": 1000}).radius_m == 1000


def test_butler_note_dump_keys_match_business_reads():
    """model_dump() 输出 snake 键，与 run_butler_note 的 req.get(...) 键逐一对齐。"""
    m = ButlerNoteRequest.model_validate(
        {"city": "杭州", "hotelTier": "高端", "regionHint": "浙江", "validationLog": ["x"]}
    )
    dumped = m.model_dump()
    for key in ("city", "hotel_tier", "region_hint", "validation_log"):
        assert key in dumped


def test_city_guide_dump_by_alias_uses_input_key():
    m = CityGuideRequest.model_validate({"input": "想去江南", "history": [{"role": "user", "content": "嗨"}]})
    dumped = m.model_dump(by_alias=True)
    assert "input" in dumped and "user_input" not in dumped
    assert dumped["input"] == "想去江南"
    assert dumped["history"] == [{"role": "user", "content": "嗨"}]


# ---------- 4. §4.4.4 核对表种子：基线契约字段存在 ----------


def test_daily_plan_baseline_contract_fields():
    from app.schemas.trip import DailyPlan

    assert "theme" in DailyPlan.model_fields
    assert "backup_plan" in DailyPlan.model_fields
    assert "photo_spots" in DailyPlan.model_fields
    assert "practical_notes" in DailyPlan.model_fields


def test_trip_item_baseline_contract_fields():
    from app.schemas.trip import TripItem

    assert "source" in TripItem.model_fields
    assert "verification_status" in TripItem.model_fields
