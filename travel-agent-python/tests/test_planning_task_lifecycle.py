"""持久执行闭环（V16 planning_task，上线 v1 T1）的行为钉。

验收口径逐条对 PLAN §5 T1：

1. **重启恢复**：排队/研究/生成/收尾各阶段"杀进程"后，有效任务（绝对截止未到）被接管续跑，
   过期任务终态化——判据是持久租约与 deadline，不是进程内活跃表。
2. **所有权与失权写入**：接管会轮换 task_id；旧 worker 的下一次外呼、落库与终态写全部被拒
   （条件更新 rowcount=0 → `TaskOwnershipLost`），不会覆盖新持有者的结论。
3. **累计预算**：接管继承已花掉的调用/token；用户显式补齐重开时间窗但累计账本只增不减，
   所以"反复重试拿不到新额度"是可查的事实（绑成费用硬顶归 T2）。
4. **取消**：取消旗写在持久面；持有者在下一个检查点停手，已交付天保留为 PARTIAL，
   一天没交付才 FAILED；执行态收 CANCELLED。

全离线：LLM/研究入口在编排模块名字上打桩，业务库是每用例一份临时 SQLite
（生产用 MySQL，同一套模型；迁移在 MySQL 上的实际执行见 handoffs/T1.md 的隔离 smoke）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

import app.agent  # noqa: F401  先完成门面导入（存量导入环，同 test_llm_route 口径）
from app.agent import use_task_limits
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import ApiError, install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    BudgetDetail,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    PlanningTask,
    SysUser,
)
from app.schemas.trip import DailyPlan, TripItem
from app.services import (
    day_persistence,
    generation_recovery,
    itinerary_generation,
    planning_tasks,
    state_and_sessions,
    user_service,
)
from tests.planning_task_probe import simulate_worker_exit

PASSWORD = "example123"
CITY = "杭州"


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'planning_task.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", "example-only-hs256-test-signing-material")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    planning_tasks.reset()
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
        session.add(SysUser(username="bob", password=user_service.hash_password(PASSWORD), status=1, role="user"))
    yield
    planning_tasks.reset()
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    engine.dispose()


@pytest.fixture
def clients(env) -> dict[str, TestClient]:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    alice = TestClient(app, follow_redirects=False)
    alice.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    bob = TestClient(app, follow_redirects=False)
    bob.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
    return {"alice": alice, "bob": bob, "anon": TestClient(app, follow_redirects=False)}


def _seed_trip(user_id: int = 1, *, delivered_days: int = 1, total_days: int = 2) -> int:
    """GENERATING 行程壳 + 前 N 天已交付（带一条 45 元景点）、其余天 PENDING。"""
    with db_session.session_scope() as session:
        main = ItineraryMain(
            user_id=user_id,
            title=f"{CITY}{total_days}日游",
            city=CITY,
            days=total_days,
            persons=1,
            status=1,
            gen_state="GENERATING",
        )
        session.add(main)
        session.flush()
        for day_no in range(1, total_days + 1):
            day = ItineraryDay(
                itinerary_id=main.id,
                day_no=day_no,
                city=CITY,
                generation_status="SUCCEEDED" if day_no <= delivered_days else "PENDING",
            )
            session.add(day)
            session.flush()
            if day_no <= delivered_days:
                session.add(
                    ItineraryItem(
                        itinerary_id=main.id,
                        day_id=day.id,
                        item_type="attraction",
                        poi_name=f"景点{day_no}",
                        cost=Decimal("45.00"),
                        sort_no=0,
                        review_requirement="none",
                        freshness_status="fresh",
                    )
                )
        return main.id


def _plan(day_no: int) -> DailyPlan:
    return DailyPlan(
        day_no=day_no,
        note=f"第 {day_no} 天",
        theme="湖山线",
        items=[
            TripItem(poi_name=f"新景点{day_no}", item_type="attraction", cost=45, start_time="09:00", end_time="11:30"),
            TripItem(poi_name=f"知味观{day_no}", item_type="food", cost=40, start_time="11:30", end_time="13:00"),
        ],
    )


def _fake_agents(monkeypatch, per_day: list[DailyPlan] | None = None) -> dict:
    calls: dict = {"day": [], "context": 0}

    monkeypatch.setattr(
        itinerary_generation,
        "run_plan_context",
        lambda request, itinerary_id=None, **kwargs: (
            calls.__setitem__("context", calls["context"] + 1),
            {"candidates": [], "foods": [], "hotels": [], "consumption": None},
        )[1],
    )

    def fake_day(request):
        calls["day"].append(request.day_no)
        if per_day is None:
            raise RuntimeError("LLM 上游炸了")
        return per_day[min(request.day_no, len(per_day)) - 1]

    def fake_stream(request, cancel=None, **kwargs):
        # 整段流式不可用 → plan_days 退回逐日兜底（同 tests/test_m5b_core_ready 的口径）
        raise RuntimeError("整段流式不可用")
        yield  # pragma: no cover

    monkeypatch.setattr(itinerary_generation, "run_generate_day", fake_day)
    monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", fake_stream)
    # 富化池空转但必须交还句柄（与 tests/test_regenerate_day 同口径）：否则执行终态
    # 永远停在 RUNNING，测不到"任务收尾即落结论"。
    monkeypatch.setattr(
        itinerary_generation.enricher_pool,
        "submit",
        lambda task, *args: planning_tasks.release(args[1]) if len(args) > 1 else None,
    )
    return calls


def _run_inline(monkeypatch) -> None:
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))


def _main(trip_id: int) -> ItineraryMain:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        session.expunge(main)
        return main


def _task_row(trip_id: int) -> PlanningTask:
    with db_session.session_scope() as session:
        row = session.get(PlanningTask, trip_id)
        assert row is not None
        session.expunge(row)
        return row


# ---------------------------------------------------------------- 1. 接管与续跑 ----


def test_takeover_resumes_queued_task_and_keeps_cumulative_ledger(env) -> None:
    """排队阶段"杀进程"重启：任务被接管续跑，已花掉的调用不清零，attempt 记录第几个 worker。"""
    trip_id = _seed_trip()
    first = planning_tasks.acquire(1, trip_id)
    first.reserve_llm(10, 20)
    first.record_retrieval(3)
    first_deadline = first.deadline_at
    simulate_worker_exit(trip_id)  # = 进程死在这里：本地句柄清空、租约过期

    second = planning_tasks.takeover(1, trip_id)

    row = _task_row(trip_id)
    assert row.attempt == 2 and row.task_id == second.task_id and row.task_id != first.task_id
    assert second.llm_calls == 1 and second.retrievals == 3, "接管继承已花掉的预算，不归零"
    assert second.prompt_tokens + second.completion_tokens == 30
    assert second.deadline_at == first_deadline, "绝对截止沿用，接管不重开时间窗"
    assert row.consumed_llm_calls == 1 and row.consumed_retrievals == 3, "累计账本随预算写入落库"


def test_takeover_after_deadline_terminalizes_instead_of_resuming(env, monkeypatch) -> None:
    """过期任务终态化：已交付天保留为 PARTIAL，不重开窗口、不再外呼。"""
    trip_id = _seed_trip(delivered_days=1)
    planning_tasks.acquire(1, trip_id)
    with db_session.session_scope() as session:
        past = datetime.now() - timedelta(seconds=1)
        session.execute(
            update(PlanningTask)
            .where(PlanningTask.itinerary_id == trip_id)
            .values(deadline_at=past, lease_expires_at=past, state="RUNNING")
        )
        # 谓词要求"闲置超过 5 分钟"，所以心跳单独拨老；截止时间只要过期即可（两个不同的钟）
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == trip_id)
            .values(updated_at=datetime.now() - timedelta(minutes=30))
        )
    calls = _fake_agents(monkeypatch, [_plan(2)])
    _run_inline(monkeypatch)
    simulate_worker_exit(trip_id)

    assert generation_recovery.recover() == 1
    assert _main(trip_id).gen_state == "PARTIAL", "过期但已有交付天 → 部分交付，不是重新开跑"
    assert calls["day"] == [] and calls["context"] == 0, "过期任务不许再烧一次外呼"
    with db_session.session_scope() as session:
        rows = session.execute(select(BudgetDetail).where(BudgetDetail.itinerary_id == trip_id)).scalars().all()
    assert list(rows), "部分交付必须带最终预算（与 T0 口径一致）"
    assert _task_row(trip_id).state == "EXPIRED", "执行态也收口，否则下轮扫描又来看一次"


def test_takeover_resumes_undelivered_days(env, monkeypatch) -> None:
    """有效期内重启：恢复扫描接管、继承预算，把没交付的天补完后由持有者收终态。"""
    trip_id = _seed_trip(delivered_days=1)
    first = planning_tasks.acquire(1, trip_id)
    first.reserve_llm(10, 20)
    with db_session.session_scope() as session:
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == trip_id)
            .values(updated_at=datetime.now() - timedelta(minutes=30))
        )
    calls = _fake_agents(monkeypatch, [_plan(2)])
    _run_inline(monkeypatch)
    simulate_worker_exit(trip_id)

    # 僵尸分支沿用 Java 计数口径（不改 gen_resumed、不记 changed），可观察效果是活儿干完了
    generation_recovery.recover()

    assert calls["day"] == [2], "只补没交付的那天，已交付天不重生成"
    assert _main(trip_id).gen_state == "COMPLETED"
    row = _task_row(trip_id)
    assert row.state == "FINISHED" and row.attempt >= 2, "接管后由当前持有者收终态，attempt 记录执行序号"
    assert row.task_id != first.task_id
    assert row.consumed_llm_calls >= 1, "接管继承已花掉的调用，不重置账本"


# ------------------------------------------------------------ 2. 失权后的写入 ----


def test_taken_over_worker_cannot_call_or_write(env) -> None:
    """接管后旧 worker：下一次外呼前就被挡下，落库写同样被拒（走的是产品写路径的守卫）。"""
    trip_id = _seed_trip()
    old = planning_tasks.acquire(1, trip_id)
    fresh = planning_tasks.takeover(1, trip_id)
    assert fresh.task_id != old.task_id

    with pytest.raises(planning_tasks.TaskOwnershipLost):
        old.reserve_llm(10, 10)
    with use_task_limits(old), pytest.raises(planning_tasks.TaskOwnershipLost):
        day_persistence.set_trip_theme(trip_id, "旧 worker 想改的标题")
    assert _main(trip_id).title != "旧 worker 想改的标题"
    assert planning_tasks.holds_lease(trip_id, fresh.task_id) is True
    assert planning_tasks.holds_lease(trip_id, old.task_id) is False


def test_old_worker_cannot_write_terminal_state(env) -> None:
    """本地句柄已交还（进程重启/被接管）的 worker 不许写终态：产品态归当前持有者。"""
    trip_id = _seed_trip(delivered_days=1)
    planning_tasks.acquire(1, trip_id)
    simulate_worker_exit(trip_id)  # = 那个 worker 没了，句柄与租约都不再属于它

    planning_tasks.finish_failure(trip_id, ValueError("旧 worker 的收尾"))

    assert _main(trip_id).gen_state == "GENERATING", "失权 worker 不能把产品态写成 PARTIAL/FAILED"
    assert _task_row(trip_id).state == "QUEUED", "执行态同样不被旧 worker 覆盖"


def test_budget_write_renews_lease(env) -> None:
    """每次预算写入顺带续租：活着的外呼成本记录本身就是心跳。"""
    trip_id = _seed_trip()
    limits = planning_tasks.acquire(1, trip_id)
    with db_session.session_scope() as session:
        session.execute(
            update(PlanningTask)
            .where(PlanningTask.itinerary_id == trip_id)
            .values(lease_expires_at=datetime.now() - timedelta(seconds=30))
        )
    expired = _task_row(trip_id).lease_expires_at
    assert expired is not None
    limits.reserve_llm(5, 5)
    renewed = _task_row(trip_id).lease_expires_at
    assert renewed is not None and renewed > expired, "预算写入必须把租约推回未来"


# -------------------------------------------------------------------- 3. 取消 ----


def test_cancel_stops_next_call_and_keeps_delivered_days(env) -> None:
    """取消旗写在持久面：持有者在下一次外呼前停手，已交付天保留为 PARTIAL。"""
    trip_id = _seed_trip(delivered_days=1)
    limits = planning_tasks.acquire(1, trip_id)
    result = planning_tasks.request_cancel(1, trip_id)
    assert result["cancelRequested"] is True and result["undeliveredDays"] == [2]
    assert _task_row(trip_id).cancel_requested_at is not None

    with pytest.raises(planning_tasks.TaskCancelled):
        limits.reserve_llm(10, 10)

    planning_tasks.finish_failure(trip_id, planning_tasks.TaskCancelled("cancelled"))
    assert _main(trip_id).gen_state == "PARTIAL", "取消不清空已交付内容"
    planning_tasks.release(trip_id, limits.task_id)
    row = _task_row(trip_id)
    assert row.state == "CANCELLED" and row.result_code == "CANCELLED"


def test_cancel_without_delivered_days_fails_trip(env) -> None:
    trip_id = _seed_trip(delivered_days=0)
    limits = planning_tasks.acquire(1, trip_id)
    planning_tasks.request_cancel(1, trip_id)
    with pytest.raises(planning_tasks.TaskCancelled):
        limits.record_retrieval(1)
    planning_tasks.finish_failure(trip_id, planning_tasks.TaskCancelled("cancelled"))
    planning_tasks.release(trip_id, limits.task_id)
    assert _main(trip_id).gen_state == "FAILED"
    assert _task_row(trip_id).state == "CANCELLED"


def test_cancel_endpoint_auth_ownership_and_rejection(clients, env) -> None:
    """端点面：匿名 401、他人 404、没有进行中的任务 409、进行中可取消。"""
    trip_id = _seed_trip()
    anon = clients["anon"].post(f"/api/itinerary/{trip_id}/generation/cancel")
    assert anon.status_code == 401, "取消是写操作，默认拒绝匿名"

    planning_tasks.acquire(1, trip_id)
    other = clients["bob"].post(f"/api/itinerary/{trip_id}/generation/cancel")
    assert other.json()["code"] == 404, "他人行程不暴露存在性"

    mine = clients["alice"].post(f"/api/itinerary/{trip_id}/generation/cancel")
    assert mine.status_code == 200, mine.json()
    assert mine.json()["data"]["cancelRequested"] is True

    idle = _seed_trip()
    rejected = clients["alice"].post(f"/api/itinerary/{idle}/generation/cancel")
    assert rejected.json()["code"] == 409, "没有进行中任务时不假装取消成功"


def test_cancel_reaches_another_processs_task(env) -> None:
    """取消写在持久面：不是本进程提交的任务同样能取消（进程内活跃表做不到这条）。"""
    trip_id = _seed_trip()
    with db_session.session_scope() as session:
        session.add(
            PlanningTask(
                itinerary_id=trip_id,
                user_id=1,
                task_id="other-worker-task",
                lease_owner="other-worker",
                attempt=1,
                state="RUNNING",
                submit_at=datetime.now() - timedelta(minutes=1),
                deadline_at=datetime.now() + timedelta(minutes=4),
                lease_expires_at=datetime.now() + timedelta(minutes=1),
                budget_json=json.dumps({}),
            )
        )
    result = planning_tasks.request_cancel(1, trip_id)
    assert result["attempt"] == 1
    assert _task_row(trip_id).cancel_requested_at is not None


# ---------------------------------------------------------------- 4. 幂等与准入 ----


def test_duplicate_submit_same_trip_is_rejected(env) -> None:
    """重复提交同一行程：活着的租约就是"有人在跑"的权威答案，第二次提交被拒且不动所有权。"""
    trip_id = _seed_trip()
    first = planning_tasks.acquire(1, trip_id)
    with pytest.raises(ApiError) as error:
        planning_tasks.acquire(1, trip_id)
    assert error.value.status == 409
    row = _task_row(trip_id)
    assert row.task_id == first.task_id and row.attempt == 1, "被拒的提交不许轮换持有者或推进 attempt"


def test_admission_survives_local_registry_loss(env) -> None:
    """同用户第二个行程的 429 判定不依赖本地注册表：重启后依然成立。"""
    trip_id = _seed_trip(user_id=3)
    planning_tasks.acquire(3, trip_id)
    simulate_worker_exit()  # 只清本地句柄：租约仍然活着（那个 worker 只是"我们看不见"）
    with pytest.raises(ApiError) as error:
        planning_tasks.acquire(3, _seed_trip(user_id=3))
    assert error.value.status == 429
