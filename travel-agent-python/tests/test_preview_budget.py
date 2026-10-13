"""Public-preview task limits exercise real HTTP attempts, concurrency and durable accounting."""

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from app.agent.runtime.run_limits import RunLimitExceeded, RunLimits, phase_scope, use_task_limits
from app.agent.runtime.task_budget import load_task_budget, new_task_budget
from app.agent.runtime.usage_store import UsageStore
from app.common import llm_client
from app.common.config import settings
from app.common.envelope import ApiError
from app.schemas.trip import DailyPlan
from app.services import day_persistence, planning_tasks


def test_failed_attempt_uses_last_call_and_cannot_retry(monkeypatch, tmp_path):
    hits = []

    def fail(request):
        hits.append(request)
        return httpx.Response(503, request=request)

    http = httpx.Client(transport=httpx.MockTransport(fail))
    store = UsageStore(tmp_path / "usage.db")
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: http)
    monkeypatch.setattr(llm_client, "usage_store", store)
    monkeypatch.setattr(llm_client, "_retry_sleep", lambda _attempt: None)
    limits = RunLimits(max_llm_calls=1, task_id="attempt-test", max_tokens=10000)
    client = llm_client.LLMClient(base_url="https://example.invalid/v1", api_key="example", model="fake")
    with use_task_limits(limits), pytest.raises(RunLimitExceeded):
        client.chat([{"role": "user", "content": "hello"}], max_tokens=100)
    assert len(hits) == limits.llm_calls == 1
    rows = store.task_calls("attempt-test")
    assert len(rows) == 1 and not rows[0]["success"] and not rows[0]["usage_complete"]
    assert rows[0]["prompt_tokens"] > 0 and rows[0]["completion_tokens"] == 100
    assert limits.usage_unknown == 1


def test_atomic_reservation_in_parallel_never_exceeds_limit():
    limits = RunLimits(max_llm_calls=1)

    def reserve():
        try:
            return limits.reserve_llm(10, 20)
        except RunLimitExceeded:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: reserve(), range(32)))
    assert len([value for value in results if value]) == limits.llm_calls == 1
    assert limits.prompt_tokens + limits.completion_tokens == 30


def test_missing_usage_retains_reservation_and_known_usage_refunds():
    limits = RunLimits(max_tokens=1000)
    first = limits.reserve_llm(100, 200)
    limits.settle_llm(first, None)
    assert limits.prompt_tokens + limits.completion_tokens == 300
    second = limits.reserve_llm(100, 200)
    limits.settle_llm(second, {"prompt_tokens": 20, "completion_tokens": 30})
    assert limits.prompt_tokens + limits.completion_tokens == 350 and limits.usage_unknown == 1
    with pytest.raises(RunLimitExceeded):
        limits.reserve_llm(500, 200)
    assert limits.llm_calls == 2


def test_queue_time_and_durable_resume_share_spend(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "checkpoint_db_path", str(tmp_path / "checkpoints.db"))
    limits = new_task_budget(9, 4)
    limits.reserve_llm(10, 20)
    limits.record_retrieval(2)
    limits.submitted_at -= 40
    assert limits.persist is not None
    limits.persist(limits.snapshot())
    restored = load_task_budget(4)
    assert restored is not None
    assert restored.task_id == limits.task_id and restored.llm_calls == 1 and restored.retrievals == 2
    assert 250 < restored.remaining() < 260
    assert load_task_budget(5) is None


def test_nested_stage_keeps_root_budget_and_caps_time():
    limits = RunLimits(task_id="root")
    with use_task_limits(limits), phase_scope(0.01):
        token = limits.reserve_llm(1, 1)
        time.sleep(0.02)
        with pytest.raises(RunLimitExceeded):
            limits.check("deadline")
        limits.settle_llm(token, None)
    assert limits.llm_calls == 1 and limits.remaining() > 1


def test_user_lease_queue_rejection_and_expired_write(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "checkpoint_db_path", str(tmp_path / "budget.db"))
    planning_tasks.reset()
    limits = planning_tasks.acquire(7, 1)
    try:
        with pytest.raises(ApiError) as error:
            planning_tasks.acquire(7, 2)
        assert error.value.status == 429
        limits.started_at -= 300
        with use_task_limits(limits), pytest.raises(RunLimitExceeded):
            day_persistence.persist(1, None, 1, DailyPlan(day_no=1, items=[]), "day-1", "fp")
    finally:
        planning_tasks.release(1, limits.task_id)
    next_task = planning_tasks.acquire(7, 2)
    planning_tasks.release(2, next_task.task_id)
    assert not planning_tasks.is_active(2)


def test_research_counter_and_context_copies_share_cap():
    limits = RunLimits(max_research_calls=1)
    with use_task_limits(limits):
        copied = contextvars.copy_context()
    copied.run(limits.record_research)
    with pytest.raises(RunLimitExceeded):
        copied.run(limits.record_research)
    assert limits.research_calls == 1
