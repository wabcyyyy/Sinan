"""SPEC C2.1 记账 API 路由测试（SQLite 内存库，无网络/Redis）。

断言焦点（PLAN C2.1 测试与门禁）：
1. CRUD：创建/列表聚合/更新/软删，金额 Decimal 字符串两位小数；
2. 归属：他人行程与不存在的账目一律 404，账目跨行程关联被拒；
3. 聚合：按 currency+category 分组求和，跨币种不相加；0.10+0.20=0.30；
   更新/删除后聚合即时正确；有效金额用 Decimal 比较而非 float；
4. 非法输入：非正数/超上限/超过两位小数/坏币种码/坏类目被 400 拒绝，
   不先量化非法输入再断言；
5. 关联：itemId 必须属于当前行程；PUT 显式 null 清空可空字段、缺省不改、
   非空列显式 null 拒绝。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.expenses import router as expenses_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    Expense,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.services import user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'expense.db'}")
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
        session.add(SysUser(username="alice", password=hashed, status=1, role="user"))
        session.add(SysUser(username="bob", password=hashed, status=1, role="user"))
        for owner, title in ((1, "杭州2日游"), (2, "外人的行程")):
            session.add(
                ItineraryMain(
                    user_id=owner,
                    title=title,
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
        other_day = ItineraryDay(itinerary_id=2, day_no=1, generation_status="SUCCEEDED")
        session.add_all([day, other_day])
        session.flush()
        session.add(
            ItineraryItem(
                day_id=day.id,
                itinerary_id=1,
                item_type="attraction",
                poi_name="西湖",
                sort_no=0,
            )
        )
        session.add(
            ItineraryItem(
                day_id=other_day.id,
                itinerary_id=2,
                item_type="food",
                poi_name="别家的馆子",
                sort_no=0,
            )
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(expenses_router)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/auth/login", json={"username": "alice", "password": "x"})
    return client


def _create(client: TestClient, itinerary_id: int = 1, **overrides) -> dict:
    body = {"category": "food", "amount": "12.50", "currency": "CNY"}
    body.update(overrides)
    response = client.post(f"/api/itinerary/{itinerary_id}/expenses", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_day_association_and_owner_check_share_write_transaction(client, monkeypatch):
    from sqlalchemy import event

    from app.services import itinerary_query

    checked_sessions = []
    write_sessions = []
    original = itinerary_query.require_writable_main

    def require_writable_main(session, user_id, itinerary_id):
        checked_sessions.append(session)
        return original(session, user_id, itinerary_id)

    def capture_write(session, flush_context, instances):
        if any(isinstance(row, Expense) for row in session.new):
            write_sessions.append(session)

    monkeypatch.setattr(itinerary_query, "require_writable_main", require_writable_main)
    factory = db_session.get_session_factory()
    event.listen(factory, "before_flush", capture_write)
    try:
        _create(client, dayNo=1, itemId=1)
        assert checked_sessions == write_sessions
        # Trip 1 advertises two days but has no persisted day 2; trip 2 alone has day 3.
        with db_session.session_scope() as session:
            session.add(ItineraryDay(itinerary_id=2, day_no=3))
        for day_no in (2, 3):
            response = client.post(
                "/api/itinerary/1/expenses", json={"category": "food", "amount": "1.00", "dayNo": day_no}
            )
            assert response.status_code == 400
        with db_session.session_scope() as session:
            assert len(session.scalars(select(Expense)).all()) == 1
    finally:
        event.remove(factory, "before_flush", capture_write)


def test_deleted_parent_blocks_expense_writes(client: TestClient) -> None:
    expense = _create(client)
    with db_session.session_scope() as session:
        parent = session.get(ItineraryMain, 1)
        assert parent is not None
        parent.deleted = 1
    path = f"/api/itinerary/expenses/{expense['id']}"
    assert client.put(path, json={"amount": "99.00"}).status_code == 404
    assert client.delete(path).status_code == 404
    with db_session.session_scope() as session:
        row = session.get(Expense, expense["id"])
        assert row is not None
        assert row.amount == Decimal("12.50")
        assert not row.deleted


# ---------- CRUD 与序列化 ----------


def test_create_returns_vo_with_string_amount(client: TestClient) -> None:
    data = _create(
        client,
        category="hotel",
        amount="888.90",
        dayNo=1,
        spentAt="2026-04-01",
        paymentMethod="微信支付",
        note="首晚房费",
    )
    assert data["category"] == "hotel"
    assert data["amount"] == "888.90"
    assert data["currency"] == "CNY"
    assert data["itineraryId"] == 1
    assert data["dayNo"] == 1
    assert data["spentAt"] == "2026-04-01"
    assert data["paymentMethod"] == "微信支付"
    assert data["note"] == "首晚房费"
    assert data["itemId"] is None
    assert data["createdAt"]

    with db_session.session_scope() as session:
        row = session.scalars(select(Expense)).one()
    assert row.amount == Decimal("888.90")


def test_list_groups_totals_by_currency_and_category(client: TestClient) -> None:
    _create(client, category="food", amount="0.10")
    _create(client, category="food", amount="0.20")
    _create(client, category="food", amount="0.20", currency="USD")
    _create(client, category="transport", amount="5.00", currency="USD")

    data = client.get("/api/itinerary/1/expenses").json()["data"]
    assert len(data["expenses"]) == 4
    assert data["totals"] == [
        {"category": "food", "currency": "CNY", "amount": "0.30"},
        {"category": "food", "currency": "USD", "amount": "0.20"},
        {"category": "transport", "currency": "USD", "amount": "5.00"},
    ]


def test_totals_survive_rounding_decimal_sum(client: TestClient) -> None:
    # Decimal 累加不受二进制浮点误差影响：0.1+0.2 必须精确等于 0.30
    for amount in ("0.10", "0.20", "0.30"):
        _create(client, category="other", amount=amount)
    totals = client.get("/api/itinerary/1/expenses").json()["data"]["totals"]
    assert Decimal(totals[0]["amount"]) == Decimal("0.60")


def test_update_changes_fields_and_totals(client: TestClient) -> None:
    created = _create(client, category="food", amount="10.00")
    expense_id = created["id"]

    updated = client.put(
        f"/api/itinerary/expenses/{expense_id}",
        json={"category": "transport", "amount": "2.35"},
    ).json()["data"]
    assert updated["category"] == "transport"
    assert updated["amount"] == "2.35"
    assert updated["currency"] == "CNY", "缺省字段不改"

    totals = client.get("/api/itinerary/1/expenses").json()["data"]["totals"]
    assert totals == [{"category": "transport", "currency": "CNY", "amount": "2.35"}]


def test_delete_soft_deletes_and_excludes_from_totals(client: TestClient) -> None:
    first = _create(client, amount="5.00")
    _create(client, amount="7.00")

    assert client.delete(f"/api/itinerary/expenses/{first['id']}").status_code == 200
    data = client.get("/api/itinerary/1/expenses").json()["data"]
    assert [row["id"] for row in data["expenses"]] != [first["id"]]
    assert data["totals"] == [{"category": "food", "currency": "CNY", "amount": "7.00"}]

    with db_session.session_scope() as session:
        kept = session.scalars(
            select(Expense).where(Expense.id == first["id"]).execution_options(include_deleted=True)
        ).one()
    assert kept.deleted == 1, "记账走软删，行还在库里"


# ---------- 归属与隔离 ----------


def test_other_users_itinerary_is_404(client: TestClient) -> None:
    for response in (
        client.get("/api/itinerary/2/expenses"),
        client.post("/api/itinerary/2/expenses", json={"category": "food", "amount": "1.00"}),
    ):
        assert response.status_code == 404
        assert response.json()["code"] == 404
    assert client.get("/api/itinerary/99999/expenses").status_code == 404


def test_expense_of_other_owner_is_404(client: TestClient) -> None:
    with db_session.session_scope() as session:
        session.add(
            Expense(
                itinerary_id=2,
                user_id=2,
                category="food",
                amount=Decimal("9.99"),
                currency="CNY",
            )
        )
    response = client.put("/api/itinerary/expenses/1", json={"amount": "1.00"})
    assert response.status_code == 404
    assert client.delete("/api/itinerary/expenses/1").status_code == 404


def test_item_must_belong_to_current_itinerary(client: TestClient) -> None:
    with db_session.session_scope() as session:
        foreign_item = session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == 2)).one()

    rejected = client.post(
        "/api/itinerary/1/expenses",
        json={"category": "food", "amount": "3.00", "itemId": foreign_item.id},
    )
    assert rejected.status_code == 400
    assert "不属于该行程" in rejected.json()["message"]

    with db_session.session_scope() as session:
        own_item = session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == 1)).one()
    accepted = client.post(
        "/api/itinerary/1/expenses",
        json={"category": "attraction", "amount": "3.00", "itemId": own_item.id},
    )
    assert accepted.status_code == 200
    assert accepted.json()["data"]["itemId"] == own_item.id


# ---------- 更新路径：有效字段组合的关联校验 ----------


def test_update_validates_effective_day_item_fields(client: TestClient) -> None:
    created = _create(client, dayNo=1)
    expense_id = created["id"]

    # 行程 1 只有 day_no=1：目标日 2 不属于该行程
    rejected = client.put(f"/api/itinerary/expenses/{expense_id}", json={"dayNo": 2})
    assert rejected.status_code == 400
    assert "日期" in rejected.json()["message"]

    with db_session.session_scope() as session:
        own_item = session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == 1)).one()
    moved = client.put(f"/api/itinerary/expenses/{expense_id}", json={"itemId": own_item.id}).json()["data"]
    assert moved["itemId"] == own_item.id and moved["dayNo"] == 1

    # 显式 null 清空 itemId 后，保留的 dayNo 仍然合法
    cleared = client.put(f"/api/itinerary/expenses/{expense_id}", json={"itemId": None}).json()["data"]
    assert cleared["itemId"] is None and cleared["dayNo"] == 1


# ---------- 非法金额/币种（不量化、不静默舍入）----------


@pytest.mark.parametrize(
    "payload",
    [
        {"category": "food", "amount": "0"},
        {"category": "food", "amount": "-1.00"},
        {"category": "food", "amount": "99999999.991"},  # 超两位小数：拒绝而非舍入
        {"category": "food", "amount": "100000000.00"},  # 超出 DECIMAL(10,2) 上界
        {"category": "food", "amount": "12.50", "currency": "cny"},  # 必须 ISO 大写形状
        {"category": "food", "amount": "12.50", "currency": "RMB!"},
        {"category": "snacks", "amount": "12.50"},  # 类目不在白名单
        {"category": "food", "amount": "12.50", "currency": "CN"},  # 币种必须恰好三位大写字母
        {"category": "food", "amount": "12.50", "currency": "USDD"},
    ],
)
def test_invalid_inputs_rejected(client: TestClient, payload: dict) -> None:
    response = client.post("/api/itinerary/1/expenses", json=payload)
    assert response.status_code == 400, response.text
    with db_session.session_scope() as session:
        assert session.scalars(select(Expense)).all() == [], "非法输入不得落库"


def test_update_currency_shape_rejected(client: TestClient) -> None:
    created = _create(client)
    response = client.put(f"/api/itinerary/expenses/{created['id']}", json={"currency": "usd"})
    assert response.status_code == 400


def test_float_amounts_rejected_as_overprecision(client: TestClient) -> None:
    response = client.post(
        "/api/itinerary/1/expenses",
        json={"category": "food", "amount": 10.999},
    )
    assert response.status_code == 400


def test_update_rejects_null_on_non_nullable_and_clears_nullable(client: TestClient) -> None:
    created = _create(
        client,
        dayNo=1,
        itemId=None,
        spentAt="2026-04-01",
        paymentMethod="现金",
        note="带娃门票",
    )
    expense_id = created["id"]

    with db_session.session_scope() as session:
        own_item = session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == 1)).one()
    client.put(f"/api/itinerary/expenses/{expense_id}", json={"itemId": own_item.id, "note": "备注A"})

    # 可空字段显式 null = 清空；未提及字段不动
    updated = client.put(
        f"/api/itinerary/expenses/{expense_id}",
        json={"dayNo": None, "note": None},
    ).json()["data"]
    assert updated["dayNo"] is None
    assert updated["note"] is None
    assert updated["spentAt"] == "2026-04-01", "未提及字段保持原值"
    assert updated["itemId"] == own_item.id

    for field in ("category", "amount", "currency"):
        response = client.put(f"/api/itinerary/expenses/{expense_id}", json={field: None})
        assert response.status_code == 400


def test_empty_update_keeps_everything(client: TestClient) -> None:
    created = _create(client, note="原备注")
    updated = client.put(f"/api/itinerary/expenses/{created['id']}", json={}).json()["data"]
    assert updated == created


# ---------- 详情缓存失效（写路径必须精确失效该行程的详情缓存）----------


def test_writes_evict_detail_cache(client: TestClient, monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(cache_store, "delete", lambda namespace, key: seen.append(f"{namespace}:{key}"))
    created = _create(client, amount="1.00")
    client.put(f"/api/itinerary/expenses/{created['id']}", json={"amount": "2.00"})
    client.delete(f"/api/itinerary/expenses/{created['id']}")
    assert seen == ["itinerary:detail:1:1"] * 3, "创建/更新/删除各失效一次详情缓存"
