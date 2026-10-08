"""M5-b 迁移验证：apply-plans 与 hotel-option（草稿落库链路的门禁）。

这一段最值钱的三条断言：
1. **客户端传的 `plans` 必须被忽略**——只认 `actionMessageId` 指向的服务端草稿；
   这条一旦"顺手改成信任 body"，就等于把「AI 建议 → 用户确认」的审计链关掉；
2. 四道 409 阶梯（缺版本 / 已失效 / 被更新建议取代 / 行程已变化且草稿被消费）；
3. 应用是**整份替换**：草稿没点名的行程项软删、天数收缩则尾部日软删、日元数据整份覆盖。
"""

from __future__ import annotations

import json
from datetime import date, time
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    ItineraryChatMessage,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    ItineraryVersion,
    SysUser,
)
from app.services import itinerary_chat, user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"
PASSWORD = "example123"
HOTEL_NAME = "杭州国际酒店"
ROOM_NAME = "豪华大床房"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm5b.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
        trip = ItineraryMain(
            user_id=1,
            title="杭州2日游",
            city="杭州",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 21),
            days=2,
            persons=2,
            budget=Decimal("3000.00"),
            status=2,
        )
        session.add(trip)
        session.flush()
        first = ItineraryDay(
            itinerary_id=trip.id,
            day_no=1,
            travel_date=date(2026, 4, 20),
            city="杭州",
            note="旧备注",
            metadata_json='{"theme":"旧主题","backupPlan":[{"name":"下雨就去博物馆"}]}',
            generation_status="SUCCEEDED",
        )
        second = ItineraryDay(
            itinerary_id=trip.id, day_no=2, travel_date=date(2026, 4, 21), city="杭州", generation_status="SUCCEEDED"
        )
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


def _item_ids(trip_id: int) -> dict[str, int]:
    with db_session.session_scope() as session:
        rows = (
            session.execute(
                select(ItineraryItem)
                .execution_options(include_deleted=True)
                .where(ItineraryItem.itinerary_id == trip_id)
            )
            .scalars()
            .all()
        )
        return {row.poi_name: row.id for row in rows}


def _draft(trip_id: int, plans: list[dict], *, hotel_options: list[dict] | None = None) -> tuple[int, str]:
    """写一条 AI 待用草稿，`_baseRevision` 用当前行程指纹（与 Java 的写入方式同形）。"""
    revision = itinerary_chat.plan_revision(itinerary_chat.current_plans(trip_id))
    rows = [{**plan, "_baseRevision": revision} for plan in plans]
    options = [{**option, "baseRevision": revision} for option in (hotel_options or [])]
    with db_session.session_scope() as session:
        message = ItineraryChatMessage(
            itinerary_id=trip_id,
            user_id=1,
            role="ai",
            content="建议",
            changed=1,
            plans_json=json.dumps(rows, ensure_ascii=False),
            hotel_options_json=json.dumps(options, ensure_ascii=False),
        )
        session.add(message)
        session.flush()
        return message.id, revision


def test_apply_draft_never_promotes_database_item_id_to_external_poi_id(client):
    trip_id = _trip_id(client)
    plans = jsonable_encoder(itinerary_chat.current_plans(trip_id))
    before = {item["id"]: item.get("poi_id") for plan in plans for item in plan["items"]}
    message_id, revision = _draft(trip_id, plans)
    response = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
    )
    assert response.status_code == 200, response.text
    after = {item["id"]: item["poiId"] for day in response.json()["data"]["dayList"] for item in day["items"]}
    assert after == before


def _live_items(day_no: int) -> list[str]:
    with db_session.session_scope() as session:
        day = session.execute(select(ItineraryDay).where(ItineraryDay.day_no == day_no)).scalar_one()
        return [
            item.poi_name
            for item in session.execute(
                select(ItineraryItem).where(ItineraryItem.day_id == day.id).order_by(ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        ]


def _deleted_names(trip_id: int) -> list[str]:
    with db_session.session_scope() as session:
        return [
            item.poi_name
            for item in session.execute(
                select(ItineraryItem)
                .execution_options(include_deleted=True)
                .where(ItineraryItem.itinerary_id == trip_id, ItineraryItem.deleted == 1)
            )
            .scalars()
            .all()
        ]


def _operations(trip_id: int) -> list[str]:
    with db_session.session_scope() as session:
        return [
            row.operation
            for row in session.execute(
                select(ItineraryVersion)
                .where(ItineraryVersion.itinerary_id == trip_id)
                .order_by(ItineraryVersion.version_no)
            )
            .scalars()
            .all()
        ]


# ---------- apply-plans ----------


def test_apply_plans_replaces_items_keeps_identity_and_soft_deletes_rest(client: TestClient) -> None:
    trip_id = _trip_id(client)
    xihu = _item_ids(trip_id)["西湖"]
    plans = [
        {
            "day_no": 1,
            "note": "湖山线",
            "theme": "西湖晨游",
            "items": [
                {"id": xihu, "item_type": "attraction", "poi_name": "西湖", "start_time": "09:00"},
                {"item_type": "attraction", "poi_name": "灵隐寺", "cost": 45},
            ],
        },
        {"day_no": 2, "items": [{"item_type": "hotel", "poi_name": HOTEL_NAME, "cost": 880}]},
    ]
    message_id, revision = _draft(trip_id, plans)

    body = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
    ).json()
    assert body["code"] == 200, body
    assert _live_items(1) == ["西湖", "灵隐寺"] and _live_items(2) == [HOTEL_NAME]
    # 草稿没点名的项软删（含第二天原来的老旅馆）
    assert sorted(_deleted_names(trip_id)) == ["杭州老旅馆", "楼外楼"]
    assert _item_ids(trip_id)["西湖"] == xihu, "带 id 的项是原地更新，不是删了重建"

    with db_session.session_scope() as session:
        day1 = session.execute(select(ItineraryDay).where(ItineraryDay.day_no == 1)).scalar_one()
        # 元数据整份覆盖：旧主题与旧备选都要消失
        assert day1.metadata_json is not None
        assert json.loads(day1.metadata_json) == {"theme": "西湖晨游"}
        assert day1.note == "湖山线"
        lingyin = session.execute(select(ItineraryItem).where(ItineraryItem.poi_name == "灵隐寺")).scalar_one()
        # 语料库退役：成本随草稿落地，来源字段如实落默认（无权威库背书）
        assert lingyin.cost == Decimal("45.00") and lingyin.verification_status == "unverified"
        hotel = session.execute(select(ItineraryItem).where(ItineraryItem.poi_name == HOTEL_NAME)).scalar_one()
        assert hotel.item_type == "hotel" and hotel.cost == Decimal("880.00")
        assert (hotel.freshness_status, hotel.review_requirement) == ("unknown", "before_departure")
    assert _operations(trip_id) == ["apply_plans", "apply_plans"]
    history = client.get(f"/api/itinerary/{trip_id}/chat-history").json()["data"]
    assert history[0]["plans"] == [] and history[0]["changed"] is False


def test_body_plans_are_ignored_and_server_draft_wins(client: TestClient) -> None:
    """apply 接口只认 actionMessageId 指向的服务端草稿（Java 门面的有意丢弃）。

    若哪天有人把它改成信任 body，这条断言会立刻红：客户端就能借「应用草稿」写入
    任何 AI 没建议过的点位，审计链也就此断开。
    """
    trip_id = _trip_id(client)
    message_id, revision = _draft(
        trip_id, [{"day_no": 1, "items": [{"item_type": "attraction", "poi_name": "灵隐寺"}]}]
    )
    forged = [{"day_no": 1, "items": [{"item_type": "transport", "poi_name": "客户端伪造接驳"}]}]

    body = client.post(
        f"/api/itinerary/{trip_id}/apply-plans",
        json={"plans": forged, "actionMessageId": message_id, "baseRevision": revision},
    ).json()
    assert body["code"] == 200, body
    assert _live_items(1) == ["灵隐寺"]
    assert "客户端伪造接驳" not in _deleted_names(trip_id) + _live_items(1)


def test_apply_plans_drops_missing_days_and_rewrites_trip_shape(client: TestClient) -> None:
    trip_id = _trip_id(client)
    message_id, revision = _draft(trip_id, [{"day_no": 1, "items": []}])
    client.post(f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision})
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        days = (
            session.execute(
                select(ItineraryDay).execution_options(include_deleted=True).where(ItineraryDay.itinerary_id == trip_id)
            )
            .scalars()
            .all()
        )
        assert main.days == 1 and main.title == "杭州1日游"
        assert str(main.end_date) == "2026-04-20"
        assert {day.day_no: day.deleted for day in days} == {1: 0, 2: 1}, "尾部日期软删"


def test_create_missing_days_when_plan_is_longer(client: TestClient) -> None:
    trip_id = _trip_id(client)
    plans = [{"day_no": no, "items": []} for no in (1, 2, 3)]
    message_id, revision = _draft(trip_id, plans)
    client.post(f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision})
    with db_session.session_scope() as session:
        created = session.execute(select(ItineraryDay).where(ItineraryDay.day_no == 3)).scalar_one()
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert created.note == "宽松安排" and created.city == "杭州"
        assert str(created.travel_date) == "2026-04-22", "起始日 + (dayNo-1)"
        assert main.days == 3 and str(main.end_date) == "2026-04-22"


def test_revision_ladder_returns_the_same_409_wording(client: TestClient) -> None:
    trip_id = _trip_id(client)
    plans = [{"day_no": 1, "items": []}]
    first_id, _first_rev = _draft(trip_id, plans)
    second_id, _second_rev = _draft(trip_id, plans)

    missing = client.post(f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": None}).json()
    assert missing["code"] == 409 and missing["message"] == "该方案缺少版本信息，请重新生成后再应用"

    gone = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": 999999, "baseRevision": "x"}
    ).json()
    assert gone["code"] == 409 and gone["message"] == "该方案已失效，请使用最新建议"

    superseded = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": first_id, "baseRevision": "x"}
    ).json()
    assert superseded["message"] == "该方案已被更新的建议取代，请使用最新方案"

    stale = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": second_id, "baseRevision": "stale-revision"}
    ).json()
    assert stale["code"] == 409 and stale["message"] == "行程已发生变化，该方案已失效，请重新生成建议"
    with db_session.session_scope() as session:
        stale_attempt = session.get(ItineraryChatMessage, second_id)
        superseded_row = session.get(ItineraryChatMessage, first_id)
        assert stale_attempt is not None and superseded_row is not None
    # Java 的 requirePendingAction 在同一个 @Transactional 里「先消费、再抛 409」，
    # 回滚把消费也一起撤掉 → 草稿仍在，直到下一次成功写入才被失效。迁移保持同语义。
    assert stale_attempt.plans_json != "[]" and superseded_row.plans_json != "[]"


@pytest.mark.parametrize(
    ("plans", "expected"),
    [
        ([{"day_no": no, "items": []} for no in range(1, 9)], "行程草稿必须包含 1 到 7 个完整日期，未应用任何修改"),
        ([{"day_no": 1, "items": []}, {"day_no": 1, "items": []}], "行程草稿日期重复或越界，未应用任何修改"),
        (
            [{"day_no": 1, "items": [{"item_type": "spa", "poi_name": "汤屋"}]}],
            "行程项名称或类型不合法，未应用任何修改",
        ),
        (
            [{"day_no": 1, "items": [{"item_type": "attraction", "poi_name": "西湖", "cost": "30"}]}],
            "行程项费用不合法，未应用任何修改",
        ),
        (
            [{"day_no": 1, "items": [{"item_type": "attraction", "poi_name": "西湖", "duration_min": 2000}]}],
            "行程项时长不合法，未应用任何修改",
        ),
        (
            [{"day_no": 1, "items": [{"item_type": "attraction", "poi_name": "西湖", "start_time": "9:30"}]}],
            "时间格式不合法，未应用任何修改",
        ),
        (
            [{"day_no": 1, "items": [{"item_type": "attraction", "poi_name": "西湖"}] * 21}],
            "单日行程项数量或格式不合法，未应用任何修改",
        ),
    ],
)
def test_plan_validation_rejects_before_writing_anything(client: TestClient, plans, expected) -> None:
    trip_id = _trip_id(client)
    message_id, revision = _draft(trip_id, plans)
    body = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
    ).json()
    assert body["code"] == 400 and body["message"] == expected
    assert _live_items(1) == ["西湖", "楼外楼"], "校验失败必须什么都没写"


def test_item_id_that_does_not_match_name_aborts_the_whole_apply(client: TestClient) -> None:
    trip_id = _trip_id(client)
    lou = _item_ids(trip_id)["楼外楼"]
    plans = [{"day_no": 1, "items": [{"id": lou, "item_type": "attraction", "poi_name": "西湖"}]}]
    message_id, revision = _draft(trip_id, plans)
    body = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
    ).json()
    assert body["code"] == 400 and body["message"] == "行程项身份校验失败，未应用任何修改"
    assert _live_items(1) == ["西湖", "楼外楼"]


def test_apply_plans_accepts_places_beyond_the_retired_corpus(client: TestClient) -> None:
    """语料库退役后草稿即信任边界：不再做「候选 POI 城市归属」拦截。

    （旧口径：不在 poi_knowledge 里的点名会 400；该守卫随 V4 迁移一并退役。）
    """
    trip_id = _trip_id(client)
    message_id, revision = _draft(trip_id, [{"day_no": 1, "items": [{"item_type": "attraction", "poi_name": "外滩"}]}])
    body = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
    ).json()
    assert body["code"] == 200, body
    assert _live_items(1) == ["外滩"]

    message_id, revision = _draft(
        trip_id, [{"day_no": 1, "items": [{"item_type": "transport", "poi_name": "杭州东站接驳"}]}]
    )
    assert (
        client.post(
            f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
        ).json()["code"]
        == 200
    )
    assert "杭州东站接驳" in _live_items(1)


def test_apply_requires_owned_itinerary(client: TestClient) -> None:
    body = client.post("/api/itinerary/999999/apply-plans", json={"actionMessageId": 1, "baseRevision": "x"}).json()
    assert body["code"] == 404 and body["message"] == "行程不存在"


# ---------- hotel-option ----------


def _hotel_draft(trip_id: int, room_name: str = ROOM_NAME, base_price: float = 900) -> tuple[int, str]:
    """候选卡片即事实源（语料库退役）：房价/房型/描述全部随卡片走。"""
    options = [
        {
            "id": "H-1",
            "hotelName": HOTEL_NAME,
            "tier": "豪华型",
            "basePrice": 880,
            "roomTypes": [{"roomName": room_name, "basePrice": base_price, "description": "含双早"}],
        }
    ]
    return _draft(trip_id, [], hotel_options=options)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"roomType": ROOM_NAME, "dayNos": [1]}, "酒店名称不能为空"),
        ({"hotelName": HOTEL_NAME, "dayNos": [1]}, "请选择房型"),
        ({"hotelName": HOTEL_NAME, "roomType": ROOM_NAME, "dayNos": []}, "请选择具体入住晚次"),
    ],
)
def test_hotel_option_bean_validation_messages(client: TestClient, payload, expected) -> None:
    trip_id = _trip_id(client)
    _hotel_draft(trip_id)
    body = client.post(f"/api/itinerary/{trip_id}/hotel-option", json=payload).json()
    assert body["code"] == 400 and body["message"] == expected


def test_hotel_option_prices_by_room_type_season_and_writes_remark(client: TestClient) -> None:
    trip_id = _trip_id(client)
    message_id, revision = _hotel_draft(trip_id)

    body = client.post(
        f"/api/itinerary/{trip_id}/hotel-option",
        json={
            "hotelName": HOTEL_NAME,
            "roomType": ROOM_NAME,
            "tier": "豪华型",
            "dayNos": [1, 2],
            "actionMessageId": message_id,
            "baseRevision": revision,
        },
    ).json()
    assert body["code"] == 200, body
    # 4 月是平季、系数 1；两晚各写一条酒店项
    assert body["data"]["dayList"][0]["items"][-1]["remark"].startswith(
        f"房型：{ROOM_NAME}；基准价￥900.00；按平季系数×1；"
    )
    with db_session.session_scope() as session:
        hotels = (
            session.execute(
                select(ItineraryItem).where(ItineraryItem.itinerary_id == trip_id, ItineraryItem.item_type == "hotel")
            )
            .scalars()
            .all()
        )
        # 第二天原有的老旅馆行被**原地替换**成所选酒店，不是补一条新的（同 Java）
        assert len(hotels) == 2 and {item.poi_name for item in hotels} == {HOTEL_NAME}
        assert {item.cost for item in hotels} == {Decimal("900.00")}, "4 月平季、系数 1（房价取自候选卡片）"
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.hotel_tier == "豪华型", "所选晚次覆盖全部已有酒店日 → 档次写回主表"
    assert _operations(trip_id) == ["apply_hotel", "apply_hotel"]


def test_hotel_option_rejects_choices_outside_the_draft(client: TestClient) -> None:
    trip_id = _trip_id(client)
    message_id, revision = _hotel_draft(trip_id)
    unknown_room = client.post(
        f"/api/itinerary/{trip_id}/hotel-option",
        json={
            "hotelName": HOTEL_NAME,
            "roomType": "不存在的房型",
            "dayNos": [1],
            "actionMessageId": message_id,
            "baseRevision": revision,
        },
    ).json()
    assert unknown_room["code"] == 409 and "所选酒店或房型不属于当前有效方案" in unknown_room["message"]

    wrong_day = client.post(
        f"/api/itinerary/{trip_id}/hotel-option",
        json={
            "hotelName": HOTEL_NAME,
            "roomType": ROOM_NAME,
            "dayNos": [9],
            "actionMessageId": message_id,
            "baseRevision": revision,
        },
    ).json()
    assert wrong_day["code"] == 400 and wrong_day["message"] == "选择的入住晚次不在当前行程中"

    missing_hotel = client.post(
        f"/api/itinerary/{trip_id}/hotel-option",
        json={
            "hotelName": "不存在酒店",
            "roomType": ROOM_NAME,
            "dayNos": [1],
            "actionMessageId": message_id,
            "baseRevision": revision,
        },
    ).json()
    assert missing_hotel["code"] == 404 and missing_hotel["message"] == "未找到该城市的酒店候选"


def test_hotel_option_requires_positive_price(client: TestClient) -> None:
    trip_id = _trip_id(client)
    message_id, revision = _hotel_draft(trip_id, base_price=0)
    body = client.post(
        f"/api/itinerary/{trip_id}/hotel-option",
        json={
            "hotelName": HOTEL_NAME,
            "roomType": ROOM_NAME,
            "dayNos": [1],
            "actionMessageId": message_id,
            "baseRevision": revision,
        },
    ).json()
    assert body["code"] == 400 and body["message"] == "所选房型暂无有效参考价"


# ---------- M4（spec §9.2）：缩扩天复活 / planning revision CAS ----------


def _plan_rows_from_current(trip_id: int, *, day_nos: list[int]) -> list[dict]:
    """取当前行程的指定天 plan 行（形状与 current_plans 一致），供草稿组装。"""
    plans = jsonable_encoder(itinerary_chat.current_plans(trip_id))
    return [plan for plan in plans if plan.get("day_no") in day_nos]


def test_apply_survives_shrink_then_reexpand_on_same_day_row(client):
    """2→1→2：软删行占 uk_itinerary_day_no 键位——扩天必须复活原行而非新 INSERT。

    断言：无唯一键错误；day2 行 id 不变（原行复活）；恢复日不复活旧条目/旧生成
    动作（generation_status=PENDING、action/fingerprint 清空）；默认 N-1 夜重算；
    有 PENDING 天时 main 不冒充 COMPLETED。
    """
    trip_id = _trip_id(client)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.gen_state = "COMPLETED"
        day2 = (
            session.execute(select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id, ItineraryDay.day_no == 2))
            .scalars()
            .one()
        )
        day2.generation_status = "SUCCEEDED"
        day2.generation_action_id = "day-1-2"
        day2.generation_fingerprint = "fp-old"
    original_day2_id = (
        session.execute(select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id, ItineraryDay.day_no == 2))
        .scalars()
        .one()
        .id
    )

    # 先捕获两天 plan 行（缩天后 current_plans 只剩 day1，扩天草稿要用原始两天形状）；
    # day2 草稿只带旧酒店 id——验证"身份放行 + 不复活原行（按新条目落地）"
    plans_two = _plan_rows_from_current(trip_id, day_nos=[1, 2])
    old_hotel_id = _item_ids(trip_id)["杭州老旅馆"]
    for plan in plans_two:
        if plan.get("day_no") == 2:
            plan["items"] = [it for it in plan.get("items") or [] if it.get("poi_name") == "杭州老旅馆"]
    # 2→1：收缩到只有 day1
    message_id, revision = _draft(trip_id, _plan_rows_from_current(trip_id, day_nos=[1]))
    assert (
        client.post(
            f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
        ).status_code
        == 200
    )
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.days == 1 and main.stay_nights == 0, "默认语义：1 天 = 0 夜"

    # 1→2：扩回两天——软删的 day2 原行复活
    message_id2, revision2 = _draft(trip_id, plans_two)
    response = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id2, "baseRevision": revision2}
    )
    assert response.status_code == 200, response.text
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.days == 2 and main.end_date == date(2026, 4, 21)
        assert main.stay_nights == 1, "默认语义：2 天 = 1 夜（可重算）"
        rows = (
            session.execute(
                select(ItineraryDay)
                .execution_options(include_deleted=True)
                .where(ItineraryDay.itinerary_id == trip_id, ItineraryDay.day_no == 2)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1, "没有唯一键冲突的重复行：复活在原 (itinerary_id, day_no) 上"
        revived = rows[0]
        assert revived.id == original_day2_id, "day2 行是原行复活，不是新 INSERT"
        assert revived.deleted == 0
        assert revived.generation_status == "PENDING", "恢复日不复活旧生成结果"
        assert revived.generation_action_id is None and revived.generation_fingerprint is None, "恢复日不复活旧生成动作"
        revived_day_items = (
            session.execute(select(ItineraryItem).where(ItineraryItem.day_id == revived.id, ItineraryItem.deleted == 0))
            .scalars()
            .all()
        )
        assert [item.poi_name for item in revived_day_items] == ["杭州老旅馆"], "草稿明确带的条目正常落地"
        assert all(item.id != old_hotel_id for item in revived_day_items), "旧条目原行不复活：按新条目落地"
        old_hotel_row = session.execute(
            select(ItineraryItem).execution_options(include_deleted=True).where(ItineraryItem.id == old_hotel_id)
        ).scalar_one()
        assert old_hotel_row.deleted == 1, "旧软删行保持删除"
        assert main.gen_state == "PARTIAL", "有 PENDING 天时不得保持 COMPLETED"


def test_apply_cas_stale_revision_conflicts_and_increments_on_success(client):
    """两并发应用仅一个成功：A 与并发写者同读 rev N；写者先提交（N+1），A 的 CAS 409 整单不写。"""
    from app.services import itinerary_version

    trip_id = _trip_id(client)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        base_revision_at_seed = main.planning_revision
    plans_a = _plan_rows_from_current(trip_id, day_nos=[1, 2])
    revision_hash = itinerary_chat.plan_revision(itinerary_chat.current_plans(trip_id))
    rows = [
        {**plan, "_baseRevision": revision_hash, "_basePlanningRevision": base_revision_at_seed} for plan in plans_a
    ]
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=trip_id,
                user_id=1,
                role="ai",
                content="建议A",
                changed=1,
                plans_json=json.dumps(rows, ensure_ascii=False),
            )
        )
        session.flush()
        message_a = (
            session.execute(select(ItineraryChatMessage).order_by(ItineraryChatMessage.id.desc())).scalars().first()
        )
    assert message_a is not None

    # 并发写者赢下竞争：修订号先推进（模拟另一个 apply/编辑已提交）
    with db_session.session_scope() as session:
        itinerary_version.bump_planning_revision(session, trip_id)

    versions_before = _operations(trip_id)
    response = client.post(
        f"/api/itinerary/{trip_id}/apply-plans",
        json={"actionMessageId": message_a.id, "baseRevision": revision_hash},
    )
    assert response.status_code == 409, response.text
    assert "过期" in response.json()["message"]
    assert _operations(trip_id) == versions_before, "CAS 失败整事务回滚：无版本快照残留"
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.planning_revision == base_revision_at_seed + 1, "失败的 apply 不推进修订号"
        message_row = session.get(ItineraryChatMessage, message_a.id)
        assert message_row is not None and message_row.changed == 1, "草稿未被消费（整单回滚）"

    # 对照：基准匹配（草稿挂到当前修订）→ 成功并推进
    fresh_rows = [
        {**plan, "_baseRevision": revision_hash, "_basePlanningRevision": base_revision_at_seed + 1} for plan in plans_a
    ]
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=trip_id,
                user_id=1,
                role="ai",
                content="建议A2",
                changed=1,
                plans_json=json.dumps(fresh_rows, ensure_ascii=False),
            )
        )
        session.flush()
        message_a2 = (
            session.execute(select(ItineraryChatMessage).order_by(ItineraryChatMessage.id.desc())).scalars().first()
        )
    assert message_a2 is not None
    assert (
        client.post(
            f"/api/itinerary/{trip_id}/apply-plans",
            json={"actionMessageId": message_a2.id, "baseRevision": revision_hash},
        ).status_code
        == 200
    )
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.planning_revision == base_revision_at_seed + 2


def test_legacy_draft_without_planning_revision_still_hash_guarded(client):
    """存量草稿（无 _basePlanningRevision）退化 hash 比对：错 hash 仍 409，对 hash 放行并推进。"""
    trip_id = _trip_id(client)
    plans = jsonable_encoder(itinerary_chat.current_plans(trip_id))
    rows = [{**plan, "_baseRevision": "stale-hash"} for plan in plans]
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=trip_id,
                user_id=1,
                role="ai",
                content="旧草稿",
                changed=1,
                plans_json=json.dumps(rows, ensure_ascii=False),
            )
        )
        session.flush()
        legacy = (
            session.execute(select(ItineraryChatMessage).order_by(ItineraryChatMessage.id.desc())).scalars().first()
        )
    assert legacy is not None
    response = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": legacy.id, "baseRevision": "stale-hash"}
    )
    assert response.status_code == 409, "存量草稿仍受 hash 关卡保护"

    ok_draft_id, ok_revision = _draft(trip_id, plans)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        before = main.planning_revision
    assert (
        client.post(
            f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": ok_draft_id, "baseRevision": ok_revision}
        ).status_code
        == 200
    )
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.planning_revision == before + 1, "成功应用推进修订号"


def test_repeat_apply_consumed_draft_does_not_double_apply(client):
    """重复确认不重复变更：草稿已消费 → 409，版本/费用不重复产生。"""
    trip_id = _trip_id(client)
    message_id, revision = _draft(trip_id, _plan_rows_from_current(trip_id, day_nos=[1, 2]))
    first = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
    )
    assert first.status_code == 200
    operations_after_first = _operations(trip_id)
    second = client.post(
        f"/api/itinerary/{trip_id}/apply-plans", json={"actionMessageId": message_id, "baseRevision": revision}
    )
    assert second.status_code == 409, "同一草稿重复确认被拒（已消费）"
    assert _operations(trip_id) == operations_after_first, "重复确认不产生新版本"
