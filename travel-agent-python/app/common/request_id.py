"""全站请求关联 id（审计 §3.5.1 / P0-7）。

复核附注修正后的真实残余问题：未捕获异常的堆栈 uvicorn 会打（starlette 注册
Exception handler 后仍 `raise`），但堆栈、access log 行、客户端拿到的信封三者
之间**没有关联键**——多条报障无法归并到同一请求。本中间件给每个 HTTP 请求生成
短 id，写进 `scope["state"]`（兜底处理器等就地可取）并回写 `X-Request-ID`
响应头；500 兜底日志与响应头带同一 id 的部分在 `app/common/envelope.py`。

实现口径与 `SecurityHeadersMiddleware` 一致：纯 ASGI、只在
`http.response.start` 追加头，不包装响应流、不经 BaseHTTPMiddleware——
SSE 链路不多一层缓冲。已存在的 `X-Request-ID` 不覆盖（agent 面 /v1/generate
按 trace request_id 回显的既有行为保留，见 tests/test_api_observability.py）。
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from starlette.requests import Request
from starlette.types import Message, Receive, Scope, Send

_ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

#: 响应头键（wire 小写；agent 面既有端点同款，见 app/api/agent.py 的 generate）。
REQUEST_ID_HEADER = "x-request-id"

#: scope state 里的键名；`request_id_of(request)` 是唯一取用入口。
_STATE_KEY = "request_id"


def new_request_id() -> str:
    """短 id：12 位十六进制（48 bit 熵）——足够按日志归并，又不至于刷屏。"""
    return secrets.token_hex(6)


def request_id_of(request: Request) -> str | None:
    """当前请求的关联 id（中间件注入）；无中间件的装配（部分单测的裸 app）返回 None。"""
    return getattr(request.state, _STATE_KEY, None)


class RequestIdMiddleware:
    """每请求生成短 id：注入 scope state + 回写 X-Request-ID 响应头。"""

    def __init__(self, app: _ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        request_id = new_request_id()
        # Request 是 scope 的薄视图：这里写入 scope["state"]，路由层/兜底处理器
        # 手里的 Request 共享同一 scope dict，因此能读到同一个 id。
        Request(scope).state.request_id = request_id

        async def send_with_request_id(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {str(name, "latin-1").lower() for name, _value in headers}
                if REQUEST_ID_HEADER not in present:
                    headers.append((REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("ascii")))
                    message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_request_id)
