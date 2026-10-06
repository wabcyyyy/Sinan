"""M5-a 迁移验证：同进程直调 agent（nl-edit 与三个城市能力端点）。

这一段最容易丢的不是功能而是**口径**，所以断言集中在这几处：
1. nl-edit 的 5 种 op 与 Java 逐条一致，含三条**看起来像 bug 的既有行为**
   （move_day 恒为 day_no+1、delete 无 day_no 时作用于整趟、时间不合法整单 400 回滚）；
2. agent 失败的 502 文案：业务原因 → 「请求失败：<原因>」，不合法/崩溃 → 「…服务暂不可用」，
   这正是过去 Java `postForNode` 的映射，换进程不能把它换掉；
3. 附近推荐在 agent 崩溃时**静默返回空列表**而不是报错——这是迁移前就有的降级，不许"顺手修好"。
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent.editing.nl_edit import EditOp
from app.agent.grounding.existence import VERIFIED, ResolveResult
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    CityGeo,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.services import itinerary_city, itinerary_nl_edit, user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"
PASSWORD = "example123"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm5a.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    # 语料库退役：nl-edit 的外部地点解析/酒店候选默认给空（用例按需覆盖）
    monkeypatch.setattr(itinerary_nl_edit, "resolve_poi", lambda name, city: ResolveResult.unknown("stub_off"))
    monkeypatch.setattr(itinerary_nl_edit, "search_hotels", lambda city, limit=8: [])
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
        session.add(CityGeo(city_name="杭州", country="中国", country_code="CN", is_domestic=True))
        trip = ItineraryMain(
            user_id=1,
            title="杭州2日游",
            city="杭州",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 2),
            days=2,
            persons=2,
            budget=Decimal("2000.00"),
            status=2,
        )
        session.add(trip)
        session.flush()
        first = ItineraryDay(itinerary_id=trip.id, day_no=1, generation_status="SUCCEEDED")
        second = ItineraryDay(itinerary_id=trip.id, day_no=2, generation_status="SUCCEEDED")
        session.add_all([first, second])
        session.flush()
        session.add_all(
            [
                ItineraryItem(
                    day_id=first.id,
                    itinerary_id=trip.id,
                    item_type="attraction",
                    poi_name="西湖",
                    cost=Decimal("0.00"),
                    sort_no=0,
                    start_time=time(9, 30),
                ),
                ItineraryItem(
                    day_id=first.id,
                    itinerary_id=trip.id,
                    item_type="food",
                    poi_name="楼外楼",
                    cost=Decimal("100.00"),
                    sort_no=1,
                ),
                ItineraryItem(
                    day_id=second.id,
                    itinerary_id=trip.id,
                    item_type="hotel",
                    poi_name="杭州老旅馆",
                    poi_id="1",
                    cost=Decimal("200.00"),
                    sort_no=0,
                ),
            ]
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    return client


def _trip_id(client: TestClient) -> int:
    return client.get("/api/itinerary").json()["data"][0]["id"]


def _ops(monkeypatch, *ops: EditOp) -> None:
    monkeypatch.setattr(itinerary_nl_edit, "run_edit_ops", lambda _req: list(ops))


def _items(trip_id: int) -> dict[str, list[dict]]:
    with db_session.session_scope() as session:
        days = session.execute(select(ItineraryDay).order_by(ItineraryDay.day_no)).scalars().all()
        return {
            str(day.day_no): [
                {
                    "poi": item.poi_name,
                    "type": item.item_type,
                    "sort": item.sort_no,
                    "start": item.start_time,
                    "deleted": item.deleted,
                }
                for item in session.execute(
                    select(ItineraryItem)
                    .execution_options(include_deleted=True)
                    .where(ItineraryItem.day_id == day.id)
                    .order_by(ItineraryItem.sort_no)
                )
                .scalars()
                .all()
            ]
            for day in days
        }


# ---------- nl-edit：五种 op ----------


def test_nl_edit_delete_and_add_report_in_java_wording(client: TestClient, monkeypatch) -> None:
    trip_id = _trip_id(client)
    _ops(
        monkeypatch,
        EditOp(action="delete", day_no=1, poi_name="楼外楼"),
        EditOp(action="add", day_no=1, poi_name="灵隐寺", start_time="14:00"),
    )

    body = client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "删了楼外楼，下午加灵隐寺"}).json()
    assert body["data"]["applied"] == ["删除「楼外楼」", "第1天新增「灵隐寺」"]

    day1 = _items(trip_id)["1"]
    assert [row["poi"] for row in day1 if not row["deleted"]] == ["西湖", "灵隐寺"]
    assert next(row for row in day1 if row["poi"] == "楼外楼")["deleted"] == 1, "删除是软删"
    added = next(row for row in day1 if row["poi"] == "灵隐寺")
    assert added["sort"] == 1 and added["start"] == time(14, 0)


def test_add_op_resolves_external_place_and_marks_coords_verified(client: TestClient, monkeypatch) -> None:
    """外部地点层命中的点位要带真实坐标与来源，坐标按 observed、票价留白走估价。"""
    trip_id = _trip_id(client)

    def _hit(name, city):
        return ResolveResult(
            state=VERIFIED,
            provider="nominatim",
            name=name,
            external_id="N123",
            latitude=30.2407,
            longitude=120.1315,
            address="浙江省杭州市 法云弄1号",
        )

    monkeypatch.setattr(itinerary_nl_edit, "resolve_poi", _hit)
    _ops(monkeypatch, EditOp(action="add", day_no=2, poi_name="灵隐寺"))
    client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "第二天加灵隐寺"})

    with db_session.session_scope() as session:
        item = session.execute(select(ItineraryItem).where(ItineraryItem.poi_name == "灵隐寺")).scalar_one()
        assert item.item_type == "attraction" and item.cost is None
        assert float(item.latitude or 0) == 30.2407 and float(item.longitude or 0) == 120.1315
        assert item.poi_id == "N123" and item.address == "浙江省杭州市 法云弄1号"
        assert item.source == "nominatim" and item.verification_status == "partially_verified"
        assert item.value_kind == "observed" and item.review_requirement == "before_departure"


def test_add_op_without_knowledge_hit_falls_back_to_plain_attraction(client: TestClient, monkeypatch) -> None:
    trip_id = _trip_id(client)
    _ops(monkeypatch, EditOp(action="add", day_no=1, poi_name=" nonexistent 点位"))
    client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "加个不存在的点位"})
    with db_session.session_scope() as session:
        item = session.execute(select(ItineraryItem).where(ItineraryItem.poi_name == " nonexistent 点位")).scalar_one()
        assert item.item_type == "attraction" and item.cost is None and item.source_updated_at is None
        # verification_status/value_kind/freshness_status 是 NOT NULL + DDL 默认值：
        # 未命中知识库只能落默认值，写显式 NULL 在 MySQL 上直接违约
        assert (item.verification_status, item.value_kind, item.freshness_status) == (
            "unverified",
            "generated",
            "unknown",
        )


def test_move_day_lands_on_next_day_and_appends(client: TestClient, monkeypatch) -> None:
    """Java 的 move_day 恒为 day_no+1（提示词说的是「换到第几天」），此处原样保留。"""
    trip_id = _trip_id(client)
    _ops(monkeypatch, EditOp(action="move_day", day_no=1, poi_name="楼外楼"))
    client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "楼外楼挪到第二天"})
    rows = _items(trip_id)
    assert [row["poi"] for row in rows["1"] if not row["deleted"]] == ["西湖"]
    assert [row["poi"] for row in rows["2"] if not row["deleted"]] == ["杭州老旅馆", "楼外楼"]


def test_move_day_on_last_day_is_capped_and_adds_nothing(client: TestClient, monkeypatch) -> None:
    trip_id = _trip_id(client)
    _ops(monkeypatch, EditOp(action="move_day", day_no=2, poi_name="杭州老旅馆"))
    body = client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "酒店再往后一天"}).json()
    assert body["code"] == 400 and body["message"] == "未能从指令中解析出可执行的修改，请换个说法"


def test_upgrade_hotel_matches_tier_keywords(client: TestClient, monkeypatch) -> None:
    trip_id = _trip_id(client)
    monkeypatch.setattr(
        itinerary_nl_edit,
        "search_hotels",
        lambda city, limit=8: [
            {"id": "H1", "name": "杭州国际酒店", "avg_cost": 880.0, "description": "城市地标酒店", "kinds": ""},
            {"id": "H2", "name": "西湖宾馆", "avg_cost": 400.0, "description": "湖景", "kinds": ""},
        ],
    )
    _ops(monkeypatch, EditOp(action="upgrade_hotel", day_no=None, poi_name=None, tier="豪华型"))
    body = client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "酒店换成豪华点的"}).json()
    assert body["data"]["applied"] == ["酒店已调整为豪华型"]
    with db_session.session_scope() as session:
        hotel = session.execute(select(ItineraryItem).where(ItineraryItem.item_type == "hotel")).scalar_one()
        assert hotel.poi_name == "杭州国际酒店" and hotel.cost == Decimal("880.0")
        assert hotel.remark == "城市地标酒店", "档次关键词命中描述"


def test_delete_without_day_no_spans_whole_trip(client: TestClient, monkeypatch) -> None:
    trip_id = _trip_id(client)
    _ops(monkeypatch, EditOp(action="delete", day_no=None, poi_name="杭州老旅馆"))
    client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "把酒店删掉"})
    assert _items(trip_id)["2"][0]["deleted"] == 1


def test_bad_time_aborts_whole_request_and_rolls_back_applied_ops(client: TestClient, monkeypatch) -> None:
    """一条 op 的时间不合法 → 整单 400，前面已执行的 op 也要一起回滚（同 Java 的单事务）。"""
    trip_id = _trip_id(client)
    _ops(
        monkeypatch,
        EditOp(action="delete", day_no=1, poi_name="楼外楼"),
        EditOp(action="add", day_no=1, poi_name="灵隐寺", start_time="9:30"),
    )
    body = client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "改一下"}).json()
    assert body["code"] == 400 and body["message"] == "时间格式必须为 HH:mm"
    assert all(not row["deleted"] for row in _items(trip_id)["1"]), "回滚后楼外楼仍在"


def test_nl_edit_writes_two_snapshots_and_invalidates_ai_drafts(client: TestClient, monkeypatch) -> None:
    trip_id = _trip_id(client)
    with db_session.session_scope() as session:
        from app.db.models import ItineraryChatMessage

        session.add(
            ItineraryChatMessage(
                itinerary_id=trip_id,
                user_id=1,
                role="ai",
                content="建议",
                plans_json='[{"day_no":1,"_baseRevision":"abc"}]',
                changed=1,
            )
        )
    _ops(monkeypatch, EditOp(action="delete", day_no=1, poi_name="楼外楼"))
    client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "删楼外楼"})

    versions = client.get(f"/api/itinerary/{trip_id}/versions").json()["data"]
    assert [row["operation"] for row in versions] == ["nl_edit", "nl_edit"]
    history = client.get(f"/api/itinerary/{trip_id}/chat-history").json()["data"]
    assert history[0]["plans"] == [] and history[0]["changed"] is False


# ---------- agent 失败的 502 口径 ----------


def test_agent_business_failure_keeps_request_failed_prefix(client: TestClient, monkeypatch) -> None:
    def refuse(_req):
        raise ValueError("没能理解这条修改指令，请换种说法")

    monkeypatch.setattr(itinerary_nl_edit, "run_edit_ops", refuse)
    body = client.post(f"/api/itinerary/{_trip_id(client)}/nl-edit", json={"instruction": "随便改改"}).json()
    assert body["code"] == 502 and body["message"] == "请求失败：没能理解这条修改指令，请换种说法"


def test_agent_unparseable_input_maps_to_unavailable_message(client: TestClient) -> None:
    """空指令今天会被 FastAPI 打成 422 → Java 侧 502「指令解析服务暂不可用」，口径保持。"""
    body = client.post(f"/api/itinerary/{_trip_id(client)}/nl-edit", json={"instruction": ""}).json()
    assert body["code"] == 502 and body["message"] == "指令解析服务暂不可用"


def test_missing_itinerary_is_404_before_calling_the_model(client: TestClient, monkeypatch) -> None:
    called = []
    monkeypatch.setattr(itinerary_nl_edit, "run_edit_ops", lambda req: called.append(req) or [])
    assert client.post("/api/itinerary/999999/nl-edit", json={"instruction": "改"}).json()["code"] == 404
    assert called == [], "鉴权/归属失败时不该花钱调模型"


# ---------- 三个城市能力端点 ----------


def test_clarify_shape_and_empty_message_rejection(client: TestClient, monkeypatch) -> None:
    from app.schemas.trip import ClarifyResponse

    monkeypatch.setattr(
        itinerary_city,
        "run_clarify",
        lambda req: ClarifyResponse(slots={"city": "杭州"}, missing=["days"], question="去玩几天？", ready=True),
    )
    data = client.post("/api/itinerary/clarify", json={"message": "想去杭州", "slots": {}}).json()["data"]
    assert data == {
        "slots": {"city": "杭州"},
        "missing": ["days"],
        "question": "去玩几天？",
        "ready": True,
        "options": [],
        "blocked": False,
        # AILIVE-2 起 ClarifyResponse 恒带 degraded（正常轮 False，见 test_clarify_intake）
        "degraded": False,
    }

    unavailable = client.post("/api/itinerary/clarify", json={"message": ""}).json()
    assert unavailable["code"] == 502 and unavailable["message"] == "意图解析服务暂不可用"


def test_city_guide_fills_java_defaults_and_drops_nameless_suggestions(client: TestClient, monkeypatch) -> None:
    def fake_run(payload):
        captured.append(payload)
        return {
            "kind": None,
            "city": None,
            "message": "",
            "suggestions": [{"name": "杭州", "reason": None}, {"reason": "无名也保留"}],
        }

    captured: list[dict] = []
    monkeypatch.setattr(itinerary_city, "run_city_guide", fake_run)
    data = client.post(
        "/api/itinerary/city-guide", json={"input": "想看江南", "history": [{"role": "user", "content": "想看江南"}]}
    ).json()["data"]
    assert data["kind"] == "unclear" and data["message"] == "想去哪里玩？说说你的想法～"
    assert data["suggestions"] == [{"name": "杭州", "reason": ""}]
    # supported 由服务端现算（不接受客户端伪造）
    assert captured[0]["supported"] == ["杭州"] and captured[0]["input"] == "想看江南"


def test_poi_nearby_degrades_silently_and_applies_default_limit(client: TestClient, monkeypatch) -> None:
    seen: dict = {}

    def fake_nearby(city, **kwargs):
        seen["city"] = city
        seen.update(kwargs)
        return [{"name": "雷峰塔", "distance_m": 320, "latitude": 30.2}]

    monkeypatch.setattr(itinerary_city, "find_nearby_pois", fake_nearby)
    data = client.post(
        "/api/itinerary/poi-nearby", json={"city": "杭州", "name": "西湖", "limit": 0, "radiusM": 0}
    ).json()["data"]
    assert data["items"] == [
        {"name": "雷峰塔", "category": "attraction", "rating": None, "address": None, "distanceM": 320}
    ]
    assert seen["limit"] == 5 and seen["radius_m"] is None, "0 值 falsy 归默认（同迁移前）"
    assert seen["latitude"] is None

    def explode(*_a, **_k):
        raise RuntimeError("坐标解析炸了")

    monkeypatch.setattr(itinerary_city, "find_nearby_pois", explode)
    assert client.post("/api/itinerary/poi-nearby", json={"city": "杭州"}).json()["data"] == {"items": []}


def test_add_op_refuses_endorsement_for_out_of_area_resolution(client: TestClient, monkeypatch) -> None:
    """解析成功但落在别的城市：点位照样加入（用户点名要的），但不给外部背书。"""
    trip_id = _trip_id(client)

    def _elsewhere(name, city):
        return ResolveResult(
            state="unknown",
            provider="nominatim",
            name=name,
            out_of_area=True,
            latitude=31.85,
            longitude=117.22,
            reason="resolved_out_of_area",
        )

    monkeypatch.setattr(itinerary_nl_edit, "resolve_poi", _elsewhere)
    _ops(monkeypatch, EditOp(action="add", day_no=2, poi_name="方所书店"))
    client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "第二天加方所书店"})

    with db_session.session_scope() as session:
        item = session.execute(select(ItineraryItem).where(ItineraryItem.poi_name == "方所书店")).scalar_one()
        assert item.latitude is None and item.longitude is None
        assert item.verification_status == "unverified"


def test_nl_edit_rejects_failed_trip(client: TestClient, monkeypatch) -> None:
    """R3-F9（2026-10-05 拍板：拒绝编辑）：gen_state=FAILED 的行程 nl-edit 直接 409。

    旧预期为什么是错的：失败壳+可编辑体并存时，编辑先落库、恢复重拉（R2-F7 收窄
    后的增量补天）再覆盖手改——两写者互踩。拒绝入口让恢复成为失败行程唯一的内容
    来源（S6 实证：status=3 曾可 200 落库）。
    """
    trip_id = _trip_id(client)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.gen_state = "FAILED"
    _ops(monkeypatch, EditOp(action="delete", day_no=1, poi_name="楼外楼"))

    response = client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "删了楼外楼"})
    assert response.status_code == 409
    assert "行程生成失败" in response.json()["message"]
    # 内容未被改动（拒绝发生在快照与落库之前）
    assert next(row for row in _items(trip_id)["1"] if row["poi"] == "楼外楼")["deleted"] == 0

    # 旧数据回落口径：gen_state IS NULL + status=3（Java 迁移存量）同样拒绝——
    # 失败判定与 generation_recovery 的 failed 过滤器保持同一份。
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.gen_state, main.status = None, 3
    legacy = client.post(f"/api/itinerary/{trip_id}/nl-edit", json={"instruction": "再删一个"})
    assert legacy.status_code == 409
