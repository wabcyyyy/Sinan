"""M3 读路径迁移验证：列表 / 详情 / 偏好。

重点是"形状"而不是"能返回 200"：前端按 Java 的字段名取值，
任何改名或序列化格式漂移都是静默故障。因此断言集中在
字段集合、时间格式、stayNights 回落、质量状态判定、缓存与精确失效、
以及 FastAPI 特有的**路由顺序**陷阱（/preferences 必须早于 /{id}）。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    BudgetDetail,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.services import itinerary_query

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm3.db'}")
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
    from app.services import user_service

    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        session.add_all(
            [
                SysUser(username="alice", password=hashed, nickname=None, status=1, role="user"),
                SysUser(username="mallory", password=hashed, nickname=None, status=1, role="user"),
            ]
        )
        trip = ItineraryMain(
            user_id=1,
            title="杭州3日游",
            city="杭州",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 3),
            days=3,
            persons=2,
            budget=Decimal("3000.00"),
            status=2,
            trip_theme="西湖慢行",
            stay_nights=0,
            suggestions_json='[{"x": 1}]',
        )
        other = ItineraryMain(user_id=2, title="别人的行程", city="北京", days=1, persons=1, status=2)
        gone = ItineraryMain(user_id=1, title="已删除", city="杭州", days=1, persons=1, status=2, deleted=1)
        session.add_all([trip, other, gone])
        session.flush()

        day = ItineraryDay(
            itinerary_id=trip.id,
            day_no=1,
            travel_date=date(2026, 4, 1),
            note="到达",
            generation_status="SUCCEEDED",
            metadata_json='{"theme":"湖畔","miniRoute":{"a":1},"photoSpots":[{"name":"断桥"}],'
            '"practicalNotes":["早去"],"dayOptions":[{"label":"A","summary":"s","tradeoff":"t"}]}',
        )
        session.add(day)
        session.flush()
        session.add_all(
            [
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=trip.id,
                    item_type="attraction",
                    poi_name="西湖",
                    latitude=Decimal("30.220000"),
                    longitude=Decimal("120.120000"),
                    start_time=time(9, 30),
                    end_time=time(11, 0),
                    duration_min=120,
                    cost=Decimal("0.00"),
                    source="opentripmap:poi:185763",
                    source_updated_at=datetime(2026, 3, 1, 8, 0),
                    verification_status="verified",
                    value_kind="observed",
                    freshness_status="fresh",
                    review_requirement="none",
                    why_note="离酒店步行可达",
                    intro="三潭印月所在",
                    sort_no=0,
                ),
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=trip.id,
                    item_type="food",
                    poi_name="楼外楼",
                    start_time=time(12, 0, 30),
                    cost=Decimal("88.50"),
                    source="llm.open_day",
                    review_requirement="before_departure",
                    freshness_status="stale",
                    sort_no=1,
                ),
            ]
        )
        session.add_all(
            [
                BudgetDetail(itinerary_id=trip.id, category="门票", amount=Decimal("0.00"), item_count=1),
                BudgetDetail(itinerary_id=trip.id, category="餐饮", amount=Decimal("88.50"), item_count=1),
            ]
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/auth/login", json={"username": "alice", "password": "x"})
    return client


# ---------- 列表 ----------


def test_list_summary_shape_order_and_totals(client: TestClient) -> None:
    data = client.get("/api/itinerary").json()["data"]
    # 软删行程不可见；按 id 倒序（与 Java orderByDesc(id) 一致）
    assert [row["title"] for row in data] == ["杭州3日游"]
    row = data[0]
    assert set(row) == {
        "id",
        "title",
        "city",
        "startDate",
        "endDate",
        "days",
        "persons",
        "budget",
        "totalAmount",
        "status",
        "tripTheme",
        "createdAt",
        # S1 新增：封面/收藏/归档/分享态（hasShare 只回布尔，token 不下发列表）
        "coverUrl",
        "coverSource",
        "coverCredit",
        "favorite",
        "archived",
        "hasShare",
    }
    assert row["startDate"] == "2026-04-01" and row["budget"] == 3000.0
    assert row["totalAmount"] == 88.5  # 一次 IN 查询聚合，不是逐行程查
    assert row["coverUrl"] is None and row["coverCredit"] is None
    assert row["favorite"] is False and row["archived"] is False and row["hasShare"] is False


# ---------- 详情形状 ----------


def test_detail_top_level_shape(client: TestClient) -> None:
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    data = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    assert data["schemaVersion"] == "1.0"
    assert data["qualityRuleVersion"] == "travel-quality-1.2", "1.2 = 增加预算超支规则"
    assert data["validatedAt"] is None
    assert data["suggestions"] == [{"x": 1}]
    assert data["budgetList"] == [
        {"category": "门票", "amount": 0.0, "itemCount": 1},
        {"category": "餐饮", "amount": 88.5, "itemCount": 1},
    ]
    assert data["totalAmount"] == 88.5
    # S1：详情是本人视角——cover 三列 + shareToken 可回（列表只回 hasShare 布尔）
    assert data["coverUrl"] is None and data["coverSource"] is None
    assert data["coverCredit"] is None and data["shareToken"] is None
    assert data["favorite"] is False and data["archived"] is False


def test_stay_nights_uses_main_column_not_item_scan(client: TestClient) -> None:
    """主表 stay_nights=0 是有效值；只有 NULL 才回落 days-1。

    按"有酒店项的天数"反推会把 3 天行程算成更少（退房日没有酒店项）。
    """
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    data = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    assert data["stayNights"] == 0


def test_day_metadata_and_item_field_names(client: TestClient) -> None:
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    day = client.get(f"/api/itinerary/{trip_id}").json()["data"]["dayList"][0]
    assert day["theme"] == "湖畔" and day["miniRoute"] == {"a": 1}
    assert day["photoSpots"] == [{"name": "断桥"}] and day["practicalNotes"] == ["早去"]
    # dayOptions 必须透出：已落库却不透出即前端槽位空转
    assert day["dayOptions"] == [{"label": "A", "summary": "s", "tradeoff": "t"}]
    first, second = day["items"]
    assert {"whyThis", "factEvidence", "imageUrl", "image", "description", "intro", "sortNo"} <= set(first)
    assert first["startTime"] == "09:30", "Jackson ISO_LOCAL_TIME 省略零秒"
    assert second["startTime"] == "12:00:30"
    assert first["whyThis"] == "离酒店步行可达"
    # 语料库退役：description 不再回查 poi_knowledge,与 intro 同源（条目快照自带）
    assert first["description"] == "三潭印月所在" and first["intro"] == "三潭印月所在"
    assert first["sourceUpdatedAt"] == "2026-03-01T08:00"
    assert second["description"] is None  # 无 intro 的条目如实为空，不做整城加载


def test_quality_status_and_pending_facts(client: TestClient) -> None:
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    data = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    # 有一项 review_requirement != none 且 stale → STALE 优先于 READY_WITH_WARNINGS
    assert data["pendingFactCount"] == 1
    assert data["qualityStatus"] == "STALE"
    report = data["qualityReport"]
    assert report["blockingIssues"] == [] and len(report["warnings"]) == 1
    assert report["metrics"] == {"pendingFactCount": 1}
    # destinationStatus 口径（P1-2 修正后）：存在 opentripmap/nominatim/web.search
    # 权威来源行 → researched；llm.open_day 是生成期标记不算权威
    assert data["destinationStatus"] == "researched"
    assert [s["provider"] for s in data["sources"]] == ["opentripmap:poi:185763", "llm.open_day"]


def test_incomplete_day_takes_precedence_over_stale(client: TestClient) -> None:
    """PENDING/RUNNING 日 → DRAFT 必须优先于 STALE/READY（Java 判定顺序）。"""
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    with db_session.session_scope() as session:
        session.query(ItineraryDay).filter_by(itinerary_id=trip_id).one().generation_status = "PENDING"
    data = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    assert data["qualityStatus"] == "DRAFT"
    assert data["pendingFactCount"] == 1, "计数仍如实上报，只是不参与状态判定"


def test_corrupt_optional_json_does_not_break_detail(client: TestClient) -> None:
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    with db_session.session_scope() as session:
        day = session.query(ItineraryDay).first()
        day.metadata_json = "{ 坏掉的 json"
    data = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    assert data["dayList"][0]["theme"] is None
    assert len(data["dayList"][0]["items"]) == 2, "元数据损坏不应阻塞点位读取"


# ---------- 归属与缓存 ----------


def test_other_users_itinerary_is_404_not_403(client: TestClient) -> None:
    assert client.get("/api/itinerary/2").status_code == 404  # 属于 mallory
    assert client.get("/api/itinerary/999").status_code == 404
    assert client.get("/api/itinerary/0").status_code == 400  # 业务路径按 Java 语义映射校验错误


def test_soft_deleted_itinerary_is_404(client: TestClient) -> None:
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    with db_session.session_scope() as session:
        session.query(ItineraryMain).filter_by(id=trip_id).one().deleted = 1
    assert client.get(f"/api/itinerary/{trip_id}").status_code == 404
    assert [r["id"] for r in client.get("/api/itinerary").json()["data"]] == []


def test_detail_is_cached_and_evicted_per_itinerary(client: TestClient) -> None:
    trip_id = client.get("/api/itinerary").json()["data"][0]["id"]
    assert client.get(f"/api/itinerary/{trip_id}").json()["data"]["title"] == "杭州3日游"
    with db_session.session_scope() as session:
        session.query(ItineraryMain).filter_by(id=trip_id).one().title = "改过名"
    assert client.get(f"/api/itinerary/{trip_id}").json()["data"]["title"] == "杭州3日游", "详情应命中缓存"
    itinerary_query.evict_detail(1, trip_id)
    assert client.get(f"/api/itinerary/{trip_id}").json()["data"]["title"] == "改过名", "精确失效后必须回源"


# ---------- 路由顺序与偏好 ----------


def test_literal_preference_routes_are_not_swallowed_by_id(client: TestClient) -> None:
    """/preferences 若声明在 /{id} 之后会被当成 id=preferences → 422，Java 无此坑。"""
    response = client.get("/api/itinerary/preferences")
    assert response.status_code == 200 and response.json()["data"] == []
    assert client.get("/api/itinerary/preferences/signals").json()["data"] == []


def test_preference_signals_upsert_and_visibility(client: TestClient) -> None:
    body = {
        "explicitPreferences": ["人文历史", "美食"],
        "hardConstraints": ["不带小孩"],
        "negativePreferences": ["购物"],
        "source": "explicit",
        "confidence": 0.8,
    }
    assert client.post("/api/itinerary/preferences/signals", json=body).json()["code"] == 200
    assert (
        client.post("/api/itinerary/preferences/signals", json={"explicitPreferences": ["美食"]}).json()["code"] == 200
    )

    signals = client.get("/api/itinerary/preferences/signals").json()["data"]
    by_label = {row["label"]: row for row in signals}
    assert by_label["美食"]["count"] == 2, "同标签重复正向信号要累加而不是插新行"
    assert by_label["购物"]["negative"] is True and by_label["不带小孩"]["hardConstraint"] is True
    assert by_label["人文历史"]["confidence"] == 0.8, "confidence 每次信号写入都会重写（同 Java）"
    # signals 列表排序：硬约束优先，其余按更新时间倒序
    hard_first = client.get("/api/itinerary/preferences/signals").json()["data"][0]["label"]
    assert hard_first == "不带小孩"
    # top 只排除负向项、按 count 倒序；硬约束标签 count=1 同样会被选入（同 Java）。
    # count 相同的先后顺序在 MySQL 下不保证，因此只断言首位与集合内容。
    top = client.get("/api/itinerary/preferences").json()["data"]
    assert top[0] == "美食" and "购物" not in top
    assert set(top) == {"美食", "不带小孩", "人文历史"}
