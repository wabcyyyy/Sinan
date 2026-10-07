"""请求体大小预检中间件（P0-4 应用侧）：/api/** 超限 413，body 绝不读进内存。

两层口径的另一半在边缘：deploy/Caddyfile 的 request_body 与
travel-frontend-react/nginx.conf 的 client_max_body_size（/api/** 2MB、上传端点
6MB/9MB 档，三处互为镜像）。这里的用例钉住应用侧自己的语义：
- 只看 Content-Length，chunked（无 Content-Length）放行交边缘兜底；
- 命中超限时直接回 413 信封，不调下游 app、不碰 receive（不读 body）；
- GET/SSE 无请求体，天然不受影响；
- 上传端点按"服务侧文件本体上限 + 1MB multipart 余量"分档：封面上传（数字段
  {id}）与图片意图 6MB、语音意图 9MB，其余 /api/** 一律 2MB。
"""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from starlette.types import Message

from main import (
    API_BODY_MAX_BYTES,
    SPEECH_UPLOAD_BODY_MAX_BYTES,
    UPLOAD_BODY_MAX_BYTES,
    RequestBodySizeLimitMiddleware,
)


class _SentinelApp:
    """下游占位：记录自己是否被调用；被调用即证明请求穿过了中间件。"""

    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _scope(method: str, path: str, headers: list[tuple[str, str]]) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "2.1"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "root_path": "",
        "headers": [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers],
        "client": ("127.0.0.1", 60000),
        "server": ("testserver", 80),
    }


def _run(middleware: RequestBodySizeLimitMiddleware, scope: dict, *, body_must_be_read: bool) -> list[Message]:
    """驱动一次 ASGI 调用，返回发出的响应消息（下游是否被调用由调用方的占位自查）。

    body_must_be_read=False 时 receive 一旦被调即视为失败——超限请求的 body 必须
    原封不动地留在链路上（这正是本中间件存在的意义：不缓冲整包）。
    """

    async def receive() -> dict:
        if not body_must_be_read:
            raise AssertionError("中间件不应读取请求体（只做 Content-Length 预检）")
        return {"type": "http.request", "body": b"x", "more_body": False}

    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    asyncio.run(middleware(scope, receive, send))
    return messages


def _post(path: str, content_length: int) -> dict:
    return _scope("POST", path, [("content-type", "application/json"), ("content-length", str(content_length))])


def test_oversized_api_body_is_413_without_reading_body_or_calling_downstream() -> None:
    sentinel = _SentinelApp()
    middleware = RequestBodySizeLimitMiddleware(sentinel)
    messages = _run(middleware, _post("/api/itinerary/clarify", API_BODY_MAX_BYTES + 1), body_must_be_read=False)
    assert not sentinel.called, "超限请求不应到达下游应用"
    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 413
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    envelope = json.loads(body.decode("utf-8"))
    assert envelope["code"] == 413 and envelope["data"] is None
    headers = {name: value for name, value in start["headers"]}
    assert headers[b"content-type"] == b"application/json"


def test_at_limit_passes_and_cover_upload_tier_is_wider() -> None:
    sentinel = _SentinelApp()
    middleware = RequestBodySizeLimitMiddleware(sentinel)
    # 恰等于 2MB：放行（上限含边界，与边缘 max_size 语义一致）
    _run(middleware, _post("/api/itinerary/clarify", API_BODY_MAX_BYTES), body_must_be_read=True)
    assert sentinel.called
    # 封面上传（数字段 id）走 6MB 档：6MB 放行、超 1 字节拒绝
    sentinel.called = False
    _run(middleware, _post("/api/itinerary/7/cover/upload", UPLOAD_BODY_MAX_BYTES), body_must_be_read=True)
    assert sentinel.called
    sentinel.called = False
    messages = _run(
        middleware, _post("/api/itinerary/7/cover/upload", UPLOAD_BODY_MAX_BYTES + 1), body_must_be_read=False
    )
    assert not sentinel.called
    assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 413


def test_image_and_speech_intent_tiers_cover_2_to_5mb_uploads() -> None:
    """图片意图（服务侧 5MB）与语音意图（服务侧 8MB）不在 2MB 档：回归防护——
    豁免清单漏掉它们时，2-5MB 的手机照片与长录音会从可用变 413。"""
    sentinel = _SentinelApp()
    middleware = RequestBodySizeLimitMiddleware(sentinel)
    # 3MB 的图片（2MB 档之外、6MB 档之内）：必须放行
    _run(middleware, _post("/api/image-intent", API_BODY_MAX_BYTES + 1024 * 1024), body_must_be_read=True)
    assert sentinel.called
    # 6MB 边界放行、超 1 字节拒绝
    sentinel.called = False
    _run(middleware, _post("/api/image-intent", UPLOAD_BODY_MAX_BYTES), body_must_be_read=True)
    assert sentinel.called
    sentinel.called = False
    messages = _run(middleware, _post("/api/image-intent", UPLOAD_BODY_MAX_BYTES + 1), body_must_be_read=False)
    assert not sentinel.called
    assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 413
    # 语音意图 9MB 档：8MB 录音放行（multipart 包裹后仍在 9MB 内）、超 1 字节拒绝
    sentinel.called = False
    _run(middleware, _post("/api/speech-intent", SPEECH_UPLOAD_BODY_MAX_BYTES), body_must_be_read=True)
    assert sentinel.called
    sentinel.called = False
    messages = _run(middleware, _post("/api/speech-intent", SPEECH_UPLOAD_BODY_MAX_BYTES + 1), body_must_be_read=False)
    assert not sentinel.called
    assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 413


def test_cover_upload_tier_requires_numeric_segment() -> None:
    """非数字段（不是真实路由形态）不享 6MB 档：仍按 /api/** 的 2MB 拒绝。"""
    sentinel = _SentinelApp()
    middleware = RequestBodySizeLimitMiddleware(sentinel)
    messages = _run(
        middleware, _post("/api/itinerary/not-an-id/cover/upload", API_BODY_MAX_BYTES + 1), body_must_be_read=False
    )
    assert not sentinel.called
    assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 413


def test_chunked_without_content_length_passes_to_edge_backstop() -> None:
    """无 Content-Length（chunked）不在此拦——由边缘 request_body/client_max_body_size 兜底。"""
    sentinel = _SentinelApp()
    middleware = RequestBodySizeLimitMiddleware(sentinel)
    scope = _scope("POST", "/api/itinerary/clarify", [("content-type", "application/json")])
    _run(middleware, scope, body_must_be_read=True)
    assert sentinel.called


def test_get_and_non_api_paths_are_unaffected() -> None:
    sentinel = _SentinelApp()
    middleware = RequestBodySizeLimitMiddleware(sentinel)
    # GET 无请求体：不拦（SSE 的 /events 同为 GET）
    _run(middleware, _scope("GET", "/api/itinerary/1/events", []), body_must_be_read=True)
    assert sentinel.called
    # 中间件口径只覆盖 /api/**：/mcp 与静态路径不经此判（边缘有各自的门）
    sentinel.called = False
    _run(middleware, _post("/mcp", API_BODY_MAX_BYTES + 1), body_must_be_read=True)
    assert sentinel.called
    # 非_http scope（lifespan）原样透传给下游，不走 HTTP 分支
    sentinel.called = False

    async def _noop_receive() -> dict:
        return {"type": "lifespan.startup"}

    async def _noop_send(_message: Message) -> None:
        return None

    asyncio.run(middleware({"type": "lifespan"}, _noop_receive, _noop_send))
    assert sentinel.called, "lifespan 等非 HTTP scope 必须原样透传"


def test_assembled_app_enforces_limit_end_to_end() -> None:
    """装配后的 main.app：真实 Content-Length 链路 413；未超限与 GET 照常到路由。"""
    import main

    client = TestClient(main.app)
    oversized = client.post("/api/test/hello", content=b"x" * (API_BODY_MAX_BYTES + 1))
    assert oversized.status_code == 413
    assert oversized.json()["code"] == 413
    # 小体积请求必须能走到路由层（/api/test/hello 只注册了 GET，POST 到达路由即 405）
    assert client.post("/api/test/hello", content=b"{}").status_code == 405
    # GET（探活/SSE 形态）不受影响
    assert client.get("/api/test/hello").status_code == 200


def test_assembled_app_cover_upload_tier_end_to_end() -> None:
    import main

    client = TestClient(main.app)
    # 2MB < 6MB：穿过后被鉴权拦（401），证明没被 2MB 一刀切拦在中间件
    mid = client.post("/api/itinerary/7/cover/upload", content=b"x" * (API_BODY_MAX_BYTES + 1))
    assert mid.status_code == 401
    # 超 6MB：中间件 413（先于鉴权，边缘与应用侧谁先拦都是 413）
    over = client.post("/api/itinerary/7/cover/upload", content=b"x" * (UPLOAD_BODY_MAX_BYTES + 1))
    assert over.status_code == 413


def test_assembled_app_intent_tiers_end_to_end() -> None:
    """装配后的 main.app：图片/语音意图端点的 2-5MB 上传不再被 2MB 一刀切拦成 413
    （回归防护，豁免面与边缘 Caddy/nginx matcher 同步），超各自档位仍是 413。"""
    import main

    client = TestClient(main.app)
    # 3MB 图片（在 2MB 档之外）：穿过中间件、被鉴权拦 401
    image_mid = client.post("/api/image-intent", content=b"x" * (API_BODY_MAX_BYTES + 1024 * 1024))
    assert image_mid.status_code == 401
    # 超 6MB 档：413
    image_over = client.post("/api/image-intent", content=b"x" * (UPLOAD_BODY_MAX_BYTES + 1))
    assert image_over.status_code == 413
    # 8MB 录音（multipart 包裹后在 9MB 档内）：穿过中间件、被鉴权拦 401
    speech_mid = client.post("/api/speech-intent", content=b"x" * (SPEECH_UPLOAD_BODY_MAX_BYTES - 1024 * 1024))
    assert speech_mid.status_code == 401
    # 超 9MB 档：413
    speech_over = client.post("/api/speech-intent", content=b"x" * (SPEECH_UPLOAD_BODY_MAX_BYTES + 1))
    assert speech_over.status_code == 413
