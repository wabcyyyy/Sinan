"""Shared fixtures for the low-thinking experiments: frozen Tokyo input, LLM call capture, offline postprocess.

No secrets are printed; reasoning text is never stored (only token counts).
"""

import copy
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "travel-agent-python"
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))
sys.stdout.reconfigure(encoding="utf-8")

from app.agent.grounding import grounding_evidence  # noqa: E402

_tickets: dict = {}
# Only this process holds evidence tickets; never write frozen fixture evidence into runtime Redis.
grounding_evidence.cache_store = SimpleNamespace(
    set_json=lambda ns, key, value, ttl: _tickets.__setitem__((ns, key), value),
    get_json=lambda ns, key: _tickets.get((ns, key)),
)

from app.agent.tools.impl import _as_candidate  # noqa: E402
from app.common import llm_client  # noqa: E402
from app.common.config import settings  # noqa: E402
from app.schemas.trip import GenerateDayRequest, GenerateRequest  # noqa: E402
from app.schemas.trip_requirements import TripRequirements  # noqa: E402

SETUP = json.loads(
    (ROOT / "docs/public/release-category-p1-fix-2026-10-12/adequate/setup.json").read_text(encoding="utf-8")
)
SOURCE = json.loads(
    (ROOT / "docs/public/release-diet-evidence-2026-10-12/live-strict-trip21.json").read_text(encoding="utf-8")
)
INTENT = "东京2天，2个人，预算3000元人民币，带70岁老人，慢节奏，不去寺庙，每天最多两个景点，素食可吃蛋奶。"


def frozen_context() -> dict:
    """Six Overpass vegetarian rows + 5 attractions + 1 hotel (same rows as the 2026-10-12 adequate group)."""
    return {key: [_as_candidate(dict(row), "东京") for row in rows] for key, rows in SETUP["context"].items()}


def requirements() -> TripRequirements:
    return TripRequirements.model_validate(SETUP["requirements"])


def day_request(context: dict, day_no: int = 1, days: int = 2) -> GenerateDayRequest:
    return GenerateDayRequest(
        city="东京",
        persons=2,
        budget=3000,
        day_no=day_no,
        days=days,
        needs_hotel=True,
        preferences=SOURCE["input"].get("preferences") or [],
        intent=INTENT,
        requirements_struct=requirements(),
        context=context,
    )


def trip_request() -> GenerateRequest:
    return GenerateRequest(
        city="东京",
        days=2,
        persons=2,
        budget=3000,
        preferences=SOURCE["input"].get("preferences") or [],
        intent=INTENT,
        requirements_struct=requirements(),
    )


class CallLog:
    """Wraps LLMClient.chat_response / stream_chat_deltas at class level to record every real request."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.entry = ""
        self._orig_resp = llm_client.LLMClient.chat_response
        self._orig_stream = llm_client.LLMClient.stream_chat_deltas
        self._orig_event = llm_client.record_event

    def install(self) -> None:
        log = self

        def chat_response(client, messages, **kwargs):
            started = time.monotonic()
            rec = {"entry": log.entry, "mode": "blocking", "max_tokens": kwargs.get("max_tokens", 2048)}
            log.calls.append(rec)
            try:
                resp = log._orig_resp(client, messages, **kwargs)
            except Exception as exc:
                rec.update(error=f"{type(exc).__name__}: {str(exc)[:200]}", seconds=round(time.monotonic() - started, 2))
                raise
            usage = resp.get("usage") or {}
            content = (resp.get("message") or {}).get("content") or ""
            rec.update(
                seconds=round(time.monotonic() - started, 2),
                finish_reason=resp.get("finish_reason"),
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                content=content,
            )
            return resp

        def stream_chat_deltas(client, messages, **kwargs):
            started = time.monotonic()
            rec = {"entry": log.entry, "mode": "stream", "max_tokens": kwargs.get("max_tokens", 2048)}
            log.calls.append(rec)
            parts = []
            try:
                for delta in log._orig_stream(client, messages, **kwargs):
                    if "first_content_s" not in rec:
                        rec["first_content_s"] = round(time.monotonic() - started, 2)
                    parts.append(delta)
                    yield delta
            except Exception as exc:
                rec.update(error=f"{type(exc).__name__}: {str(exc)[:200]}")
                raise
            finally:
                rec.update(seconds=round(time.monotonic() - started, 2), content="".join(parts))

        def record_event(kind, name, **kw):
            meta = kw.get("metadata") or {}
            if name == "llm.stream_request" and log.calls and log.calls[-1]["mode"] == "stream":
                log.calls[-1].update(
                    finish_reason=meta.get("finish_reason"),
                    prompt_tokens=meta.get("prompt_tokens"),
                    completion_tokens=meta.get("completion_tokens"),
                    reasoning_tokens=meta.get("reasoning_tokens"),
                )
            return log._orig_event(kind, name, **kw)

        llm_client.LLMClient.chat_response = chat_response
        llm_client.LLMClient.stream_chat_deltas = stream_chat_deltas
        llm_client.record_event = record_event


def thinking_snapshot() -> dict:
    payload: dict = {}
    llm_client._apply_provider_options(payload, settings.llm_base_url, False)
    return {
        "model": settings.llm_model,
        "llm_enable_thinking": settings.llm_enable_thinking,
        "llm_reasoning_effort": settings.llm_reasoning_effort,
        "provider_payload": payload,
    }


def json_complete(text: str) -> bool:
    from app.agent.core.json_utils import parse_llm_json

    try:
        parse_llm_json(text)
        return True
    except Exception:
        return False


def strip_content(calls: list[dict]) -> list[dict]:
    out = []
    for c in calls:
        row = {k: v for k, v in c.items() if k != "content"}
        row["content_chars"] = len(c.get("content") or "")
        row["json_complete"] = json_complete(c.get("content") or "")
        out.append(row)
    return out


def deepcopy(x):
    return copy.deepcopy(x)
