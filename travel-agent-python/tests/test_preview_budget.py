"""Public-preview task limits exercise real HTTP attempts, concurrency and durable accounting.

预算与所有权的持久面在 `planning_task`（V16/T1）：本文件盯"账本跨重启不被重置、排队准入
按持久面判定"，所有权/租约/取消的完整语义见 tests/test_planning_task_lifecycle.py。
"""

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.agent.runtime.run_limits import RunLimitExceeded, RunLimits, phase_scope, use_task_limits
from app.agent.runtime.usage_store import UsageStore
from app.common import llm_client
from app.common.envelope import ApiError
from app.db import session as db_session
from app.db.models import Base, PlanningTask
from app.schemas.trip import DailyPlan
from app.services import day_persistence, planning_tasks


@pytest.fixture
def task_db(tmp_path):
    """持久执行面需要业务库（planning_task 行）；离线套件跑临时 SQLite。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'planning_task.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    planning_tasks.reset()
    yield
    planning_tasks.reset()
    db_session.init_engine(None, None)
    engine.dispose()


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


def test_takeover_keeps_spend_and_absolute_deadline(task_db):
    """重启接管继承已花掉的预算与绝对截止时刻：预算不因重启归零，也不因晚重启而提前过期。"""
    limits = planning_tasks.acquire(9, 4)
    limits.reserve_llm(10, 20)
    limits.record_retrieval(2)
    before = planning_tasks.state_of(4)
    assert before is not None

    # 模拟"提交它的那个进程已经没了"：本地句柄清空，持久行原样留着。
    planning_tasks.reset()
    restored = planning_tasks.takeover(9, 4)

    assert restored.llm_calls == 1 and restored.retrievals == 2, "接管后已花掉的调用不清零"
    assert restored.task_id != limits.task_id, "接管轮换任务令牌，旧 worker 从此写不回"
    after = planning_tasks.state_of(4)
    assert after is not None
    assert after["deadlineAt"] == before["deadlineAt"], "绝对截止沿用，不重开时间窗"
    assert after["attempt"] == before["attempt"] + 1
    assert 250 < restored.remaining() < 300, "剩余时间由绝对截止决定（旧口径从提交时刻反推，晚重启会误判过期）"
    assert after["consumed"]["consumed_llm_calls"] == 1 and after["consumed"]["consumed_retrievals"] == 2
    with pytest.raises(planning_tasks.TaskWindowExpired):
        planning_tasks.takeover(9, 5)


def test_takeover_refuses_after_absolute_deadline(task_db):
    """过期任务终态化：过了绝对截止就不给续跑窗口（由调用方收敛终态，见恢复回归）。"""
    planning_tasks.acquire(9, 6)
    with db_session.session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(PlanningTask.itinerary_id == 6)
            .values(deadline_at=datetime.now() - timedelta(seconds=1))
        )
    planning_tasks.reset()
    with pytest.raises(planning_tasks.TaskWindowExpired):
        planning_tasks.takeover(9, 6)


def test_nested_stage_keeps_root_budget_and_caps_time():
    limits = RunLimits(task_id="root")
    with use_task_limits(limits), phase_scope(0.01):
        token = limits.reserve_llm(1, 1)
        time.sleep(0.02)
        with pytest.raises(RunLimitExceeded):
            limits.check("deadline")
        limits.settle_llm(token, None)
    assert limits.llm_calls == 1 and limits.remaining() > 1


def test_user_lease_queue_rejection_and_expired_write(task_db):
    """排队准入与期限判定都读持久面：同用户第二个行程 429，期限已过的写路径直接抛。"""
    limits = planning_tasks.acquire(7, 1)
    try:
        with pytest.raises(ApiError) as error:
            planning_tasks.acquire(7, 2)
        assert error.value.status == 429
        with db_session.session_scope() as session:
            session.execute(
                update(PlanningTask)
                .where(PlanningTask.itinerary_id == 1)
                .values(deadline_at=datetime.now() - timedelta(seconds=1))
            )
        limits.deadline_at = time.time() - 1
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
