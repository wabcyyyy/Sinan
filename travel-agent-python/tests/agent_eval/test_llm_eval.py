"""真实模型评测的调用与用量统计回归；不调用模型。"""

import httpx
import pytest

from tests.agent_eval.llm_eval import _trace_stats


def test_product_response_keeps_persisted_opening_uncertainty():
    from tests.agent_eval.product_path import _response_from_detail

    response = _response_from_detail(
        {"genState": "COMPLETED", "dayList": [{"dayNo": 1, "items": [], "practicalNotes": ["开放安排待确认：博物馆"]}]},
        {"city": "杭州", "days": 1},
        {},
    )
    assert response.daily_plans[0].practical_notes == ["开放安排待确认：博物馆"]


def test_product_eval_uses_injected_stream_and_counts_research(monkeypatch):
    from contextlib import ExitStack

    from app.agent.generation.content import trip_stream
    from app.agent.research import reasoning
    from app.agent.runtime.trace import record_event
    from app.common.config import settings
    from tests.agent_eval import llm_eval, mock_llm
    from tests.agent_eval.eval_agent import _enter_offline_mocks, _guard_llm_http

    def plan_research(task):
        record_event("llm", "llm.request", metadata={"prompt_tokens": 13, "completion_tokens": 7})
        return {}

    stream_calls = []
    evaluate_response = llm_eval.evaluate_response

    def evaluate(*args):
        assert settings.redis_url == "redis://127.0.0.1:1/0", "指标必须在证据票所属缓存内计算"
        return evaluate_response(*args)

    def stream(request, *, cancel=None):
        stream_calls.append(request)
        record_event("llm", "llm.stream_request", metadata={"prompt_tokens": 50, "completion_tokens": 70})
        yield from mock_llm.fixture_trip_stream(request, cancel=cancel)

    with ExitStack() as stack, monkeypatch.context() as monkeypatch:
        _enter_offline_mocks(stack, True)
        leaks = _guard_llm_http(stack)
        monkeypatch.setattr(reasoning, "plan_research", plan_research)
        monkeypatch.setattr(trip_stream, "llm_open_trip_stream", stream)
        monkeypatch.setattr(llm_eval, "evaluate_response", evaluate)
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:2/0")
        monkeypatch.setattr(llm_eval, "_catalog", lambda city: (_ for _ in ()).throw(AssertionError("多余的参照检索")))
        run, _ = llm_eval._run_once({"city": "杭州", "days": 3, "persons": 2}, "test", "stream")
        assert settings.redis_url == "redis://127.0.0.1:2/0"
    assert leaks == []
    assert len(stream_calls) == 1
    assert run["status"] == "success"
    assert run["stats"]["llm_calls"] == 4
    assert run["stats"]["total_tokens"] == 180
    assert run["duration_ms"] > 0
    assert run["quality"]["terminal_reset_day_rate"] == 0
    assert run["quality"]["poi_authority_rate"] == 1.0


def test_product_eval_preserves_partial_delivery_and_repair_usage(monkeypatch):
    from contextlib import ExitStack

    from app.agent.runtime.trace import record_event
    from app.services import itinerary_generation
    from tests.agent_eval import llm_eval
    from tests.agent_eval.eval_agent import _enter_offline_mocks, _guard_llm_http

    generate_stream = itinerary_generation.run_generate_trip_stream

    def stream(request, **kwargs):
        record_event("llm", "llm.stream_request", metadata={"prompt_tokens": 50, "completion_tokens": 70})
        for event in generate_stream(request, **kwargs):
            yield event
            if event.get("type") == "day":
                raise RuntimeError("fixture stream interrupted after first delivered day")

    repaired = []

    def fail_day(itinerary_id, command, context, day_no, *args, **kwargs):
        repaired.append(day_no)
        record_event("llm", "llm.request", metadata={"prompt_tokens": 9, "completion_tokens": 1})
        raise RuntimeError("fixture repair failed")

    with ExitStack() as stack, monkeypatch.context() as monkeypatch:
        _enter_offline_mocks(stack, True)
        leaks = _guard_llm_http(stack)
        monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", stream)
        monkeypatch.setattr(itinerary_generation, "_generate_day_with_trace", fail_day)
        run, _ = llm_eval._run_once({"city": "杭州", "days": 3, "persons": 2}, "partial", "stream")
    assert leaks == []
    assert repaired == [2]
    assert run["status"] == "degraded", run.get("status_reason")
    assert run["stats"]["total_tokens"] == 130
    assert run["duration_ms"] > 0


def test_product_eval_retains_trace_when_result_read_fails(monkeypatch):
    from app.agent.runtime.trace import record_event
    from app.services import itinerary_generation
    from tests.agent_eval.product_path import run_product_path

    def broken_generate(*args):
        record_event("llm", "llm.request", metadata={"prompt_tokens": 10, "completion_tokens": 5})
        raise RuntimeError("fixture database failure")

    monkeypatch.setattr(itinerary_generation, "generate", broken_generate)
    with run_product_path({"city": "杭州", "days": 1}, "failed") as (response, trace, _):
        assert response.status == "failed"
        assert "database failure" in response.status_reason
        assert _trace_stats(trace)["total_tokens"] == 15


def test_trace_stats_counts_stream_and_blocking_usage_once():
    trace = {
        "events": [
            {"kind": "llm", "name": "llm.request", "metadata": {"prompt_tokens": 100, "completion_tokens": 20}},
            {
                "kind": "llm",
                "name": "llm.stream_request",
                "metadata": {"prompt_tokens": 1000, "completion_tokens": 2000},
            },
            {"kind": "llm", "name": "llm.retry", "metadata": {}},
            {"kind": "tool", "name": "search", "metadata": {}},
        ]
    }
    stats = _trace_stats(trace)
    assert stats["llm_calls"] == 2
    assert stats["total_tokens"] == 3120
    assert stats["usage_unknown_calls"] == 0
    assert stats["tool_calls"] == 1


def test_trace_stats_marks_missing_or_incomplete_usage():
    trace = {
        "events": [
            {"kind": "llm", "name": "llm.stream_request", "status": "cancelled"},
            {"kind": "llm", "name": "llm.request", "metadata": {"prompt_tokens": 12}},
            {
                "kind": "llm",
                "name": "llm.stream_request",
                "metadata": {"prompt_tokens": 0, "completion_tokens": 0, "usage_complete": False},
            },
        ]
    }
    stats = _trace_stats(trace)
    assert stats["llm_calls"] == 3
    assert stats["usage_unknown_calls"] == 3
    assert stats["total_tokens"] == 12


@pytest.fixture
def observed_client(monkeypatch):
    from app.common import llm_breaker, llm_client

    llm_breaker.reset_for_tests()
    monkeypatch.setattr(llm_client, "_record_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm_client, "_retry_sleep", lambda attempt: None)
    return llm_client.LLMClient(base_url="https://llm.example.test", api_key="test-only", model="test", max_attempts=2)


def test_failed_attempt_is_not_free_or_hidden_by_successful_retry(monkeypatch, observed_client):
    from app.agent.runtime.trace import trace_run

    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "synthetic timeout"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(observed_client, "_http", lambda: client)
        with trace_run("retry-usage") as trace:
            observed_client.chat_response([{"role": "user", "content": "test"}])
    stats = _trace_stats(trace.to_dict())
    assert len(calls) == stats["llm_calls"] == 2
    assert stats["usage_unknown_calls"] == 1
    assert stats["total_tokens"] == 12


def test_preflight_budget_rejection_is_not_an_outbound_call(monkeypatch, observed_client):
    from app.agent.runtime.run_limits import RunLimitExceeded, RunLimits, _active_limits
    from app.agent.runtime.trace import trace_run

    monkeypatch.setattr(observed_client, "_http", lambda: pytest.fail("preflight must not issue HTTP"))
    token = _active_limits.set(RunLimits(max_llm_calls=1, llm_calls=1))
    try:
        with trace_run("preflight-usage") as trace, pytest.raises(RunLimitExceeded):
            observed_client.chat_response([{"role": "user", "content": "test"}])
    finally:
        _active_limits.reset(token)
    assert _trace_stats(trace.to_dict())["llm_calls"] == 0


@pytest.mark.parametrize("stop", ["error", "close"])
def test_interrupted_stream_keeps_one_unknown_usage_record(monkeypatch, observed_client, stop):
    from app.agent.runtime.trace import trace_run

    class Interrupted(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            raise httpx.ReadError("synthetic broken stream")

        def close(self):
            self.closed = True

    stream = Interrupted()
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=stream))) as client:
        monkeypatch.setattr(observed_client, "_http", lambda: client)
        with trace_run("broken-stream-usage") as trace:
            generator = observed_client.stream_chat_deltas([{"role": "user", "content": "test"}])
            assert next(generator) == "hello"
            if stop == "error":
                with pytest.raises(httpx.ReadError):
                    list(generator)
            else:
                generator.close()
    stats = _trace_stats(trace.to_dict())
    assert stream.closed
    assert stats["llm_calls"] == stats["usage_unknown_calls"] == 1
