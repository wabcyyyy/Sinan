"""MCP 出口（G-3.6 只读面 + C3.3 行程读写面）：把已注册工具暴露给外部 MCP 客户端。

设计口径：
- **读面**：从 tool_registry 里挑 `read_only=True` 的工具（EXPOSED_TOOLS 白名单；
  写路径中的 apply/delete 类编辑不在此列，外部客户端的结构化修改走下面的写面）；
- **写面（C3.3）**：查行程 / 重排一天 / 记账四件套在本模块就地注册——app.api 层
  import app.services 合法（分层门禁已核），且仍经 `registry.invoke` 派发，预算、
  参数校验、审计与内部调用同一套。`read_only=False`、addon `mcp_write` 门控
  （ToolSpec.when 调用期实时判定）：默认关闭，写能力必须显式打开。工具清单在
  挂载时构建，管理端开开关后需重启进程让清单生效；未重启时调用按"未注册"拒绝。
- **信任模型（C3.3 定案）**：显式 user_id + 业务服务既有 owner/editor 闸门。MCP
  鉴权是单一共享令牌（AGENT_INTERNAL_TOKEN），令牌持有者=全实例信任（自托管
  口径），与读面同一信任级别；不做 OAuth、不做用户映射（卡内否决）。
- **描述与参数 schema 由 ToolSpec 自动生成**：工具的 description 与 parameters
  都取自注册表（ToolSpec 是单一真源），MCP 侧不维护第二份，避免两处漂移；
- **鉴权**：`AGENT_INTERNAL_TOKEN`（Bearer 或 X-Agent-Token）。与 HTTP 直调
  agent 面同一把令牌——不做 OAuth、不做访问策略矩阵（卡内明确否决）；
- **门控**：`require_addon("mcp")`——addon 关闭时整个 /mcp 路径 404（隐藏而非
  403），默认**关闭**（安全默认：出口能力要显式打开）；
- 调用一律经 `registry.invoke`：预算、参数校验、审计事件与内部工具调用同一套，
  不存在"外部调用绕过治理"的第二条路径。

依赖：mcp（FastMCP）、app.agent.tools.registry、app.common.{addons,config,envelope}、
app.services（C3.3 写面）。
"""

from __future__ import annotations

import hmac
import inspect
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from app.agent import ToolInvocationError, ToolSpec, registry
from app.common.addons import addons
from app.common.config import settings
from app.common.envelope import ApiError
from app.schemas.expense import ExpenseCreate
from app.services import day_persistence, expense_service, itinerary_command, itinerary_query

logger = logging.getLogger(__name__)

#: 对外暴露的工具白名单：只读、且对外部消费方有意义的入口。
#: 有意不含 poi_image / attach_poi_images（图片 URL 对本仓无外部价值）与
#: web_search_places（外部调用成本高，且它已由 web_search addon 单独门控）。
EXPOSED_TOOLS: tuple[str, ...] = (
    "search_pois",
    "search_attractions",
    "search_foods",
    "search_hotels",
    "find_nearby_pois",
    "get_route_matrix",
    "get_poi_detail",
    "get_consumption",
)

_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


# ---- 行程读写面（C3.3）：查行程 / 重排一天 / 记账，经 mcp_write addon 门控 ----


def _as_tool_error(exc: ApiError) -> ToolInvocationError:
    """业务 404/400 → 工具级错误：对外统一走"工具失败"通道，不泄漏 HTTP 信封。"""
    return ToolInvocationError(str(exc.message or "工具调用失败"))


def _get_itinerary_detail_handler(user_id: int, itinerary_id: int) -> dict:
    """查行程详情：复用业务面 detail 投影（owner/editor/viewer 闸门在服务内）。"""
    try:
        return itinerary_query.detail(user_id, itinerary_id)
    except ApiError as exc:
        raise _as_tool_error(exc) from exc


def _optimize_day_handler(user_id: int, itinerary_id: int, day_no: int) -> dict:
    """重排一天：day_no → day_id 后走 itinerary_command.optimize_day
    （schedule_optimizer addon 内部兜底 + 写前后版本快照 + 失效 AI 草稿全套纪律）。"""
    try:
        day = day_persistence.find_day(itinerary_id, day_no)
        if day is None:
            raise ToolInvocationError(f"行程 {itinerary_id} 不存在第 {day_no} 天")
        return itinerary_command.optimize_day(user_id, itinerary_id, day.id)
    except ApiError as exc:
        raise _as_tool_error(exc) from exc


def _add_expense_handler(
    user_id: int,
    itinerary_id: int,
    category: str,
    amount: float,
    currency: str | None = None,
    day_no: int | None = None,
    item_id: int | None = None,
    spent_at: str | None = None,
    payment_method: str | None = None,
    note: str | None = None,
) -> dict:
    """记一笔账：金额/币种/类目/日期校验全在 ExpenseCreate（Decimal、两位小数、正数）。

    MCP 入参没有 HTTP 层的 pydantic 预检，这里经 model_validate 走同一套校验后
    才进服务层；构造/校验失败如实转工具级错误，不静默舍入。
    """
    try:
        body = ExpenseCreate.model_validate(
            {
                "category": category,
                "amount": amount,
                "currency": currency or "CNY",
                "dayNo": day_no,
                "itemId": item_id,
                "spentAt": spent_at,
                "paymentMethod": payment_method,
                "note": note,
            }
        )
        return expense_service.create(user_id, itinerary_id, body)
    except ApiError as exc:
        raise _as_tool_error(exc) from exc
    except ValidationError as exc:
        raise ToolInvocationError(f"记账参数不合法：{exc.errors()[0].get('msg', '')}") from exc


def _list_expenses_handler(user_id: int, itinerary_id: int) -> dict:
    """查账目与分类聚合（按 currency+category 分组）。"""
    try:
        return expense_service.list_expenses(user_id, itinerary_id)
    except ApiError as exc:
        raise _as_tool_error(exc) from exc


def _write_spec(
    name: str,
    description: str,
    parameters: dict[str, Any],
    handler: Any,
    *,
    max_calls: int,
    idempotent: bool,
    risk_level: str,
) -> ToolSpec:
    """行程写面 ToolSpec：`when` 在调用期实时判定 mcp_write（关=未注册，隐藏）。"""
    return ToolSpec(
        name=name,
        version="1.0",
        description=description,
        parameters=parameters,
        read_only=False,
        risk_level=risk_level,
        timeout_seconds=10,
        max_calls=max_calls,
        retry_policy={"max_retries": 0},
        requires_confirmation=False,
        idempotent=idempotent,
        handler=handler,
        when=lambda _ctx: addons.is_enabled("mcp_write"),
    )


def _register_itinerary_tools() -> tuple[ToolSpec, ...]:
    """就地注册行程读写工具（导入即注册；handler 调 app.services，分层合法）。"""
    specs = (
        _write_spec(
            name="get_itinerary_detail",
            description="查询指定用户的行程详情（每日安排、预算与账目对照）",
            parameters={
                "type": "object",
                "properties": {"user_id": {"type": "integer"}, "itinerary_id": {"type": "integer"}},
                "required": ["user_id", "itinerary_id"],
                "additionalProperties": False,
            },
            handler=_get_itinerary_detail_handler,
            max_calls=16,
            idempotent=True,
            risk_level="low",
        ),
        _write_spec(
            name="optimize_day",
            description="对行程的某一天做确定性顺序重排（day_no 从 1 计）",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "itinerary_id": {"type": "integer"},
                    "day_no": {"type": "integer"},
                },
                "required": ["user_id", "itinerary_id", "day_no"],
                "additionalProperties": False,
            },
            handler=_optimize_day_handler,
            max_calls=8,
            idempotent=False,
            risk_level="medium",
        ),
        _write_spec(
            name="add_expense",
            description="为行程记一笔实际花费（category: attraction/food/hotel/transport/shopping/other）",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer"},
                    "itinerary_id": {"type": "integer"},
                    "category": {"type": "string"},
                    "amount": {"type": "number"},
                    "currency": {"type": "string"},
                    "day_no": {"type": "integer"},
                    "item_id": {"type": "integer"},
                    "spent_at": {"type": "string"},
                    "payment_method": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["user_id", "itinerary_id", "category", "amount"],
                "additionalProperties": False,
            },
            handler=_add_expense_handler,
            max_calls=16,
            idempotent=False,
            risk_level="medium",
        ),
        _write_spec(
            name="list_expenses",
            description="查询行程账目明细与按币种+类目的聚合",
            parameters={
                "type": "object",
                "properties": {"user_id": {"type": "integer"}, "itinerary_id": {"type": "integer"}},
                "required": ["user_id", "itinerary_id"],
                "additionalProperties": False,
            },
            handler=_list_expenses_handler,
            max_calls=16,
            idempotent=True,
            risk_level="low",
        ),
    )
    for spec in specs:
        registry.register(spec)
    return specs


ITINERARY_TOOL_SPECS: tuple[ToolSpec, ...] = _register_itinerary_tools()


def _py_type(json_type: str) -> type:
    """JSON Schema 类型 → Python 类型（FastMCP 从签名推断，必须给真实类型）。

    不能用 `Any` 占位：FastMCP 会对注解做 `issubclass(annotation, Context)`，
    拿到字符串注解会直接 TypeError（实测）。
    """
    return _JSON_TO_PY.get(json_type, str)


def _signature_from_schema(schema: dict[str, Any]) -> inspect.Signature:
    """按 ToolSpec.parameters 造精确签名（required 无默认、可选默认 None）。"""
    properties: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    parameters: list[inspect.Parameter] = []
    for name, definition in properties.items():
        annotation = _py_type(str((definition or {}).get("type") or "string"))
        if name in required:
            parameters.append(inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation))
        else:
            parameters.append(
                inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=annotation | None, default=None)
            )
    return inspect.Signature(parameters)


def _handler_for(spec: ToolSpec):
    """为 spec 造一个调用期经注册表派发的 handler（预算/校验/审计全覆盖）。"""

    def _call(**params: Any) -> Any:
        try:
            return registry.invoke(spec.name, params)
        except ToolInvocationError as exc:
            # 工具级失败（预算/参数/未注册）如实上抛给 MCP 客户端，不吞成空结果
            raise ValueError(str(exc)) from exc

    _call.__name__ = spec.name
    _call.__doc__ = spec.description
    # 签名按注册表 schema 生成：既让 FastMCP 的类型检查通过，也让"参数长什么样"
    # 由注册表决定（下方还会用 ToolSpec.parameters 覆盖一遍最终 schema）。
    _call.__signature__ = _signature_from_schema(spec.parameters)  # type: ignore[attr-defined]
    return _call


def exposed_specs() -> list[ToolSpec]:
    """当前可暴露的工具（读面白名单 + mcp_write 开启时的行程读写工具）。

    registry.list_specs 已按 when 门控收起被禁能力：mcp_write 关闭时写工具
    在这里自然消失（与读面同一取数口径），无需本函数再查一遍 addon。
    """
    visible = {spec.name: spec for spec in registry.list_specs()}
    exposed = [visible[name] for name in EXPOSED_TOOLS if name in visible]
    exposed.extend(spec for spec in ITINERARY_TOOL_SPECS if spec.name in visible)
    return exposed


def _apply_schema(server: FastMCP, spec: ToolSpec) -> None:
    """把 ToolSpec.parameters 注回 FastMCP 的工具对象（schema 单一真源在注册表）。

    FastMCP 默认从函数签名推断 schema；这里用注册表的 JSON Schema 覆盖，
    保证"对外契约 = 内部治理元数据"（改 ToolSpec 即改对外契约，一处生效）。
    """
    tool = server._tool_manager.get_tool(spec.name)
    if tool is None:
        return
    tool.parameters = spec.parameters


def build_server() -> FastMCP:
    """构建 MCP 服务实例（每次调用新建，便于测试与热更新 addon 状态）。"""
    server = FastMCP(
        name="travel-assistant",
        instructions=(
            "旅行数据工具面：检索本地权威 POI 库、查询同城近邻、估算路线时间矩阵与消费水位；"
            "mcp_write 开启后另提供行程读取、单日重排与记账写入（均需显式传 user_id，"
            "由服务端做归属校验）。数据来自本地知识库与行程库，不访问第三方地理服务。"
        ),
    )
    for spec in exposed_specs():
        server.add_tool(_handler_for(spec), name=spec.name, description=spec.description)
        _apply_schema(server, spec)
    return server


def _authorized(headers: dict[bytes, bytes]) -> bool:
    """`AGENT_INTERNAL_TOKEN` 校验（Bearer 或 X-Agent-Token，二选一）。"""
    expected = settings.agent_internal_token
    if not expected:
        # 未配置令牌时与 HTTP 直调面同口径：仅回环部署可匿名（见 main 的启动校验）。
        # 但写工具例外：MCP 的 user_id 由调用方自备（自托管信任），一旦在没凭据的
        # 端口上开着写入，等于任何人可读写他人行程（R1-8）。
        return not addons.is_enabled("mcp_write")
    raw = (headers.get(b"authorization") or headers.get(b"x-agent-token") or b"").decode("latin-1").strip()
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


class McpGate:
    """ASGI 包装：addon 门控（未开启即 404）+ 令牌校验（不通过即 401）。"""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        if not addons.is_enabled("mcp"):
            await self._reject(send, 404, b"Not Found")
            return
        if not _authorized({k.lower(): v for k, v in (scope.get("headers") or [])}):
            await self._reject(send, 401, b"Unauthorized")
            return
        await self._app(scope, receive, send)

    @staticmethod
    async def _reject(send: Any, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body})


def mcp_asgi_app() -> Any:
    """挂载用 ASGI app（含门控）；addon 状态在每次请求时实时判定。"""
    return McpGate(build_server().streamable_http_app())
