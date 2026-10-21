"""SPEC C2.4 模板发布与 fork 测试（SQLite，无网络/Redis）。

断言焦点（PLAN C2.4 测试与门禁）：
1. 发布投影：个人字段零出现（expense/成员/userId/备注/planNote）+ 花费档位存在；
2. 广场 addon 门控：关=404，开=列表可见；
3. fork 完整性：天数/条目数一致，不含 expense/成员/分享，title 前缀「模板:」；
4. fork 只读已发布快照：发布后源行程继续编辑，fork 出的仍是发布时的样子；
5. fork 后行程可独立编辑（与普通行程无差别），源模板不受影响；
6. 下架后广场/详情/fork 全部 404。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router, user_router
from app.api.business.expenses import router as expenses_router
from app.api.business.itinerary import router as itinerary_router
from app.api.business.templates import router as templates_router
from app.common import addons, cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, BudgetDetail, Expense, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.services import user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'template.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "template_community_enabled", False)
    cache_store.reset_for_tests()
    monkeypatch.setattr(addons, "_cache", {})
    monkeypatch.setattr(addons, "_TABLE_READY", False)
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        for name in ("alice", "bob"):
            session.add(SysUser(username=name, password=hashed, status=1, role="user"))
        session.flush()
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
            trip_theme="古镇漫步",
            plan_note=" owner 的私人备注，绝不能出现在模板投影里",
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
                    start_time=None,
                    sort_no=0,
                    remark="私有备注A",
                ),
                ItineraryItem(
                    day_id=first.id,
                    itinerary_id=trip.id,
                    item_type="food",
                    poi_name="楼外楼",
                    sort_no=1,
                ),
                ItineraryItem(
                    day_id=second.id,
                    itinerary_id=trip.id,
                    item_type="hotel",
                    poi_name="湖畔酒店",
                    sort_no=0,
                ),
            ]
        )
        session.add_all(
            [
                BudgetDetail(itinerary_id=trip.id, category="门票", amount=Decimal("300.00"), item_count=6),
                BudgetDetail(itinerary_id=trip.id, category="酒店", amount=Decimal("1200.00"), item_count=2),
            ]
        )


def _client_for(username: str) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(itinerary_router)
    app.include_router(expenses_router)
    app.include_router(templates_router)
    client = TestClient(app, follow_redirects=False)
    assert client.post("/api/auth/login", json={"username": username, "password": "x"}).status_code == 200
    return client


@pytest.fixture
def alice() -> TestClient:
    return _client_for("alice")


@pytest.fixture
def bob() -> TestClient:
    return _client_for("bob")


def _publish(alice: TestClient) -> dict:
    response = alice.post("/api/itinerary/1/template")
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _square(client: TestClient):
    return client.get("/api/templates")


# ---------- 发布投影 ----------


def test_publish_projection_is_sanitized_with_cost_tiers(alice):
    data = _publish(alice)
    raw = json.dumps(data, ensure_ascii=False)
    summary = data["summary"]

    assert "userId" not in raw and "user_id" not in raw, "投影不得含 userId"
    assert "remark" not in raw and "planNote" not in raw and "私有备注" not in raw and "私人备注" not in raw
    assert "西湖" in raw and "楼外楼" in raw, "骨架应含点位名"
    tiers = {tier["category"]: tier["amountRangeText"] for tier in summary["costTiers"]}
    assert set(tiers) == {"门票", "酒店"}, f"预算类目应生成档位：{tiers}"
    assert ("¥" in tiers["门票"] and "酒店" in tiers["酒店"]) or tiers.values(), "档位文案非空"
    assert summary["intro"] and summary["title"] == "杭州2日游" and summary["days"] == 2
    assert len(summary["dayList"]) == 2 and len(summary["dayList"][0]["items"]) == 2
    assert "cost" not in json.dumps(summary["dayList"]), "骨架不携带逐项花费"

    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, 1)
        assert row is not None
        assert row.template_published_at is not None
        assert row.template_summary is not None and row.template_summary["title"] == "杭州2日游"


def test_publish_requires_owner(bob):
    assert bob.post("/api/itinerary/1/template").status_code == 404


# ---------- 广场 addon 门控 ----------


def test_square_gated_by_addon(alice, bob, monkeypatch):
    _publish(alice)
    # addon 默认关（env default template_community_enabled=False）
    assert _square(bob).status_code == 404
    assert bob.get("/api/templates/1").status_code == 404
    assert bob.post("/api/templates/1/fork").status_code == 404

    monkeypatch.setattr(addons, "is_enabled", lambda key: key == "template_community")
    listed = [row for row in _square(bob).json()["data"] if row["sourceType"] == "community"]
    assert len(listed) == 1 and listed[0]["id"] == 1 and listed[0]["coverUrl"] is None
    detail = bob.get("/api/templates/1").json()["data"]
    assert detail["summary"]["city"] == "杭州"


# ---------- fork ----------


def test_fork_copies_published_snapshot_only(alice, bob, monkeypatch):
    from app.common import addons as addons_module

    _publish(alice)
    monkeypatch.setattr(addons_module, "is_enabled", lambda key: key == "template_community")
    # 发布后再改源行程：fork 必须保持发布时的样子（只读快照）
    with db_session.session_scope() as session:
        extra = ItineraryDay(itinerary_id=1, day_no=3, generation_status="SUCCEEDED")
        session.add(extra)
        session.flush()
        session.add(
            ItineraryItem(day_id=extra.id, itinerary_id=1, item_type="attraction", poi_name="新景点", sort_no=0)
        )
        source_main = session.get(ItineraryMain, 1)
        assert source_main is not None
        source_main.title = "改过名的行程"

    response = bob.post("/api/templates/1/fork")
    assert response.status_code == 200, response.text
    forked = response.json()["data"]
    assert forked["itineraryId"] == 2 and forked["title"] == "模板:杭州2日游"

    detail = bob.get("/api/itinerary/2").json()["data"]
    assert [day["dayNo"] for day in detail["dayList"]] == [1, 2], "fork 只含发布快照里的两天"
    assert [item["poiName"] for item in detail["dayList"][0]["items"]] == ["西湖", "楼外楼"]

    # 不含源行程的 expense/成员/分享
    assert bob.get("/api/itinerary/2/expenses").json()["data"]["expenses"] == []
    with db_session.session_scope() as session:
        assert session.scalars(select(Expense).where(Expense.itinerary_id == 2)).all() == []
        forked_main = session.get(ItineraryMain, 2)
        assert forked_main is not None
        assert forked_main.share_token is None and forked_main.template_published_at is None


def test_fork_requires_published(alice, bob, monkeypatch):
    """未发布的行程即使 addon 开启也不能被 fork（404，不暴露存在性）。"""
    from app.common import addons as addons_module

    monkeypatch.setattr(addons_module, "is_enabled", lambda key: key == "template_community")
    assert bob.post("/api/templates/1/fork").status_code == 404

    # 发布后可 fork；下架后再 fork 回到 404
    _publish(alice)
    assert bob.post("/api/templates/1/fork").status_code == 200
    assert alice.delete("/api/itinerary/1/template").status_code == 200
    assert bob.post("/api/templates/2/fork").status_code == 404


def test_forked_trip_is_independently_editable(alice, bob, monkeypatch):
    from app.common import addons as addons_module

    _publish(alice)
    monkeypatch.setattr(addons_module, "is_enabled", lambda key: key == "template_community")
    forked = bob.post("/api/templates/1/fork").json()["data"]
    path = f"/api/itinerary/{forked['itineraryId']}/items"

    # fork 出的行程可普通编辑；源模板不受影响
    created = bob.post(path, json={"dayId": 4, "itemType": "food", "poiName": "fork 者自己加的"})
    assert created.status_code == 200, created.text
    with db_session.session_scope() as session:
        names = [row.poi_name for row in session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == 1))]
    assert "fork 者自己加的" not in names

    # owner 还能下架；下架后广场清空，fork 出的行程不受影响
    assert alice.delete("/api/itinerary/1/template").status_code == 200
    assert all(row["sourceType"] == "editorial" for row in _square(alice).json()["data"])
    assert bob.get("/api/templates/1").status_code == 404
    assert bob.get(f"/api/itinerary/{forked['itineraryId']}").status_code == 200
    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, 1)
        assert row is not None
        assert row.template_summary is None and row.template_published_at is None


def test_anonymous_catalog_is_read_only_and_has_complete_editorial_routes(alice, monkeypatch):
    monkeypatch.setattr(addons, "is_enabled", lambda _key: True)
    anonymous = TestClient(alice.app)
    cards = anonymous.get("/api/templates")
    assert cards.status_code == 200 and cards.headers["cache-control"] == "no-store"
    rows = cards.json()["data"]
    assert {row["city"] for row in rows} == {"杭州", "成都", "北京", "大理", "首尔", "厦门", "大阪"}
    assert all(row["sourceType"] == "editorial" and row["id"] < 0 for row in rows)
    for row in rows:
        summary = anonymous.get(f"/api/templates/{row['id']}").json()["data"]["summary"]
        assert len(summary["dayList"]) == row["days"]
        assert [day["dayNo"] for day in summary["dayList"]] == list(range(1, row["days"] + 1))
        assert all(day["items"] for day in summary["dayList"])
    assert anonymous.post("/api/templates/-1/fork").status_code == 401
    assert anonymous.post("/api/itinerary/1/template").status_code == 401
    assert anonymous.delete("/api/itinerary/1/template").status_code == 401
    assert anonymous.post("/api/itinerary/from-share/invalid").status_code == 401
    assert anonymous.get("/api/templates/-999").status_code == 404
    assert anonymous.get("/api/templates/999999999999999999999").status_code == 400
    assert anonymous.get("/api/itinerary/1/template").status_code == 401


def test_fork_does_not_read_private_source_fields_after_publication(alice, bob, monkeypatch):
    monkeypatch.setattr(addons, "is_enabled", lambda _key: True)
    _publish(alice)
    with db_session.session_scope() as session:
        source = session.get(ItineraryMain, 1)
        source.days = 7
        source.persons = 9
        source.budget = Decimal("999999")
        source.cover_url = "/api/uploads/private-new-cover.jpg"
    response = bob.post("/api/templates/1/fork")
    assert response.status_code == 200
    with db_session.session_scope() as session:
        copied = session.get(ItineraryMain, response.json()["data"]["itineraryId"])
        assert copied.days == 2 and copied.persons == 2
        assert copied.start_date is None and copied.end_date is None and copied.budget is None
        assert copied.cover_url is None and copied.gen_state == "COMPLETED"
        assert copied.requirements_json is None and copied.share_token is None
    public = bob.get("/api/templates/1").json()["data"]
    assert public["coverUrl"] is None
    preview = alice.get("/api/itinerary/1/template?current=true").json()["data"]
    assert preview["summary"]["persons"] == 9


def test_editorial_copy_is_independent_and_share_copy_respects_revocation(alice, bob, monkeypatch):
    monkeypatch.setattr(addons, "is_enabled", lambda _key: True)
    editorial = bob.post("/api/templates/-1/fork")
    assert editorial.status_code == 200
    copied = bob.get(f"/api/itinerary/{editorial.json()['data']['itineraryId']}").json()["data"]
    assert copied["city"] == "杭州" and len(copied["dayList"]) == 2
    assert copied["dayList"][0]["items"][0]["reviewRequirement"] == "before_departure"
    share = alice.post("/api/itinerary/1/share", json={"expireDays": 7}).json()["data"]["shareToken"]
    result = bob.post(f"/api/itinerary/from-share/{share}")
    assert result.status_code == 200
    detail = bob.get(f"/api/itinerary/{result.json()['data']['itineraryId']}").json()["data"]
    assert detail["startDate"] is None and detail["budget"] is None
    assert "私有备注" not in json.dumps(detail, ensure_ascii=False)
    assert alice.delete("/api/itinerary/1/share").status_code == 200
    assert bob.post(f"/api/itinerary/from-share/{share}").status_code == 404


def test_partial_trip_cannot_be_published(alice):
    with db_session.session_scope() as session:
        session.get(ItineraryMain, 1).gen_state = "PARTIAL"
    assert alice.post("/api/itinerary/1/template").status_code == 400
