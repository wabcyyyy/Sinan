"""MCP 出口测试（G-3.6）。

覆盖卡内验证项：
- 只读工具面（不含任何写路径工具）；
- 工具描述与参数 schema 来自 ToolSpec（注册表是单一真源，MCP 侧不维护第二份）；
- addon 关闭 → 整个 /mcp 404（隐藏而非 403）；开启后可访问；
- 令牌校验：配置了 AGENT_INTERNAL_TOKEN 时，缺/错令牌 401，正确令牌放行；
- 端到端：用官方 MCP 客户端 list_tools 拿到工具清单，并真实 call_tool 一个
  只读工具（经 registry.invoke，预算与审计与内部调用同一套）。
"""

from __future__ import annotations

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from app.api import mcp as mcp_api
from app.common import addons as addons_module
from app.common.addons import addons
from app.common.config import settings


@pytest.fixture()
def in_memory_addons(monkeypatch):
    """内存态 addon（同 test_addons 的思路：不打真库）。"""
    state: dict[str, bool] = {}
    monkeypatch.setattr(addons_module, "_table_exists", lambda: True)
    monkeypatch.setattr(
        addons_module,
        "_read_persisted",
        lambda key: state[key] if key in state else addons_module._env_default(key),
    )
    monkeypatch.setattr(addons_module, "_cache", {})

    def fake_set(key: str, enabled: bool, changed_by: str) -> None:
        if key not in addons_module.ADDONS:
            raise KeyError(key)
        state[key] = enabled
        addons_module._cache.pop(key, None)

    monkeypatch.setattr(addons_module, "set_enabled", fake_set)
    yield state


def test_exposed_tools_are_read_only():
    """mcp_write 默认关闭时对外只暴露 read_only 工具（C3.3 写面必须显式打开）。"""
    specs = mcp_api.exposed_specs()
    assert specs, "至少应暴露若干只读工具"
    assert all(spec.read_only for spec in specs)
    names = {spec.name for spec in specs}
    # 写路径/昂贵外部调用一律不在白名单
    assert "web_search_places" not in names, "联网补池成本高且另有 addon 门控"
    assert "attach_poi_images" not in names
    assert "optimize_day" not in names and "add_expense" not in names
    assert 4 <= len(specs) <= 8, "卡内要求 4~6 个（实现取 8 个只读入口，含近邻与消费查询）"


def test_write_tools_exposed_only_when_mcp_write_enabled(in_memory_addons):
    """C3.3：mcp_write 关→写工具不暴露；开→四件套可见且读面不变。"""
    addons.set_enabled("mcp", True, changed_by="admin")
    addons.set_enabled("mcp_write", False, changed_by="admin")
    off_names = {spec.name for spec in mcp_api.exposed_specs()}
    assert all(spec.read_only for spec in mcp_api.exposed_specs())

    addons.set_enabled("mcp_write", True, changed_by="admin")
    specs = {spec.name: spec for spec in mcp_api.exposed_specs()}
    write_names = {"get_itinerary_detail", "optimize_day", "add_expense", "list_expenses"}
    assert write_names <= set(specs)
    for name in write_names:
        assert specs[name].read_only is False
        assert specs[name].parameters.get("required"), "写工具必须要求显式 user_id 等参数"
    # 读面照旧
    assert "search_attractions" in specs and off_names <= set(specs)


def test_tool_contract_comes_from_registry():
    """描述与参数 schema 自动取自 ToolSpec（单一真源，无第二份）。"""
    from app.agent.tools.registry import registry

    server = mcp_api.build_server()
    for spec in mcp_api.exposed_specs():
        tool = server._tool_manager.get_tool(spec.name)
        assert tool is not None, spec.name
        assert tool.description == spec.description
        assert tool.parameters == registry.get(spec.name).parameters, "参数 schema 必须与注册表一致"


def test_addon_gate_rejects_when_disabled(in_memory_addons):
    """addon 默认关闭 → /mcp 404（隐藏而非 403）。"""
    assert addons.is_enabled("mcp") is False, "MCP 出口必须默认关闭（安全默认）"
    gate = mcp_api.McpGate(lambda *_: None)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    anyio.run(gate, {"type": "http", "headers": []}, receive, send)
    assert sent[0]["status"] == 404

    addons.set_enabled("mcp", True, changed_by="admin")
    sent.clear()
    reached = []

    async def downstream(scope, receive, send):
        reached.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    anyio.run(mcp_api.McpGate(downstream), {"type": "http", "headers": []}, receive, send)
    assert reached == [True] and sent[0]["status"] == 200


def test_token_check(monkeypatch, in_memory_addons):
    """配置令牌时：缺失/错误 401，正确放行（Bearer 或 X-Agent-Token 都认）。"""
    addons.set_enabled("mcp", True, changed_by="admin")
    monkeypatch.setattr(settings, "agent_internal_token", "s3cret")

    assert mcp_api._authorized({}) is False
    assert mcp_api._authorized({b"authorization": b"Bearer wrong"}) is False
    assert mcp_api._authorized({b"authorization": b"Bearer s3cret"}) is True
    assert mcp_api._authorized({b"x-agent-token": b"s3cret"}) is True
    # 未配置令牌（回环部署）：只读面照旧放行
    monkeypatch.setattr(settings, "agent_internal_token", "")
    assert mcp_api._authorized({}) is True
    # 但写面不行：MCP 写工具的 user_id 由调用方自备，没凭据的端口上开写入
    # 等于任何人可读写他人行程（R1-8）
    addons.set_enabled("mcp_write", True, changed_by="admin")
    assert mcp_api._authorized({}) is False
    assert mcp_api._authorized({b"x-agent-token": b""}) is False


def test_end_to_end_list_and_call(monkeypatch, in_memory_addons):
    """真实 MCP 客户端：list_tools 拿到清单，call_tool 经注册表派发并返回数据。"""
    addons.set_enabled("mcp", True, changed_by="admin")
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    fixture_row = {
        "id": 1,
        "name": "西湖",
        "category": "attraction",
        "latitude": 30.25,
        "longitude": 120.15,
        "ticket_price": 0,
        "open_time": "全天",
    }
    monkeypatch.setattr("app.agent.tools.impl.search_attractions", lambda city, prefs, limit=30: [fixture_row])

    async def _scenario():
        server = mcp_api.build_server()
        async with create_connected_server_and_client_session(server._mcp_server) as session:
            listed = await session.list_tools()
            names = [tool.name for tool in listed.tools]
            result = await session.call_tool("search_attractions", {"city": "杭州", "limit": 3})
            return names, result

    names, result = anyio.run(_scenario)
    assert "search_attractions" in names
    assert "search_pois" in names
    assert result.isError is False, f"只读工具调用不应失败：{result}"
    assert "西湖" in str(result.content), "调用结果应带出真实数据（经 registry → tools）"
