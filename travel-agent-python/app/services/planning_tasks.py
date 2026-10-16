"""规划任务的执行生命周期（V16/T1，上线 v1「建立持久化规划任务执行机制」）。

行级读写原语在 `planning_task_rows`（读与翻译）与 `planning_task_store`（条件更新），
进程内句柄表与两根定时器在 `planning_task_runs`，没有活持有者时的终态收敛在
`planning_task_convergence`；本模块只管四件事：

1. **开跑与接管**（`acquire` / `takeover` / `attach`）：拿到一份带绝对截止与累计账本的
   `RunLimits`，并把持久面的两根线注入进去——预算写入（CAS + 续租）与纯写路径的所有权
   探针。注入点是 `RunLimits.persist/guard`，agent 层不反向依赖 services。
2. **停手**：失权或取消后，下一次外呼与落库都会被拒；旧 worker 不再能覆盖新持有者的结论。
3. **终态收敛**（`finish_success` / `finish_failure` / `release`）：已交付天保留为 PARTIAL
   并同步重算最终预算；执行态（QUEUED/RUNNING → FINISHED/FAILED/CANCELLED/EXPIRED）随句柄
   脱离一起落，不会只落一半。无持有者的那一半见 `planning_task_convergence`。
4. **取消**（`request_cancel`）：旗子写在持久面，所以"取消一个不在本进程跑的任务"成立。

进程内句柄表只用于取回本地正在跑的 `RunLimits` 与定时器，**不是**所有权依据；
所有权依据永远是 `planning_task` 那一行。
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.agent import RunLimits, TaskCancelled, TaskOwnershipLost
from app.common.config import settings
from app.common.envelope import ApiError, error_code, user_reason
from app.db.models import ItineraryDay, ItineraryMain
from app.db.session import session_scope
from app.services import budget_engine, cost_ledger, day_persistence, generation_events, itinerary_query
from app.services import planning_task_rows as rows
from app.services import planning_task_runs as runs
from app.services import planning_task_store as store

logger = logging.getLogger(__name__)

TaskWindowExpired = rows.TaskWindowExpired
LeaseHeld = rows.LeaseHeld
FINISHED = rows.FINISHED
state_of = rows.state_of
is_active = rows.is_active
holds_lease = rows.holds_lease
is_cancel_pending = rows.is_cancel_pending


# ------------------------------------------------------------------ 持久面注入 ----


def _persist(itinerary_id: int, task_id: str, snapshot: dict) -> None:
    """预算落库：增量在这里算（与上次落库的快照相比），CAS 与续租在 store 里做。

    增量在内存里比，不多读一行；接管后新 worker 以继承来的计数器为基线，因此不会把
    旧 worker 已花掉的成本再记一遍。

    成本事件（V17/T2）先于预算 CAS 落账，且用 `cost_ledger` 自己的事务：
    **预留必须在请求发出之前定稿**，而预算 CAS 失败意味着"本 worker 失权"，不是
    "这笔钱没花"——把两者捆成一个事务会让失权 worker 的真实外呼从硬费用账上消失，
    正是 PLAN §5 T2 要堵的那类漏账。反过来（先 CAS 再落账）则会出现"请求已发出、
    账还没落"的窗口。失权时这批事件会随 `_save` 抛错留在内存里，下一次写入重试；
    `cost_ledger._reserve` 按预留主键幂等，重放不会重复扣。
    """
    events = snapshot.get("cost_events") or []
    if events:
        cost_ledger.apply_events(list(events))
    counters = rows.counters_of(snapshot)
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        base = held.last_counters if held is not None else {}
    deltas = {
        column: counters[key] - int(base.get(key, 0))
        for key, column in rows.LIFETIME_COLUMNS.items()
        if counters[key] > int(base.get(key, 0))
    }
    # 基线只在落库成功后才推进：失权/取消时 apply_budget 抛错，这批增量必须留给下一次
    # 写入重试，否则账本会比真实花费少（T2 的费用硬顶正是绑在这些列上）。
    store.apply_budget(itinerary_id, task_id, snapshot, deltas)
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is not None:
            held.last_counters = counters


def _probe(itinerary_id: int, task_id: str) -> None:
    """失权/取消探针：纯写路径（没有预算变化）也要在缓存窗口后问一次持久面。

    本地句柄不在、或已登记的令牌与这份身份不一致 = 立即拒，不等缓存窗口——
    否则接管后的旧 worker 还能在窗口里多写一次。
    """
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is None or held.lost or held.limits.task_id != task_id:
            raise rows.loss_reason(itinerary_id, task_id)
        stamp = rows.now().timestamp()
        if stamp - held.probe_at < settings.planning_write_probe_seconds:
            return
        held.probe_at = stamp
    with session_scope() as session:
        row = rows.read(session, itinerary_id)
    if row is None:
        raise TaskOwnershipLost(f"行程 {itinerary_id} 的任务行已不存在")
    if row.cancel_requested_at is not None:
        raise TaskCancelled(f"行程 {itinerary_id} 已被用户取消")
    if row.state not in rows.LIVE_STATES or row.task_id != task_id or row.lease_owner != rows.WORKER_ID:
        raise TaskOwnershipLost(f"行程 {itinerary_id} 的任务已不属于本 worker")


def _register(held: runs.Handle) -> RunLimits:
    """登记本地句柄，并把持久面的两根线接回这份 RunLimits。"""
    limits = held.limits
    assert limits.itinerary_id is not None and limits.task_id is not None, "运行预算缺少任务身份"
    itinerary_id, task_id = limits.itinerary_id, limits.task_id
    limits.persist = lambda snapshot: _persist(itinerary_id, task_id, snapshot)
    limits.guard = lambda: _probe(itinerary_id, task_id)
    with runs.LOCK:
        runs.HELD[itinerary_id] = held
    runs.start_timers(held, _expire)
    return limits


def _expire(itinerary_id: int, task_id: str) -> None:
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is None or held.limits.task_id != task_id:
            return
    try:
        finish_failure(itinerary_id, TimeoutError("规划任务超过总时限，请重试"))
    except Exception:
        logger.exception("could not finalize expired task %s", task_id)
    finally:
        release(itinerary_id, task_id)


# ------------------------------------------------------------------ 生命周期 API ----


def acquire(user_id: int, itinerary_id: int, *, repair: bool = False) -> RunLimits:
    """开一个新的执行尝试（首次提交，或用户对已终态行程的显式补齐/重建）。

    累计账本不从零开始——重开的是时间窗，不是额度；`submit_at` 只记首次提交，
    之后的 attempt 共享同一条交付责任。

    `repair=True` 表示这份 attempt 是「对已结算责任的后台重拉」（= 一次补齐）：
    脱离句柄时按交付结果登记退避状态（`cost_ledger.record_repair_result`）。
    """
    limits, snapshot = store.open_attempt(user_id, itinerary_id)
    logger.info("planning task acquired: itinerary=%s task=%s", itinerary_id, (limits.task_id or "")[:8])
    return _register(runs.Handle(limits=limits, last_counters=rows.counters_of(snapshot), repair=repair))


def takeover(user_id: int, itinerary_id: int) -> RunLimits:
    """接管一个仍在绝对截止期内的任务（进程重启后的恢复路径）。

    与 `acquire` 只有两条差别：时间窗沿用原来的 `deadline_at`（不重开），计数器从上一个
    worker 落库的快照继续（重启不把已花掉的成本归零）。截止已过抛 `TaskWindowExpired`，
    由调用方终态化——那才是"过期任务终态化"，而不是"重启即过期"。
    """
    limits, snapshot = store.take_over(user_id, itinerary_id)
    logger.info("planning task taken over: itinerary=%s task=%s", itinerary_id, (limits.task_id or "")[:8])
    return _register(runs.Handle(limits=limits, last_counters=rows.counters_of(snapshot)))


def attach(limits: RunLimits) -> None:
    """重新绑定一个已由本 worker 持有的任务（接管后再入池的那一步）。

    租约必须还能由本 worker 续上，否则这条任务已被别人占着（或已终态）——拒绝而不是抢占。
    """
    if limits.itinerary_id is None or limits.task_id is None:
        raise ApiError(409, "该行程已有生成任务在运行，请等它结束")
    itinerary_id, task_id = limits.itinerary_id, limits.task_id
    with runs.LOCK:
        if itinerary_id in runs.HELD:
            return
    if not store.renew_lease(itinerary_id, task_id):
        raise ApiError(409, "该行程已有生成任务在运行，请等它结束")
    _register(runs.Handle(limits=limits, last_counters=rows.counters_of(limits.snapshot())))


def mark_running(itinerary_id: int) -> None:
    """排队 → 执行（plan_days 真正开始的那一刻）。"""
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        task_id = held.limits.task_id if held else None
    if task_id is not None:
        store.mark_running(itinerary_id, task_id)


def release(itinerary_id: int, task_id: str | None = None, *, core_only: bool = False) -> None:
    """交还本地句柄；真正脱离时放租约，有结论就连结论一起落。"""
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is None or (task_id is not None and held.limits.task_id != task_id):
            return
        if core_only and held.enriching:
            return
        runs.HELD.pop(itinerary_id, None)
        runs.cancel_timers(held)
        owned_task_id, outcome, result_code = held.limits.task_id, held.outcome, held.result_code
    if owned_task_id is None:
        return
    if outcome is None:
        store.clear_lease(itinerary_id, owned_task_id)
        return
    if not store.set_terminal(itinerary_id, owned_task_id, outcome, result_code):
        logger.warning("planning task %s terminal state not written: lease lost", itinerary_id)


def mark_enriching(itinerary_id: int, value: bool) -> None:
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is not None:
            held.enriching = value


def reset() -> None:
    """丢掉本进程的本地句柄（测试与"模拟进程重启"用；持久行不动）。"""
    with runs.LOCK:
        for held in runs.HELD.values():
            runs.cancel_timers(held)
        runs.HELD.clear()


# ------------------------------------------------------------------------ 终态 ----


def finish_success(itinerary_id: int) -> None:
    """产品终态已写（COMPLETED/PARTIAL）→ 登记执行结论，租约在句柄脱离时交还。"""
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is None or held.lost:
            return
        held.outcome = FINISHED


def finish_failure(itinerary_id: int, exc: Exception) -> None:
    """失败/取消/超时的收敛：保留已交付天，写产品终态并登记执行结论。

    失权 worker 直接跳过——它已经没有资格再改这趟行程的任何数据（旧结构里这里会照写
    不误，与新持有者的结论互相覆盖）。
    """
    if not runs.holds_task(itinerary_id):
        logger.warning("finish_failure ignored for itinerary %s: lease not held", itinerary_id)
        return
    cancelled = isinstance(exc, TaskCancelled)
    outcome = rows.CANCELLED if cancelled else rows.EXPIRED if isinstance(exc, TimeoutError) else rows.FAILED
    code = "CANCELLED" if cancelled else error_code(exc)
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None or main.status != 1:
            return
        days = main.days
        user_id = int(main.user_id)
        delivered = session.scalar(
            select(ItineraryDay.id).where(
                ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.generation_status == "SUCCEEDED"
            )
        )
    if delivered:
        # 部分交付也必须有最终预算；后台队列可能未执行或已满。
        budget_engine.recalculate(itinerary_id)
        day_persistence.complete_trip(itinerary_id, False)
        generation_events.complete(
            itinerary_id, "PARTIAL", days, day_persistence.unfinished_day_nos(itinerary_id), None
        )
    else:
        reason = "已取消生成" if cancelled else str(exc)
        day_persistence.fail_trip(itinerary_id, reason, code=code)
        generation_events.error(itinerary_id, code, reason if cancelled else user_reason(exc), True)
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is not None:
            held.outcome = outcome
            held.result_code = code
    itinerary_query.evict_detail(user_id, itinerary_id)


def resume_or_restart(user_id: int, itinerary_id: int) -> RunLimits:
    """恢复扫描的"接着跑"：三种责任状态各有口径，不能一律当接管。

    - 行还 live 但租约已过期 = 持有者死了 → `takeover`（沿用绝对截止，继承账本）。
    - 行已结算（FINISHED/FAILED/EXPIRED）而产品面仍可补齐 = 上一份交付责任已经结束
      → 开**新 attempt**：重开的是时间窗，`consumed_*` 不清零，所以后台重拉同样拿不到
      新额度。把它当接管会让执行态停在终态、预算却在继续花——两份状态各自为政。
    - 行是 CANCELLED = 用户明确取消过 → 后台不自动复活（"取消停止后续外呼"必须是真的）。
    """
    with session_scope() as session:
        row = rows.read(session, itinerary_id)
    if row is None:
        raise TaskWindowExpired(f"行程 {itinerary_id} 没有任务行")
    if row.state == rows.CANCELLED:
        raise ApiError(409, "该行程已取消，请显式重新生成")
    if row.state in rows.LIVE_STATES:
        return takeover(user_id, itinerary_id)
    return acquire(user_id, itinerary_id)


def request_cancel(user_id: int, itinerary_id: int) -> dict:
    """用户取消：只置旗，实际停手由持有者在下一次预算写入/落库探针处完成。

    旗子写在持久面，所以"取消一个不在本进程跑的任务"同样成立；已交付的天不会被删，
    交付到哪一步就停在哪一步（终态由持有者的 `finish_failure` 收敛）。
    """
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None or main.user_id != user_id:
            raise ApiError(404, "行程不存在")
    flagged = store.flag_cancel(itinerary_id)
    if flagged is None:
        raise ApiError(409, "该行程当前没有正在进行的生成任务")
    with runs.LOCK:
        held = runs.HELD.get(itinerary_id)
        if held is not None and held.limits.task_id == flagged["taskId"]:
            held.probe_at = 0.0  # 让下一个检查点立刻读到旗子
    logger.info("planning task cancel requested: itinerary=%s", itinerary_id)
    return {
        "itineraryId": itinerary_id,
        "cancelRequested": True,
        "attempt": flagged["attempt"],
        "state": flagged["state"],
        "undeliveredDays": list(day_persistence.unfinished_day_nos(itinerary_id)),
    }
