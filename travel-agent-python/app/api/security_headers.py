"""全站响应头：补齐"同源内容不会被当成脚本执行"的最低一层（R1-3）。

为什么手写纯 ASGI 中间件而不是 `@app.middleware("http")`：后者是
`BaseHTTPMiddleware`，它会包装响应流；本服务对外提供 SSE（`/api/itinerary/{id}/events`），
在流式链路上多包一层历史上就出过取消与缓冲问题。这里只是在
`http.response.start` 上追加三个头，不碰 body、不分块、不缓冲。

本轮只上"无争议三件套"（D 项裁决）：
- `X-Content-Type-Options: nosniff`——别让浏览器替我们把图片响应嗅成 HTML；
- `X-Frame-Options: DENY` + CSP `frame-ancestors 'none'`——同源内容一旦被投毒，
  不能再被别人的页面 iframe 进去执行（这正是匿名图片代理吐出 SVG 的接管路径）。
完整 CSP（default/img/connect/worker-src）在 R7 单独一批，且必须真机实测后才合，
因为它同时约束 MapLibre 的 worker/blob/瓦片来源。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.types import Message, Receive, Scope, Send

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("content-security-policy", "frame-ancestors 'none'"),
)

_ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class SecurityHeadersMiddleware:
    """给每个 HTTP 响应补上缺失的安全头；已存在的头不覆盖（上游可能是导出下载）。"""

    def __init__(self, app: _ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {str(name, "latin-1").lower() for name, _value in headers}
                for name, value in SECURITY_HEADERS:
                    if name not in present:
                        headers.append((name.encode("latin-1"), value.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self._app(scope, receive, send_with_headers)
