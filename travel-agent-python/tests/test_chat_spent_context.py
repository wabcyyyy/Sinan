"""SPEC C3.4 记账 × AI:实际花费进入对话上下文与局部重排约束(SQLite,无网络)。

断言焦点:
1. chat_body.spent_* 只聚合与预算同币种(CNY)的账目,其他币种仅列币种码;
2. 无账目时三个 spent 字段全空,prompt 形状与历史零漂移;
3. spent 出现在给模型的计划 JSON 顶层、不进受保护的 trip 元数据块;
4. local_replan 同时收到 budget 与 spent 时以剩余预算为约束,超支钳 0,只给其一维持原语义。
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent.editing.chat_draft.document import _trip_plan_document
from app.agent.editing.local_replan import run_local_replan
from app.common import cache_store
from app.common.config import settings
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryMain, SysUser
from app.schemas.expense import ExpenseCreate
from app.schemas.trip import ChatTurnRequest, LocalReplanRequest
from app.services import expense_service, user_service
from app.services.itinerary_chat import build_chat_turn_context


@pytest.fixture()
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'chat_spent.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
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
        session.add(
            ItineraryMain(
                user_id=1,
                title="杭州2日游",
                city="杭州",
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 2),
                days=2,
                persons=1,
                budget=1000,
                status=2,
            )
        )
        session.flush()
        session.add(ItineraryDay(itinerary_id=1, day_no=1, generation_status="SUCCEEDED"))


def test_spent_summary_aggregates_cny_only_and_lists_other_currencies(db):
    expense_service.create(1, 1, ExpenseCreate(category="attraction", amount=Decimal("100.00")))
    expense_service.create(1, 1, ExpenseCreate(category="food", amount=Decimal("50.50")))
    expense_service.create(1, 1, ExpenseCreate(category="shopping", amount=Decimal("12.34"), currency="USD"))
    ctx = build_chat_turn_context(1, 1, "超支了,重排一下", [])
    body = ctx.chat_body
    assert body["spent_total"] == 150.5
    assert body["spent_by_category"] == {"attraction": 100.0, "food": 50.5}
    assert body["spent_other_currencies"] == ["USD"]
    # 计划口径字段不受实际花费影响(C3.4 禁止混淆两者语义)
    assert body["current_total"] == 0.0
    assert body["budget"] == 1000.0


def test_no_expenses_leaves_chat_body_without_spent(db):
    ctx = build_chat_turn_context(1, 1, "换个节奏", [])
    assert ctx.chat_body["spent_total"] is None
    assert ctx.chat_body["spent_by_category"] is None
    assert ctx.chat_body["spent_other_currencies"] is None


def test_trip_document_surfaces_spent_at_top_level(db):
    expense_service.create(1, 1, ExpenseCreate(category="attraction", amount=Decimal("88.00")))
    ctx = build_chat_turn_context(1, 1, "超支了吗", [])
    req = ChatTurnRequest.model_validate(ctx.chat_body)
    document = _trip_plan_document(req)
    assert document["spent"] == {
        "total": 88.0,
        "by_category": {"attraction": 88.0},
        "other_currencies": None,
    }
    # spent 不在受保护的 trip 元数据块里:模型不得也不需要回显它
    assert "spent" not in document["trip"]


def test_trip_document_without_ledger_has_no_spent_key():
    req = ChatTurnRequest(city="杭州", days=2, message="换个节奏")
    assert "spent" not in _trip_plan_document(req)


def _item(name, kind="attraction", start="09:00", end="10:00"):
    return {
        "item_type": kind,
        "poi_id": name,
        "poi_name": name,
        "latitude": 30.0,
        "longitude": 120.0,
        "start_time": start,
        "end_time": end,
        "duration_min": 60,
    }


def test_local_replan_spent_shrinks_budget_limit():
    result = run_local_replan(
        LocalReplanRequest(
            city="杭州",
            affected_day_nos=[1],
            plans=[{"day_no": 1, "items": [_item("A")]}],
            budget=100.0,
            spent=30.0,
        )
    )
    assert result["budget_limit"] == 70.0


def test_local_replan_spent_over_budget_clamps_limit_to_zero():
    result = run_local_replan(
        LocalReplanRequest(
            city="杭州",
            affected_day_nos=[1],
            plans=[{"day_no": 1, "items": [_item("A")]}],
            budget=100.0,
            spent=180.0,
        )
    )
    assert result["budget_limit"] == 0.0


def test_local_replan_without_spent_keeps_budget_semantics():
    plans = [{"day_no": 1, "items": [_item("A")]}]
    result = run_local_replan(
        LocalReplanRequest(city="杭州", affected_day_nos=[1], plans=deepcopy(plans), budget=100.0)
    )
    assert result["budget_limit"] == 100.0
    result = run_local_replan(LocalReplanRequest(city="杭州", affected_day_nos=[1], plans=deepcopy(plans), spent=30.0))
    # 只有 spent 没有预算时无从谈剩余,约束保持空
    assert result["budget_limit"] is None
