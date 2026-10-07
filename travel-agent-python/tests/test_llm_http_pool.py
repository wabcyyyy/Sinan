"""LLM httpx 共享客户端：复用、关闭与真实 HTTP 故障后的连接回收。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.common import llm_client


def test_shared_client_reused(monkeypatch):
    llm_client.close_http_client()
    a = llm_client._get_http_client()
    b = llm_client._get_http_client()
    assert a is b
    assert not a.is_closed
    llm_client.close_http_client()
    assert llm_client._http_client is None


def test_close_is_idempotent():
    llm_client.close_http_client()
    llm_client.close_http_client()
    c = llm_client._get_http_client()
    assert c is not None
    llm_client.close_http_client()


@pytest.fixture
def loopback_llm(monkeypatch):
    """真 TCP、容量 1 的 httpx 池；MockTransport 不实施连接容量，无法验证 PoolTimeout。"""
    state = {"mode": "healthy"}
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            mode = state["mode"]
            if mode == "error":
                self.send_error(503)
                return
            if mode == "stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for content in ("first", "second"):
                        row = json.dumps({"choices": [{"delta": {"content": content}}]})
                        self.wfile.write(f"data: {row}\n\n".encode())
                        self.wfile.flush()
                    release.wait(5)
                except OSError:
                    pass
                self.close_connection = True
                return
            body = json.dumps({"choices": [{"message": {"content": "recovered"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    http = httpx.Client(limits=httpx.Limits(max_connections=1), timeout=0.5, trust_env=False)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: http)
    monkeypatch.setattr(llm_client, "_build_timeout", lambda read: httpx.Timeout(0.5))
    client = llm_client.LLMClient(
        base_url=f"http://127.0.0.1:{server.server_port}",
        api_key="fixture",
        model="fixture",
        max_attempts=1,
    )
    yield client, state
    release.set()
    http.close()
    server.shutdown()
    server.server_close()
    thread.join(2)


@pytest.mark.parametrize("failure", ["server_error", "read_timeout", "cancel", "early_close", "pool_saturation"])
def test_shared_pool_recovers_without_reset_after_http_failure(loopback_llm, failure):
    client, state = loopback_llm
    messages = [{"role": "user", "content": "ping"}]
    if failure == "server_error":
        state["mode"] = "error"
        with pytest.raises(httpx.HTTPStatusError):
            client.chat(messages)
    else:
        state["mode"] = "stream"
        cancel = threading.Event()
        stream = client.stream_chat_deltas(messages, cancel=cancel)
        try:
            assert next(stream) == "first"
            if failure == "cancel":
                cancel.set()
                with pytest.raises(llm_client.StreamCancelled):
                    next(stream)
            elif failure == "read_timeout":
                with pytest.raises(httpx.ReadTimeout):
                    list(stream)
            elif failure == "pool_saturation":
                with pytest.raises(httpx.PoolTimeout):
                    client.chat(messages)
        finally:
            stream.close()
    # 同一个实例、没有 reset；释放/超时后，容量 1 的池必须立即接受健康请求。
    state["mode"] = "healthy"
    assert client.chat(messages) == "recovered"
