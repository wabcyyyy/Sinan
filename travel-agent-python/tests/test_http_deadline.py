"""真实 socket 上的卡住/滴流响应必须被总截止期打断，不能等待读超时。"""

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.agent.runtime.run_limits import RunLimitExceeded, RunLimits
from app.common.http_deadline import bounded_chunks, bounded_lines


@pytest.mark.parametrize("reader", [bounded_chunks, bounded_lines])
@pytest.mark.parametrize("trickle", [False, True])
def test_absolute_deadline_interrupts_socket_read(reader, trickle):
    stop = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b"data: partial")
            self.wfile.flush()
            while not stop.wait(0.04):
                if trickle:
                    try:
                        self.wfile.write(b"x")  # 永远不出现完整 SSE 行
                        self.wfile.flush()
                    except OSError:
                        break

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        with httpx.Client(timeout=3, trust_env=False) as client:
            with client.stream("GET", f"http://127.0.0.1:{server.server_port}/") as response:
                started = time.monotonic()
                with pytest.raises(RunLimitExceeded, match=r"截止|耗尽|超时|Deadline|deadline"):
                    list(reader(response, RunLimits(deadline_seconds=0.2)))
                assert time.monotonic() - started < 1.2, "总截止期不能退化为 3 秒读超时"
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
