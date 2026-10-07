"""全站 request-id 中间件与 500 兜底日志（审计 §3.5.1 / P0-7）。

口径按 §3.5.1 复核附注修正后的形态钉住：
- 堆栈由 uvicorn error log 打（本文件不测 uvicorn），应用侧负责的是
  logger.exception 的结构化记录与 request_id 关联——日志里的 id 与响应头
  X-Request-ID 同源，多条报障才能归并到同一请求；
- 500 响应经 ServerErrorMiddleware（在全部用户中间件**之外**）直发 ASGI 服务器，
  不经过 RequestIdMiddleware 的 send 包装，所以那个头由 envelope._handle_unexpected
  就地补——这是本文件端到端用例覆盖的关键路径；
- 信封 body 三键 {code,message,data} 原样（契约机检），id 只进头与日志、不进 body。
"""

from __future__ import annotations

import logging
import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.common.envelope import install_exception_handlers
from app.common.request_id import RequestIdMiddleware, request_id_of
from main import API_BODY_MAX_BYTES
from main import app as assembled_app

_REQUEST_ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")


def _mini_app() -> FastAPI:
    """只装「中间件 + 兜底处理器」的最小装配：500 与不覆盖语义不依赖业务路由。"""
    app = FastAPI()
    install_exception_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise ValueError("boom-detail-not-for-clients")

    @app.get("/echo-header")
    def echo_header() -> JSONResponse:
        return JSONResponse(content={"ok": True}, headers={"X-Request-ID": "endpoint-set-id"})

    return app


def test_assembled_app_response_carries_request_id_header() -> None:
    """装配后的 main.app：普通响应与 RequestBodySizeLimit 就地短路的 413 都带头。"""
    client = TestClient(assembled_app)
    first = client.get("/api/test/hello")
    assert first.status_code == 200
    assert _REQUEST_ID_PATTERN.match(first.headers["x-request-id"]), first.headers.get("x-request-id")
    second = client.get("/api/test/hello")
    assert second.headers["x-request-id"] != first.headers["x-request-id"], "每请求生成新 id"

    oversized = client.post("/api/itinerary/clarify", content=b"x" * (API_BODY_MAX_BYTES + 1))
    assert oversized.status_code == 413
    assert _REQUEST_ID_PATTERN.match(oversized.headers["x-request-id"]), "413 短路响应同样要能按 id 归并"


def test_existing_request_id_header_is_not_overridden() -> None:
    """端点已回显的 X-Request-ID（agent 面 generate 的 trace id 既有行为）不覆盖。"""
    client = TestClient(_mini_app())
    response = client.get("/echo-header")
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "endpoint-set-id"


def test_500_fallback_log_and_header_share_request_id(caplog) -> None:
    """500 兜底：logger.exception 带 request_id/method/path 且与响应头同 id；
    信封 body 三键原样、异常细节不回客户端。"""
    client = TestClient(_mini_app(), raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR, logger="app.common.envelope"):
        response = client.get("/boom")
    assert response.status_code == 500
    # 信封 body 形状不动（契约机检）：三键、通用文案、无异常细节
    assert response.json() == {"code": 500, "message": "系统繁忙，请稍后重试", "data": None}
    request_id = response.headers.get("x-request-id")
    assert _REQUEST_ID_PATTERN.match(request_id or ""), (
        "兜底响应头必须带 id（ServerErrorMiddleware 外层直发，不经中间件补头）"
    )

    records = [r for r in caplog.records if r.name == "app.common.envelope" and "unhandled_exception" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert f"request_id={request_id}" in message, "日志与响应头同一 id，报障才能归并"
    assert "method=GET" in message and "path=/boom" in message
    assert records[0].exc_info is not None, "logger.exception 必须携带堆栈（exc_info）"
    assert records[0].exc_info[0] is ValueError


def test_request_id_of_returns_none_without_middleware() -> None:
    """未挂中间件的装配（部分单测的裸 app）：兜底处理器拿不到 id 也不炸（记 '-'）。"""
    assert request_id_of(Request({"type": "http", "headers": []})) is None
