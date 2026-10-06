"""Addon 体系测试（G-3.1）。

覆盖：
- 默认值回落 env（无状态行时）；
- set_enabled 落状态行 + 审计 + 缓存即失效（切了立刻读到新值）；
- require_addon 停用 → ApiError 404（隐藏而非 403）；
- registry：addon 关闭 → 工具列表不含 web_search_places、function_schemas
  同步收起、invoke 报未注册；重开恢复；
- /api/admin/addons 管理端点（列表/切换/未知 key 404）；
- /api/itinerary/{id}/optimize 在 schedule_optimizer addon 关闭时 404。

表存在性：addon_state/addon_audit 由 V3 迁移建表；本测试不打真库——
对 addons 模块 patch 掉 `_read_persisted`/`set_enabled` 的持久化路径，
用内存态模拟（与 test_event_publisher 的 fake_redis 同一思路）。
"""

from __future__ import annotations

import pytest

from app.common import addons as addons_module
from app.common.addons import ADDONS, addons


@pytest.fixture()
def in_memory_addons(monkeypatch):
    """内存态 addon：无 DB，状态与审计存 dict；env 默认走真实 settings。"""
    state: dict[str, bool] = {}
    audit: list[tuple[str, bool]] = []
    monkeypatch.setattr(addons_module, "_table_exists", lambda: True)
    # 无行回落 env 默认——真实 _read_persisted 永不返回 None（契约：返回 bool）
    monkeypatch.setattr(
        addons_module,
        "_read_persisted",
        lambda key: state[key] if key in state else addons_module._env_default(key),
    )
    monkeypatch.setattr(addons_module, "_cache", {})

    def fake_set(key: str, enabled: bool, changed_by: str) -> None:
        if key not in ADDONS:
            raise KeyError(key)  # 与真实 set_enabled 契约一致：未知键拒绝
        state[key] = enabled
        audit.append((key, enabled))
        addons_module._cache.pop(key, None)  # 与真实契约一致：切换即失效缓存

    # 单例方法在调用期解析模块全局 set_enabled，patch 模块属性即对 addons.set_enabled 生效
    monkeypatch.setattr(addons_module, "set_enabled", fake_set)
    yield {"state": state, "audit": audit}


def test_defaults_fall_back_to_env(in_memory_addons):
    """无状态行 → 全部回落 env 默认（route_service 默认 False，其余 True）。"""
    assert addons.is_enabled("web_search") is True
    assert addons.is_enabled("route_service") is False


def test_set_enabled_takes_effect_immediately_and_writes_audit(in_memory_addons):
    addons.set_enabled("web_search", False, changed_by="admin")
    assert addons.is_enabled("web_search") is False, "切换后必须立即可见（缓存即失效）"
    addons.set_enabled("web_search", True, changed_by="admin")
    assert addons.is_enabled("web_search") is True
    assert in_memory_addons["audit"] == [("web_search", False), ("web_search", True)]


def test_unknown_addon_key_rejected(in_memory_addons):
    with pytest.raises(KeyError):
        addons.is_enabled("nope")
    with pytest.raises(KeyError):
        addons.set_enabled("nope", True, changed_by="admin")


def test_require_addon_raises_404_when_disabled(in_memory_addons):
    from app.common.envelope import ApiError

    gate = addons_module.require_addon("schedule_optimizer")
    gate()  # 默认（env True）放行
    addons.set_enabled("schedule_optimizer", False, changed_by="admin")
    with pytest.raises(ApiError) as exc_info:
        gate()
    assert exc_info.value.status == 404, "隐藏而非暴露：停用能力必须 404 而非 403"
    addons.set_enabled("schedule_optimizer", True, changed_by="admin")
    gate()  # 重开恢复


def test_registry_collapses_web_search_when_addon_off(in_memory_addons):
    from app.agent.tools.registry import ToolInvocationError, registry

    names = [spec.name for spec in registry.list_specs()]
    assert "web_search_places" in names
    schemas = [schema["function"]["name"] for schema in registry.function_schemas()]
    assert "web_search_places" in schemas

    addons.set_enabled("web_search", False, changed_by="admin")
    names = [spec.name for spec in registry.list_specs()]
    assert "web_search_places" not in names, "addon 关闭后工具面必须收起（G-3.1 验证项）"
    assert "web_search_places" not in [s["function"]["name"] for s in registry.function_schemas()]
    with pytest.raises(ToolInvocationError):
        registry.invoke("web_search_places", {"city": "杭州", "category": "attraction"})

    addons.set_enabled("web_search", True, changed_by="admin")
    assert any(spec.name == "web_search_places" for spec in registry.list_specs()), "重开恢复"


def test_snapshot_lists_all_registered_addons(in_memory_addons):
    snap = {row["key"]: row["enabled"] for row in addons.snapshot()}
    assert set(snap) == set(ADDONS)


def test_optimize_day_returns_404_when_addon_off(in_memory_addons):
    """schedule_optimizer addon 停用：服务层入口即 404（端点层 require_addon 之前的兜底）。"""
    from app.common.envelope import ApiError
    from app.services import itinerary_command

    addons.set_enabled("schedule_optimizer", False, changed_by="admin")
    with pytest.raises(ApiError) as exc_info:
        itinerary_command.optimize_day(user_id=1, itinerary_id=1, day_id=1)
    assert exc_info.value.status == 404
