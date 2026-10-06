"""SPEC C3.5 条目对/错反馈测试（SQLite，无网络/Redis）。

断言焦点（docs/PLAN-C3.5-反馈回路-预研.md §1/§3 + Q1/Q5/Q6 推荐列）：
1. addon 门控：item_feedback 默认关，三端点全 404（隐藏而非 403）；开启后恢复；
2. 权限矩阵（Q1 读面皆可写）：owner/editor/viewer 都能写；非成员 404 不暴露存在性；
3. upsert 一人一条：重复提交覆盖（一人一行，value 可翻转），同 item 不同人各一行；
4. 口径校验：wrong 缺 reason 400、right 带 reason 400、未收录 reason 拒绝、
   note>200 400、跨行程 item 400；
5. 撤销（Q5 硬删）：本人可撤；无行 404；撤不动别人的反馈；
6. 回显：GET 仅本人数据（owner 看不到旅伴评了什么，展示匿名）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router, user_router
from app.api.business.feedback import router as feedback_router
from app.common import addons as addons_module
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItemFeedback, ItineraryDay, ItineraryItem, ItineraryMain, ItineraryMember, SysUser
from app.services import user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def _isolated_addon_cache():
    """addons 模块级 5s TTL 缓存是跨文件时序雷：其他用例经管理端开启 item_feedback
    后，缓存残值会让"默认关"用例在本文件自己的空库里看到 True（2026-09-30 全量
    实录）。默认态用例必须看到真默认值——每个用例前后清缓存。"""
    addons_module._cache.clear()
    yield
    addons_module._cache.clear()


@pytest.fixture()
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    # 密闭性：本地 .env 可能把 C3.5 实盘开的 ITEM_FEEDBACK_ENABLED=True 带进来，
    # 抬高"env 默认"让默认关用例变红（CI 无 .env 不受影响）——钉回代码默认值。
    monkeypatch.setattr(settings, "item_feedback_enabled", False)
    cache_store.reset_for_tests()
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        for name in ("alice", "bob", "carol", "dave"):
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
        )
        session.add(trip)
        other = ItineraryMain(user_id=2, title="北京1日游", city="北京", days=1, persons=1, status=2)
        session.add(other)
        session.flush()
        day = ItineraryDay(itinerary_id=trip.id, day_no=1, generation_status="SUCCEEDED")
        other_day = ItineraryDay(itinerary_id=other.id, day_no=1, generation_status="SUCCEEDED")
        session.add_all([day, other_day])
        session.flush()
        session.add_all(
            [
                ItineraryItem(day_id=day.id, itinerary_id=1, item_type="attraction", poi_name="西湖", sort_no=0),
                ItineraryItem(day_id=day.id, itinerary_id=1, item_type="food", poi_name="楼外楼", sort_no=1),
                ItineraryItem(day_id=other_day.id, itinerary_id=2, item_type="attraction", poi_name="故宫", sort_no=0),
            ]
        )
        # bob=editor、carol=viewer（Q1：viewer 也可写）；dave 非成员
        session.add_all(
            [
                ItineraryMember(itinerary_id=1, user_id=2, role="editor"),
                ItineraryMember(itinerary_id=1, user_id=3, role="viewer"),
            ]
        )


def _client_for(username: str) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(feedback_router)
    client = TestClient(app, follow_redirects=False)
    assert client.post("/api/auth/login", json={"username": username, "password": "x"}).status_code == 200
    return client


@pytest.fixture
def alice(db):
    return _client_for("alice")


@pytest.fixture
def bob(db):
    return _client_for("bob")


@pytest.fixture
def carol(db):
    return _client_for("carol")


@pytest.fixture
def dave(db):
    return _client_for("dave")


@pytest.fixture
def addon_on(monkeypatch):
    """item_feedback 默认关（env 默认 False）；开启 = 只亮这一个能力。"""
    monkeypatch.setattr(addons_module, "is_enabled", lambda key: key == "item_feedback")


def _post(client: TestClient, item_id: int = 1, **overrides):
    body = {"itemId": item_id, "value": "wrong", "reason": "wrong_location", **overrides}
    # value=right 的默认形态不携带 reason（显式传了 reason 的用例除外，用于验证 400）
    if body["value"] == "right" and "reason" not in overrides:
        body.pop("reason")
    return client.post("/api/itinerary/1/feedback", json=body)


# ---------- addon 门控 ----------


def test_endpoints_hidden_when_addon_off(alice, bob, carol):
    """默认关：三端点一律 404（隐藏能力存在性），owner 也不可见。"""
    assert alice.get("/api/itinerary/1/feedback").status_code == 404
    assert _post(alice).status_code == 404
    assert bob.delete("/api/itinerary/1/feedback/1").status_code == 404
    assert carol.get("/api/itinerary/1/feedback").status_code == 404


def test_endpoints_reopen_when_addon_on(alice, addon_on):
    assert alice.get("/api/itinerary/1/feedback").status_code == 200
    assert _post(alice).status_code == 200


# ---------- 权限矩阵（Q1：读面皆可写） ----------


def test_owner_editor_viewer_can_write_only_own_rows_visible(alice, bob, carol, addon_on):
    assert _post(alice, value="right").status_code == 200
    assert _post(bob, reason="closed").status_code == 200
    assert _post(carol, reason="not_interested", note="排队太久不感兴趣").status_code == 200

    # 回显只含本人数据：alice 看不到 bob/carol 评了什么（展示匿名）
    mine = alice.get("/api/itinerary/1/feedback").json()["data"]["feedbacks"]
    assert [row["value"] for row in mine] == ["right"]
    assert bob.get("/api/itinerary/1/feedback").json()["data"]["feedbacks"][0]["reason"] == "closed"

    with db_session.session_scope() as session:
        rows = session.scalars(select(ItemFeedback).where(ItemFeedback.itinerary_id == 1)).all()
        assert len(rows) == 3, "三人各一行"


def test_non_member_gets_404(dave, addon_on):
    """非成员不可读不可写，且不暴露行程存在性。"""
    assert _post(dave).status_code == 404
    assert dave.get("/api/itinerary/1/feedback").status_code == 404


# ---------- upsert 一人一条 ----------


def test_repost_same_item_overwrites_single_row(alice, addon_on):
    assert _post(alice, reason="wrong_location").status_code == 200
    flipped = _post(alice, value="right")
    assert flipped.status_code == 200
    assert flipped.json()["data"]["value"] == "right"
    with db_session.session_scope() as session:
        rows = session.scalars(select(ItemFeedback).where(ItemFeedback.user_id == 1)).all()
        assert len(rows) == 1, "一人一条：覆盖而非追加"
        assert int(rows[0].value) == 1 and rows[0].reason is None, "value=right 时 reason 恒空"


# ---------- 口径校验 ----------


def test_wrong_requires_reason(alice, addon_on):
    assert _post(alice, reason=None).status_code == 400


def test_right_rejects_reason(alice, addon_on):
    assert _post(alice, value="right", reason="other").status_code == 400


def test_unknown_reason_rejected(alice, addon_on):
    assert _post(alice, reason="kind_of_ok").status_code == 400


def test_note_over_200_rejected(alice, addon_on):
    assert _post(alice, note="长" * 201).status_code == 400


def test_item_from_other_trip_rejected(bob, addon_on):
    """bob 对自己北京行程的 item(3) 打杭州行程(1)的反馈 → 400。"""
    assert _post(bob, item_id=3).status_code == 400


# ---------- 撤销（Q5：硬删，仅本人） ----------


def test_revoke_deletes_own_row_only(alice, bob, addon_on):
    assert _post(alice, reason="closed").status_code == 200
    assert _post(bob, reason="wrong_time").status_code == 200

    # bob 想撤 alice 评过的同一 item：他只有自己的行，删完 alice 的还在
    assert bob.delete("/api/itinerary/1/feedback/1").status_code == 200
    with db_session.session_scope() as session:
        remaining = session.scalars(select(ItemFeedback).where(ItemFeedback.item_id == 1)).all()
        assert len(remaining) == 1 and remaining[0].user_id == 1

    assert bob.delete("/api/itinerary/1/feedback/1").status_code == 404, "无行再撤 404"
    assert alice.delete("/api/itinerary/1/feedback/1").status_code == 200
    with db_session.session_scope() as session:
        assert session.scalars(select(ItemFeedback)).all() == []


# ---------- 回显 ----------


def test_list_shape_and_anonymity(alice, addon_on):
    _post(alice, reason="wrong_price", note="人均虚高")
    row = alice.get("/api/itinerary/1/feedback").json()["data"]["feedbacks"][0]
    assert set(row) == {"itemId", "value", "reason", "note", "createdAt", "updatedAt"}, "展示匿名：无 userId"
    assert row == {
        "itemId": 1,
        "value": "wrong",
        "reason": "wrong_price",
        "note": "人均虚高",
        "createdAt": row["createdAt"],
        "updatedAt": row["updatedAt"],
    }
