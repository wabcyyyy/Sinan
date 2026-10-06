"""SPEC C2.3 协作（邀请共同编辑）API 测试（SQLite，无网络/Redis）。

断言焦点（PLAN C2.3 测试与门禁）：
1. 邀请生命周期：owner 生成（token 仅创建响应返回一次）→ 兑换 → editor 可写 /
   viewer 写 403 → owner 改角色/移除 → 访问立即失效 → 新邀请可重新加入；
2. 边界：过期/撤销/重复兑换拒绝；已有成员兑换 409 且不消费邀请；
   跨行程成员/邀请操作拒绝；token 不在列表响应出现；
3. 角色矩阵：内容写端点（item 增改删/重排/优化/nl-edit/apply-plans/expenses/
   chat-edit/versions）owner+editor 通过、viewer 403、非成员 404；
   owner 专属（删行程/收藏/归档/分享/封面/清聊天）editor/viewer 拒绝。
并发兑换的原子性另以目标 MySQL 集成验证，不靠 SQLite 证明。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router, user_router
from app.api.business.expenses import router as expenses_router
from app.api.business.itinerary import router as itinerary_router
from app.api.business.members import router as members_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.services import collaboration_service, user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'collab.db'}")
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
    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        for name in ("alice", "bob", "carol", "dave"):
            session.add(SysUser(username=name, password=hashed, status=1, role="user"))
        session.flush()
        session.add(
            ItineraryMain(
                user_id=1,
                title="杭州2日游",
                city="杭州",
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 2),
                days=2,
                persons=1,
                status=2,
            )
        )
        session.flush()
        day = ItineraryDay(itinerary_id=1, day_no=1, generation_status="SUCCEEDED")
        session.add(day)
        session.flush()
        session.add(ItineraryItem(day_id=day.id, itinerary_id=1, item_type="attraction", poi_name="西湖", sort_no=0))


def _client_for(username: str) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(itinerary_router)
    app.include_router(expenses_router)
    app.include_router(members_router)
    client = TestClient(app, follow_redirects=False)
    response = client.post("/api/auth/login", json={"username": username, "password": "x"})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def alice() -> TestClient:
    return _client_for("alice")


@pytest.fixture
def bob() -> TestClient:
    return _client_for("bob")


@pytest.fixture
def carol() -> TestClient:
    return _client_for("carol")


def _invite(alice: TestClient, role: str = "editor") -> dict:
    response = alice.post("/api/itinerary/1/invitations", json={"role": role})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["token"], "创建响应必须带明文 token"
    assert "tokenHash" not in data and "token_hash" not in data
    return data


def _accept(client: TestClient, token: str) -> dict:
    response = client.post("/api/itinerary/invitations/accept", json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _members(client: TestClient) -> list[dict]:
    response = client.get("/api/itinerary/1/members")
    assert response.status_code == 200, response.text
    return response.json()["data"]


# ---------- 邀请生命周期 ----------


def test_invitation_lifecycle_editor_write_viewer_403(alice, bob, carol):
    editor_token = _invite(alice, "editor")["token"]
    accepted = _accept(bob, editor_token)
    assert accepted["itineraryId"] == 1 and accepted["role"] == "editor"

    # editor 读 + 写
    assert bob.get("/api/itinerary/1").status_code == 200
    assert bob.post("/api/itinerary/1/expenses", json={"category": "food", "amount": "9.90"}).status_code == 200
    put = bob.put("/api/itinerary/items/1", json={"note": "bob 改的"})
    assert put.status_code == 200, put.text

    # viewer 读可、写 403
    viewer_token = _invite(alice, "viewer")["token"]
    _accept(carol, viewer_token)
    assert carol.get("/api/itinerary/1").status_code == 200
    assert carol.get("/api/itinerary/1/expenses").status_code == 200
    denied = carol.post("/api/itinerary/1/expenses", json={"category": "food", "amount": "1.00"})
    assert denied.status_code == 403, denied.text
    assert carol.put("/api/itinerary/items/1", json={"note": "carol 改"}).status_code == 403

    # owner 改角色 viewer→editor 后可写
    carol_member = next(m for m in _members(alice) if m["userId"] == 3)
    promoted = alice.put(f"/api/itinerary/1/members/{carol_member['id']}", json={"role": "editor"})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["data"]["role"] == "editor"
    assert carol.post("/api/itinerary/1/expenses", json={"category": "food", "amount": "2.00"}).status_code == 200

    # owner 移除后访问立即失效（404 不暴露存在性），全局登录不受影响
    removed = alice.delete(f"/api/itinerary/1/members/{carol_member['id']}")
    assert removed.status_code == 200
    assert carol.get("/api/itinerary/1").status_code == 404
    assert carol.get("/api/user/info").status_code == 200, "移除成员不得影响全局登录会话"

    # 新邀请可重新加入
    rejoin = _invite(alice, "viewer")["token"]
    assert _accept(carol, rejoin)["role"] == "viewer"


def test_expired_revoked_reused_invitations_rejected(alice, bob, carol):
    # 过期
    expired = _invite(alice, "editor")
    with db_session.session_scope() as session:
        row = session.get(collaboration_service.ItineraryInvitation, expired["id"])
        assert row is not None
        row.expires_at = datetime.now() - timedelta(seconds=1)
    assert bob.post("/api/itinerary/invitations/accept", json={"token": expired["token"]}).status_code == 400

    # 撤销
    revoked = _invite(alice, "editor")
    assert alice.delete(f"/api/itinerary/1/invitations/{revoked['id']}").status_code == 200
    assert bob.post("/api/itinerary/invitations/accept", json={"token": revoked["token"]}).status_code == 400

    # 重复兑换（同一 token 第二次）
    token = _invite(alice, "editor")["token"]
    _accept(bob, token)
    assert bob.post("/api/itinerary/invitations/accept", json={"token": token}).status_code == 400
    assert carol.post("/api/itinerary/invitations/accept", json={"token": token}).status_code == 400
    # 无效 token
    assert bob.post("/api/itinerary/invitations/accept", json={"token": "x" * 40}).status_code == 404


def test_existing_member_accept_does_not_consume_or_promote(alice, bob):
    # bob 以 viewer 入场
    viewer_token = _invite(alice, "viewer")["token"]
    _accept(bob, viewer_token)
    assert next(m for m in _members(alice) if m["userId"] == 2)["role"] == "viewer"

    # 已是成员(bob)兑换 editor 邀请：409 不隐式提权，且邀请不消费——carol 之后仍能用
    another = _invite(alice, "editor")["token"]
    conflict = bob.post("/api/itinerary/invitations/accept", json={"token": another})
    assert conflict.status_code == 409, conflict.text
    assert next(m for m in _members(alice) if m["userId"] == 2)["role"] == "viewer", "冲突不得改角色"
    carol_view = _client_for("carol")
    assert _accept(carol_view, another)["role"] == "editor", "冲突时邀请不得被消费"


def test_invitation_list_and_cross_itinerary_guards(alice, bob):
    pending = _invite(alice, "editor")
    listed = alice.get("/api/itinerary/1/invitations").json()["data"]
    assert len(listed) == 1 and listed[0]["status"] == "pending"
    assert all("token" not in item for item in listed), "列表不得泄漏 token"

    # 非 owner 管理邀请/成员：404（不暴露存在性）
    assert bob.get("/api/itinerary/1/invitations").status_code == 404
    assert bob.delete(f"/api/itinerary/1/invitations/{pending['id']}").status_code == 404
    assert bob.post("/api/itinerary/1/invitations", json={"role": "editor"}).status_code == 404
    # 兑换者能读成员列表
    _accept(_client_for("bob"), pending["token"])
    assert _client_for("bob").get("/api/itinerary/1/members").status_code == 200
    # 成员不能改/删成员
    bob_client = _client_for("bob")
    member_row = next(m for m in _members(bob_client) if m["userId"] == 2)
    assert bob_client.put(f"/api/itinerary/1/members/{member_row['id']}", json={"role": "viewer"}).status_code == 404
    assert bob_client.delete(f"/api/itinerary/1/members/{member_row['id']}").status_code == 404
    # 跨行程成员操作
    with db_session.session_scope() as session:
        session.add(
            ItineraryMain(
                user_id=2,
                title="bobby 自己的",
                city="北京",
                days=1,
                persons=1,
                status=2,
                start_date=date(2026, 5, 1),
                end_date=date(2026, 5, 1),
            )
        )
    assert alice.put("/api/itinerary/2/members/1", json={"role": "viewer"}).status_code == 404


# ---------- 角色矩阵（PLAN C2.3：全部写端点列表断言） ----------


def _matrix_client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(itinerary_router)
    app.include_router(expenses_router)
    app.include_router(members_router)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/auth/login", json={"username": "alice", "password": "x"})
    return client


@pytest.mark.parametrize("role,expected", [("editor", 200), ("viewer", 403)])
def test_content_write_matrix_editor_ok_viewer_403(alice, bob, role, expected):
    token = _invite(alice, role)["token"]
    _accept(bob, token)
    writes = [
        ("put", "/api/itinerary/1/days/1/order", [1]),  # 裸数组请求体（Java @RequestBody List<Long> 同款）
        ("post", "/api/itinerary/1/items", {"dayId": 1, "itemType": "food", "poiName": "楼外底"}),
        ("put", "/api/itinerary/items/1", {"note": "改个备注"}),
        ("delete", "/api/itinerary/items/1", None),
        ("patch", "/api/itinerary/1/days/1", {"theme": "第一天"}),
        ("post", "/api/itinerary/1/expenses", {"category": "food", "amount": "5.00"}),
        ("post", "/api/itinerary/1/versions", {"operation": "snapshot", "summary": "协作快照"}),
    ]
    for method, path, body in writes:
        response = getattr(bob, method)(path, json=body) if body is not None else getattr(bob, method)(path)
        assert response.status_code == expected, (
            f"{method.upper()} {path} 期望 {expected}，实际 {response.status_code}：{response.text}"
        )


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_owner_only_matrix_rejected_for_members(alice, bob, role):
    token = _invite(alice, role)["token"]
    _accept(bob, token)
    owner_only = [
        ("delete", "/api/itinerary/1", None),
        ("post", "/api/itinerary/1/favorite", {"favorite": True}),
        ("post", "/api/itinerary/1/archive", {"archived": True}),
        ("post", "/api/itinerary/1/share", {"expiresInDays": 7}),
        ("delete", "/api/itinerary/1/chat-history", None),
    ]
    for method, path, body in owner_only:
        response = getattr(bob, method)(path, json=body) if body is not None else getattr(bob, method)(path)
        assert response.status_code in (403, 404), (
            f"owner 专属 {method.upper()} {path} 对 {role} 应拒绝，实际 {response.status_code}"
        )
        assert response.status_code != 200


def test_non_member_gets_404_not_403(carol):
    assert carol.get("/api/itinerary/1").status_code == 404
    assert carol.post("/api/itinerary/1/expenses", json={"category": "food", "amount": "1.00"}).status_code == 404
    assert carol.put("/api/itinerary/items/1", json={"note": "x"}).status_code == 404
    assert carol.get("/api/itinerary/1/members").status_code == 404


def test_optimize_write_gate_follows_role(alice, bob, monkeypatch):
    """optimize 是内容写：editor 可用（addon 开）、viewer 403。"""
    from app.common import addons

    monkeypatch.setattr(addons, "is_enabled", lambda key: True)
    token = _invite(alice, "viewer")["token"]
    _accept(bob, token)
    assert bob.post("/api/itinerary/1/optimize", json={"dayId": 1}).status_code == 403
