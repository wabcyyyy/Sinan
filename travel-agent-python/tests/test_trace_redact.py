"""trace 密钥脱敏回归测试（审计 M1）。

第三方 HTTP 异常原文常含完整请求 URL（?key=、Bearer 头），会经 error=str(exc)
落入持久化 trace 文件。这里验证集中脱敏覆盖 error 字段与 metadata 字符串值。
"""

from app.agent.runtime.trace import record_event, trace_run


def test_error_field_redacts_url_api_key():
    with trace_run("run", "req") as trace:
        record_event(
            "mcp",
            "amap.search_poi",
            status="error",
            error="ConnectError: https://mcp.amap.com/mcp?key=SECRET123&city=hz failed",
        )
    event = trace.to_dict()["events"][0]
    assert "SECRET123" not in event["error"]
    assert "key=***" in event["error"]
    assert "city=hz" in event["error"]  # 非密钥参数保留


def test_error_field_redacts_bearer_token():
    with trace_run("run", "req") as trace:
        record_event(
            "llm",
            "llm.request",
            status="error",
            error="401 Unauthorized, header Authorization: Bearer sk-abcdef.123456",
        )
    event = trace.to_dict()["events"][0]
    assert "sk-abcdef" not in event["error"]
    assert "Bearer ***" in event["error"]


def test_error_field_redacts_client_id():
    """P1-5：Unsplash 的 key 走 client_id 查询参数，trace 落盘同样要抹掉。"""
    with trace_run("run", "req") as trace:
        record_event(
            "tool",
            "unsplash.search",
            status="error",
            error="403 for url 'https://api.unsplash.com/search/photos?query=x&client_id=UNSPLASHKEY123'",
        )
    event = trace.to_dict()["events"][0]
    assert "UNSPLASHKEY123" not in event["error"]
    assert "client_id=***" in event["error"]
    assert "query=x" in event["error"]  # 非密钥参数保留


def test_metadata_string_values_are_redacted():
    with trace_run("run", "req") as trace:
        record_event(
            "tool", "amap.rest", metadata={"url": "https://restapi.amap.com/v3/place/text?key=TOPSECRET&keywords=x"}
        )
    event = trace.to_dict()["events"][0]
    assert "TOPSECRET" not in event["metadata"]["url"]
    assert "key=***" in event["metadata"]["url"]


def test_trace_span_error_redacted_on_exception():
    from app.agent.runtime.trace import trace_span

    with trace_run("run", "req") as trace:
        try:
            with trace_span("tool", "boom"):
                raise RuntimeError("failed calling https://api.x/p?api_key=HIDDENKEY")
        except RuntimeError:
            pass
    event = next(e for e in trace.to_dict()["events"] if e["status"] == "error")
    assert "HIDDENKEY" not in event["error"]
    assert "api_key=***" in event["error"]
