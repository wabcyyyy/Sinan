"""Local OpenAI-compatible HTTP fixture; no external model or travel facts."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from verify_planning_worker import fixture_plan


class FakeModel:
    def __init__(self, delay: float, retry: bool = False):
        self.delay = delay
        self.retry = retry
        self.gate = threading.Event()
        self.gate.set()
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.active = 0
        self.peak = 0
        self.retried: set[int] = set()
        model = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                request = json.loads(body["messages"][-1]["content"])
                with model.lock:
                    limited = model.retry and request["stage"] == "context" and request["trip"] not in model.retried
                    model.retried.add(request["trip"])
                    call = {**request, "started": time.time(), "status": 429 if limited else 200}
                    model.calls.append(call)
                    model.active += 1
                    model.peak = max(model.peak, model.active)
                try:
                    if not model.gate.wait(30):
                        raise TimeoutError("fixture gate not released")
                    time.sleep(0.03 if request["stage"] == "context" else model.delay)
                    content = json.dumps(fixture_plan(request.get("day", 1)), ensure_ascii=False)
                    if limited:
                        payload = json.dumps({"error": {"message": "synthetic rate limit"}}).encode()
                        self.send_response(429)
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    elif body.get("stream"):
                        chunks = [
                            {"choices": [{"delta": {"content": content}, "finish_reason": None}]},
                            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20}},
                        ]
                        payload = (
                            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                        ).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                    else:
                        payload = json.dumps(
                            {
                                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                            }
                        ).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A killed worker may close a real in-flight HTTP connection.
                finally:
                    with model.lock:
                        call["finished"] = time.time()
                        model.active -= 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def install_model_fixture(marker: str) -> None:
    """Keep plan_days/transactions/LLMClient real; fix research policy and plan content."""
    import httpx

    from app.agent import current_limits
    from app.common.llm_client import get_llm_client
    from app.schemas.trip import DailyPlan
    from app.services import itinerary_generation as generation

    send = httpx.Client.send
    origin = os.environ["LLM_BASE_URL"]

    def local_only(client, request, *args, **kwargs):
        if str(request.url).split("/chat/completions")[0] != origin:
            raise AssertionError(f"probe blocked non-fixture HTTP: {request.url.host}")
        return send(client, request, *args, **kwargs)

    httpx.Client.send = local_only

    def prompt(stage: str, day: int = 1) -> str:
        limits = current_limits()
        assert limits is not None and limits.itinerary_id is not None
        return json.dumps({"stage": stage, "trip": limits.itinerary_id, "day": day})

    def context(*_args, **_kwargs):
        get_llm_client().complete(prompt("context"), max_tokens=256)
        return {"candidates": [], "foods": [], "hotels": [], "consumption": None}

    def stream(*_args, **_kwargs):
        for day in (1, 2):
            text = get_llm_client().stream_chat([{"role": "user", "content": prompt("day", day)}], max_tokens=1024)
            yield {"type": "day", "plan": json.loads(text)}
            if day == 1 and marker:
                Path(marker).write_text("day one persisted", encoding="utf-8")
                time.sleep(120)
        yield {
            "type": "done",
            "daysExpected": 2,
            "daysEmitted": [1, 2],
            "complete": True,
            "tripTheme": None,
            "message": None,
        }

    def day(request):
        return DailyPlan.model_validate_json(
            get_llm_client().complete(prompt("repair", request.day_no), max_tokens=1024)
        )

    generation.destination_problem = lambda *_: None
    generation.run_plan_context = context
    generation.run_generate_trip_stream = stream
    generation.run_generate_day = day
