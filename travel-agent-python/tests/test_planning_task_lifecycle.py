"""持久执行闭环（V16 planning_task，上线 v1 T1）的行为钉。

验收口径逐条对 PLAN §5 T1：

1. **重启恢复**：排队/研究/生成/收尾各阶段"杀进程"后，有效任务（绝对截止未到）被接管续跑，
   过期任务终态化——判据是持久租约与 deadline，不是进程内活跃表。
2. **所有权与失权写入**：接管会轮换 task_id；旧 worker 的下一次外呼、落库与终态写全部被拒
   （条件更新 rowcount=0 → `TaskOwnershipLost`），不会覆盖新持有者的结论。
3. **累计预算**：接管继承已花掉的调用/token；用户显式补齐重开时间窗但不清空已结算用量，
   所以"反复重试拿不到新额度"是可查的事实（绑成费用硬顶归 T2）。
4. **取消**：取消旗写在持久面；持有者在下一个检查点停手，已交付天保留为 PARTIAL，
   一天没交付才 FAILED；执行态收 CANCELLED。

全离线：LLM/研究入口在编排模块名字上打桩，业务库是每用例一份临时 SQLite
（生产用 MySQL，同一套模型；这里不验证 MySQL 上的实际迁移执行）。
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
    planning_task_convergence,
    planning_tasks,
    state_and_sessions,
    user_service,
)
from tests.planning_task_probe import expire_lease, simulate_worker_exit

PASSWORD = "example123"
CITY = "杭州"


@pytest.fixture
def env(monkeypatch, tmp_path):
    from tests.planning_task_probe import install_inline_worker

    install_inline_worker(monkeypatch)
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
    """接管后旧 worker：下一次外呼前就被挡下，落库写同样被拒（走的是产品写路径的守卫）。

    接管的前提与恢复扫描一致——原租约已过期（`is_active()` 判 False）。租约还活着时的
    抢占已被 `LeaseHeld` 拒掉，见 `test_takeover_refuses_a_still_leased_task`。
    """
    trip_id = _seed_trip()
    old = planning_tasks.acquire(1, trip_id)
    expire_lease(trip_id)  # 原持有者不再续租（本地句柄留着，专门演"同进程第二份身份接管"）
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


def test_takeover_refuses_a_still_leased_task(env) -> None:
    """租约还在续租时不能抢占：恢复扫描必须让路（旧实现只看进程内表，抢得毫无痕迹）。"""
    trip_id = _seed_trip()
    planning_tasks.acquire(1, trip_id)
    planning_tasks.reset()  # 第二个 worker 的视角：本地没有句柄，但行里的租约仍然新鲜

    with pytest.raises(planning_tasks.LeaseHeld):
        planning_tasks.takeover(1, trip_id)

    assert _task_row(trip_id).attempt == 1, "被拒的接管不得推进 attempt"
    snapshot = planning_tasks.state_of(trip_id)
    assert snapshot is not None and snapshot["state"] == "QUEUED"


def test_cancel_flag_with_dead_holder_converges_once(env) -> None:
    """取消旗 + 持有者当场死掉：扫描把它收敛成 CANCELLED，且不会被反复接管。

    这条路径原先不存在——恢复只看租约，于是任务每 90s 被接管一次、每次都在 `TaskStopped`
    上退出，CANCELLED 是不可达状态（PLAN T1 的「取消」与「重启恢复」交叉的洞）。
    """
    trip_id = _seed_trip(delivered_days=1)
    planning_tasks.acquire(1, trip_id)
    planning_tasks.request_cancel(1, trip_id)
    expire_lease(trip_id)  # = 持有者没来得及收敛就没了

    assert generation_recovery.recover_one(trip_id, False) is True
    row = _task_row(trip_id)
    assert row.state == "CANCELLED" and row.result_code == "CANCELLED_PARTIAL"
    assert _main(trip_id).gen_state == "PARTIAL", "已交付的那一天不能因为取消被清空"

    planning_tasks.reset()
    assert generation_recovery.recover_one(trip_id, False) is False, "取消过的责任不由后台复活"


def test_stale_worker_cannot_write_trip_terminal_primitive(env) -> None:
    """产品终态也认所有权：绕过编排层直接调终态原语，被接管的旧 worker 照样写不动。

    以前只有 `_finish`/`finish_failure` 里的 `holds_task` 挡它——那等于"守规矩的调用方才被拦"，
    将来任何新调用点或异常路径都能把接管者判的 PARTIAL 覆盖成 COMPLETED。
    取消不在拦阻之列：那是当前持有者自己的交付责任，必须能落终态。
    """
    trip_id = _seed_trip(delivered_days=1)
    stale = planning_tasks.acquire(1, trip_id)
    expire_lease(trip_id)
    fresh = planning_tasks.takeover(1, trip_id)
    assert fresh.task_id is not None and stale.task_id != fresh.task_id

    with use_task_limits(stale):
        day_persistence.complete_trip(trip_id, True)
        day_persistence.fail_trip(trip_id, "旧 worker 想覆盖结论", user_facing=True)

    assert _main(trip_id).gen_state != "COMPLETED", "旧 worker 不得把行程写成 COMPLETED"
    assert _main(trip_id).gen_state != "FAILED", "旧 worker 也不得把行程写成 FAILED"

    planning_tasks.request_cancel(1, trip_id)
    with use_task_limits(fresh):
        planning_tasks.finish_failure(trip_id, planning_tasks.TaskCancelled("用户取消"))

    assert _main(trip_id).gen_state == "PARTIAL", "当前持有者的取消收敛必须还能写终态"
    # 执行态随句柄脱离一起落（finish_failure 只登记结论，release 才写行）：
    # 这一段同时证明"取消的持有者"没被所有权守卫误伤。
    planning_tasks.release(trip_id, fresh.task_id)
    assert _task_row(trip_id).state == "CANCELLED"


def test_resubmit_reopens_window_not_quota(env) -> None:
    """已结算的责任重新拉起：时间窗重开、累计账本不清零，"重试拿不到新额度"成为可查事实。"""
    trip_id = _seed_trip(delivered_days=1)
    first = planning_tasks.acquire(1, trip_id)
    first.reserve_llm(4, 6)
    first.record_retrieval(2)
    first_deadline = first.deadline_at
    simulate_worker_exit(trip_id)
    planning_task_convergence.terminalize_expired(trip_id, completed_days=1)

    second = planning_tasks.resume_or_restart(1, trip_id)

    assert second.llm_calls == 1 and second.retrievals == 2, "新 attempt 必须继承已花掉的账"
    assert first_deadline is not None and second.deadline_at is not None
    assert second.deadline_at > first_deadline, "重开的是时间窗，不是额度"
    row = _task_row(trip_id)
    assert row.attempt == 2 and row.consumed_llm_calls == 1 and row.state == "QUEUED"


@pytest.mark.parametrize("usage", [{"prompt_tokens": 4, "completion_tokens": 6}, None])
def test_resubmit_inherits_settled_tokens_and_keeps_unknown_reservations(env, usage) -> None:
    """结算释放多余预留，重试不重复计入；usage 未知时仍保守保留全部预占。"""
    trip_id = _seed_trip(delivered_days=1)
    first = planning_tasks.acquire(1, trip_id)
    reservation = first.reserve_llm(100, 200)
    first.settle_llm(reservation, usage)
    first.settle_llm(reservation, usage)  # 重放结算不二次退款。
    expected = (4, 6) if usage else (100, 200)
    row = _task_row(trip_id)
    assert (row.consumed_prompt_tokens, row.consumed_completion_tokens) == expected
    simulate_worker_exit(trip_id)
    planning_task_convergence.terminalize_expired(trip_id, completed_days=1)

    second = planning_tasks.resume_or_restart(1, trip_id)
    assert (second.prompt_tokens, second.completion_tokens) == expected
    next_reservation = second.reserve_llm(40, 60)
    second.settle_llm(next_reservation, {"prompt_tokens": 2, "completion_tokens": 3})
    row = _task_row(trip_id)
    assert (row.consumed_prompt_tokens, row.consumed_completion_tokens) == (expected[0] + 2, expected[1] + 3)
    assert row.consumed_llm_calls == 2


def test_incomplete_final_check_is_not_a_budget_degradation(env) -> None:
    """「检查没跑完」不得被研究段的「预算耗尽诚实降级」分支吞掉——那正是绕过验收的老路。"""
    from app.agent.runtime.run_limits import RunLimitExceeded
    from app.services.itinerary_generation import DeliveryCheckIncomplete

    assert not issubclass(DeliveryCheckIncomplete, RunLimitExceeded)
    assert issubclass(DeliveryCheckIncomplete, RuntimeError)


def test_incomplete_final_check_keeps_delivered_days_as_partial(env) -> None:
    """终检抛错时整趟不能写成 COMPLETED：已交付天保留为 PARTIAL，执行态归失败。"""
    from app.services.itinerary_generation import DeliveryCheckIncomplete

    trip_id = _seed_trip(delivered_days=1)
    planning_tasks.acquire(1, trip_id)

    planning_tasks.finish_failure(trip_id, DeliveryCheckIncomplete("行程最终质量检查未完成"))

    assert _main(trip_id).gen_state == "PARTIAL", "检查没跑完不等于全部通过"
    with db_session.session_scope() as session:
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        session.expunge_all()
    succeeded = [day.day_no for day in days if day.generation_status == "SUCCEEDED"]
    assert succeeded == [1], "已交付的天必须留着，不被一次失败清空"


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
