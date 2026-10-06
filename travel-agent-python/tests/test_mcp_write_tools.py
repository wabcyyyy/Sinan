"""SPEC C3.3 MCP 行程读写：写工具经 registry.invoke 的治理与归属校验（SQLite）。

断言焦点：
1. mcp_write 关闭：invoke 按「未注册」拒绝（when 门控调用期实时判定，隐藏而非报禁用）；
2. owner 可查详情 / 重排一天 / 记账 / 查账；他人行程一律工具级失败（不泄漏存在性）；
3. add_expense 走 ExpenseCreate 既有校验（超过两位小数的金额被拒，不静默舍入）；
4. optimize_day 的 day_no → day_id 转换与「日期不存在」错误语义；
5. 鉴权在 ASGI McpGate 层（test_mcp_export 覆盖），工具 handler 只认显式 user_id。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agent import ToolInvocationError, registry
from app.api import mcp as mcp_api  # noqa: F401  导入即完成写工具注册（被测对象）
from app.common import addons as addons_module
from app.common import cache_store
from app.common.config import settings
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.services import user_service


@pytest.fixture()
def env(monkeypatch, tmp_path):
    """SQLite 库 + 内存态 addon（不打真库；开/关即时生效需清 addon 缓存）。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'mcp_write.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    state: dict[str, bool] = {}
    monkeypatch.setattr(addons_module, "_table_exists", lambda: True)
    monkeypatch.setattr(
        addons_module,
        "_read_persisted",
        lambda key: state[key] if key in state else addons_module._env_default(key),
    )
    monkeypatch.setattr(addons_module, "_cache", {})
    _seed()
    yield state
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=hashed, status=1, role="user"))
        session.add(SysUser(username="bob", password=hashed, status=1, role="user"))
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
        for sort_no, name in ((0, "西湖"), (1, "雷峰塔")):
            session.add(
                ItineraryItem(day_id=day.id, itinerary_id=1, item_type="attraction", poi_name=name, sort_no=sort_no)
            )


def _enable(state: dict, key: str, on: bool) -> None:
    state[key] = on
    addons_module._cache.clear()


def test_write_tools_hidden_when_mcp_write_off(env):
    _enable(env, "mcp_write", False)
    with pytest.raises(ToolInvocationError, match="未注册"):
        registry.invoke("get_itinerary_detail", {"user_id": 1, "itinerary_id": 1})


def test_get_itinerary_detail_scoped_to_owner(env):
    _enable(env, "mcp_write", True)
    detail = registry.invoke("get_itinerary_detail", {"user_id": 1, "itinerary_id": 1})
    assert detail["city"] == "杭州"
    # 他人行程：业务闸门 404 → 工具级失败，不泄漏存在性
    with pytest.raises(ToolInvocationError):
        registry.invoke("get_itinerary_detail", {"user_id": 2, "itinerary_id": 1})


def test_optimize_day_converts_day_no(env):
    _enable(env, "mcp_write", True)
    result = registry.invoke("optimize_day", {"user_id": 1, "itinerary_id": 1, "day_no": 1})
    assert result is not None
    with pytest.raises(ToolInvocationError):
        registry.invoke("optimize_day", {"user_id": 1, "itinerary_id": 1, "day_no": 9})


def test_add_and_list_expenses(env):
    _enable(env, "mcp_write", True)
    vo = registry.invoke(
        "add_expense", {"user_id": 1, "itinerary_id": 1, "category": "food", "amount": 88.5, "note": "午餐"}
    )
    assert vo["amount"] == "88.50"
    assert vo["currency"] == "CNY"
    listing = registry.invoke("list_expenses", {"user_id": 1, "itinerary_id": 1})
    assert listing["totals"] == [{"category": "food", "currency": "CNY", "amount": "88.50"}]
    # 他人行程记账：归属闸门拒绝
    with pytest.raises(ToolInvocationError):
        registry.invoke("add_expense", {"user_id": 2, "itinerary_id": 1, "category": "food", "amount": 10})


def test_add_expense_rejects_over_precision_amount(env):
    _enable(env, "mcp_write", True)
    with pytest.raises(ToolInvocationError):
        registry.invoke("add_expense", {"user_id": 1, "itinerary_id": 1, "category": "food", "amount": 1.999})
    # 非法输入不得入库
    listing = registry.invoke("list_expenses", {"user_id": 1, "itinerary_id": 1})
    assert listing["expenses"] == []
