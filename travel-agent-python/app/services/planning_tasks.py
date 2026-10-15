"""规划任务的执行生命周期（V16/T1，上线 v1「建立持久化规划任务执行机制」）。

行级读写原语在 `planning_task_store`；本模块只管四件事：

1. **开跑与接管**（`acquire` / `takeover` / `attach`）：拿到一份带绝对截止与累计账本的
   `RunLimits`，并把持久面的两根线注入进去——预算写入（CAS + 续租）与纯写路径的所有权
   探针。注入点是 `RunLimits.persist/guard`，agent 层不反向依赖 services。
2. **停手**：失权或取消后，下一次外呼与落库都会被拒；旧 worker 不再能覆盖新持有者的结论。
3. **终态收敛**（`finish_success` / `finish_failure` / `terminalize_expired` /
   `terminalize_cancelled` / `release`）：已交付天保留为 PARTIAL 并同步重算最终预算；
   执行态（QUEUED/RUNNING → FINISHED/FAILED/CANCELLED/EXPIRED）随句柄脱离一起落，不会只落一半。
4. **取消**（`request_cancel`）：旗子写在持久面，所以"取消一个不在本进程跑的任务"成立。

进程内 `_held` 只用于取回本地正在跑的 `RunLimits` 与定时器，**不是**所有权依据；
所有权依据永远是 `planning_task` 那一行。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

from sqlalchemy import select

from app.agent import RunLimits, TaskCancelled, TaskOwnershipLost
from app.common.config import settings
from app.common.envelope import ApiError, error_code, user_reason
from app.db.models import ItineraryDay, ItineraryMain
from app.db.session import session_scope
from app.services import budget_engine, day_persistence, generation_events, itinerary_query
from app.services import planning_task_store as store

logger = logging.getLogger(__name__)

TaskWindowExpired = store.TaskWindowExpired
LeaseHeld = store.LeaseHeld
FINISHED = store.FINISHED
state_of = store.state_of
is_active = store.is_active
holds_lease = store.holds_lease
is_cancel_pending = store.is_cancel_pending


@dataclass
class _Held:
    """本 worker 手上的一份执行句柄（本地定时器 + 账本增量缓存，不承载所有权）。"""

    limits: RunLimits
    deadline_timer: threading.Timer | None = None
    heartbeat_timer: threading.Timer | None = None
    enriching: bool = False
    lost: bool = False
    outcome: str | None = None
    result_code: str | None = None
    last_counters: dict[str, int] = field(default_factory=dict)
    probe_at: float = 0.0


_lock = threading.RLock()
_held: dict[int, _Held] = {}


def task_limits(itinerary_id: int) -> RunLimits | None:
    """本 worker 正在跑的这份预算（排队与编排线程内部传递用）。"""
    with _lock:
        held = _held.get(itinerary_id)
        return held.limits if held else None


def is_held_local(itinerary_id: int) -> bool:
    """本 worker 手上是否有这份任务——只服务"谁该收尾"，不是所有权判据。"""
    with _lock:
        return itinerary_id in _held


def holds_task(itinerary_id: int) -> bool:
    """当前 worker 是否仍有资格为这趟行程写终态（本地句柄 + 持久租约都成立）。

    终态写入前问一次：超时定时器或恢复接管可能已经把结论写掉，此时旧 worker 的
    `_finish` 会把接管者判的 PARTIAL 覆盖成 COMPLETED——那正是"失权后写回"的产品后果。
    """
    with _lock:
        held = _held.get(itinerary_id)
        if held is None or held.lost:
            return False
        task_id = held.limits.task_id
    return holds_lease(itinerary_id, task_id)


# ------------------------------------------------------------------ 持久面注入 ----


def _persist(itinerary_id: int, task_id: str, snapshot: dict) -> None:
    """预算落库：增量在这里算（与上次落库的快照相比），CAS 与续租在 store 里做。

    增量在内存里比，不多读一行；接管后新 worker 以继承来的计数器为基线，因此不会把
    旧 worker 已花掉的成本再记一遍。
    """
    counters = store.counters_of(snapshot)
    with _lock:
        held = _held.get(itinerary_id)
        base = held.last_counters if held is not None else {}
    deltas = {
        column: counters[key] - int(base.get(key, 0))
        for key, column in store.LIFETIME_COLUMNS.items()
        if counters[key] > int(base.get(key, 0))
    }
    # 基线只在落库成功后才推进：失权/取消时 apply_budget 抛错，这批增量必须留给下一次
    # 写入重试，否则账本会比真实花费少（T2 的费用硬顶正是绑在这些列上）。
    store.apply_budget(itinerary_id, task_id, snapshot, deltas)
    with _lock:
        held = _held.get(itinerary_id)
        if held is not None:
            held.last_counters = counters


def _probe(itinerary_id: int, task_id: str) -> None:
    """失权/取消探针：纯写路径（没有预算变化）也要在缓存窗口后问一次持久面。

    本地句柄不在、或已登记的令牌与这份身份不一致 = 立即拒，不等缓存窗口——
    否则接管后的旧 worker 还能在窗口里多写一次。
    """
    with _lock:
        held = _held.get(itinerary_id)
        if held is None or held.lost or held.limits.task_id != task_id:
            raise store.loss_reason(itinerary_id, task_id)
        stamp = store.now().timestamp()
        if stamp - held.probe_at < settings.planning_write_probe_seconds:
            return
        held.probe_at = stamp
    with session_scope() as session:
        row = store.read(session, itinerary_id)
    if row is None:
        raise TaskOwnershipLost(f"行程 {itinerary_id} 的任务行已不存在")
    if row.cancel_requested_at is not None:
        raise TaskCancelled(f"行程 {itinerary_id} 已被用户取消")
    if row.state not in store.LIVE_STATES or row.task_id != task_id or row.lease_owner != store.WORKER_ID:
        raise TaskOwnershipLost(f"行程 {itinerary_id} 的任务已不属于本 worker")


def _register(held: _Held) -> RunLimits:
    """登记本地句柄，并把持久面的两根线接回这份 RunLimits。"""
    limits = held.limits
    assert limits.itinerary_id is not None and limits.task_id is not None, "运行预算缺少任务身份"
    itinerary_id, task_id = limits.itinerary_id, limits.task_id
    limits.persist = lambda snapshot: _persist(itinerary_id, task_id, snapshot)
    limits.guard = lambda: _probe(itinerary_id, task_id)
    with _lock:
        _held[itinerary_id] = held
    _start_timers(held)
    return limits


# ---------------------------------------------------------------- 定时器与心跳 ----


def _arm(delay: float, target, args: tuple) -> threading.Timer:
    timer = threading.Timer(delay, target, args)
    timer.daemon = True
    timer.start()
    return timer


def _cancel_timers(held: _Held) -> None:
    for timer in (held.deadline_timer, held.heartbeat_timer):
        if timer is not None:
            timer.cancel()


def _start_timers(held: _Held) -> None:
    limits = held.limits
    assert limits.itinerary_id is not None and limits.task_id is not None
    itinerary_id, task_id = limits.itinerary_id, limits.task_id
    remaining = max(0.001, limits.remaining())
    held.deadline_timer = _arm(remaining, _expire, (itinerary_id, task_id))
    held.heartbeat_timer = _arm(
        min(settings.planning_heartbeat_seconds, remaining), _heartbeat, (itinerary_id, task_id)
    )


def _heartbeat(itinerary_id: int, task_id: str) -> None:
    """续租；续不上就说明已被接管或已终态，句柄就地作废（不再续，探针立刻拒）。"""
    if not store.renew_lease(itinerary_id, task_id):
        logger.warning("lease renewal lost for itinerary %s (task %s)", itinerary_id, task_id)
        with _lock:
            held = _held.get(itinerary_id)
            if held is not None and held.limits.task_id == task_id:
                held.lost = True
                _cancel_timers(held)
                _held.pop(itinerary_id, None)
        return
    with _lock:
        held = _held.get(itinerary_id)
        if held is None or held.lost or held.limits.task_id != task_id:
            return
        held.heartbeat_timer = _arm(settings.planning_heartbeat_seconds, _heartbeat, (itinerary_id, task_id))


def _expire(itinerary_id: int, task_id: str) -> None:
    with _lock:
        held = _held.get(itinerary_id)
        if held is None or held.limits.task_id != task_id:
            return
    try:
        finish_failure(itinerary_id, TimeoutError("规划任务超过总时限，请重试"))
    except Exception:
        logger.exception("could not finalize expired task %s", task_id)
    finally:
        release(itinerary_id, task_id)


# ------------------------------------------------------------------ 生命周期 API ----


def acquire(user_id: int, itinerary_id: int) -> RunLimits:
    """开一个新的执行尝试（首次提交，或用户对已终态行程的显式补齐/重建）。

    累计账本不从零开始——重开的是时间窗，不是额度；`submit_at` 只记首次提交，
    之后的 attempt 共享同一条交付责任。
    """
    limits, snapshot = store.open_attempt(user_id, itinerary_id)
    logger.info("planning task acquired: itinerary=%s task=%s", itinerary_id, (limits.task_id or "")[:8])
    return _register(_Held(limits=limits, last_counters=store.counters_of(snapshot)))


def takeover(user_id: int, itinerary_id: int) -> RunLimits:
    """接管一个仍在绝对截止期内的任务（进程重启后的恢复路径）。

    与 `acquire` 只有两条差别：时间窗沿用原来的 `deadline_at`（不重开），计数器从上一个
    worker 落库的快照继续（重启不把已花掉的成本归零）。截止已过抛 `TaskWindowExpired`，
    由调用方终态化——那才是"过期任务终态化"，而不是"重启即过期"。
    """
    limits, snapshot = store.take_over(user_id, itinerary_id)
    logger.info("planning task taken over: itinerary=%s task=%s", itinerary_id, (limits.task_id or "")[:8])
    return _register(_Held(limits=limits, last_counters=store.counters_of(snapshot)))


def attach(limits: RunLimits) -> None:
    """重新绑定一个已由本 worker 持有的任务（接管后再入池的那一步）。

    租约必须还能由本 worker 续上，否则这条任务已被别人占着（或已终态）——拒绝而不是抢占。
    """
    if limits.itinerary_id is None or limits.task_id is None:
        raise ApiError(409, "该行程已有生成任务在运行，请等它结束")
    itinerary_id, task_id = limits.itinerary_id, limits.task_id
    with _lock:
        if itinerary_id in _held:
            return
    if not store.renew_lease(itinerary_id, task_id):
        raise ApiError(409, "该行程已有生成任务在运行，请等它结束")
    _register(_Held(limits=limits, last_counters=store.counters_of(limits.snapshot())))


def mark_running(itinerary_id: int) -> None:
    """排队 → 执行（plan_days 真正开始的那一刻）。"""
    with _lock:
        held = _held.get(itinerary_id)
        task_id = held.limits.task_id if held else None
    if task_id is not None:
        store.mark_running(itinerary_id, task_id)


def release(itinerary_id: int, task_id: str | None = None, *, core_only: bool = False) -> None:
    """交还本地句柄；真正脱离时放租约，有结论就连结论一起落。"""
    with _lock:
        held = _held.get(itinerary_id)
        if held is None or (task_id is not None and held.limits.task_id != task_id):
            return
        if core_only and held.enriching:
            return
        _held.pop(itinerary_id, None)
        _cancel_timers(held)
        owned_task_id, outcome, result_code = held.limits.task_id, held.outcome, held.result_code
    if owned_task_id is None:
        return
    if outcome is None:
        store.clear_lease(itinerary_id, owned_task_id)
        return
    if not store.set_terminal(itinerary_id, owned_task_id, outcome, result_code):
        logger.warning("planning task %s terminal state not written: lease lost", itinerary_id)


def mark_enriching(itinerary_id: int, value: bool) -> None:
    with _lock:
        held = _held.get(itinerary_id)
        if held is not None:
            held.enriching = value


def reset() -> None:
    """丢掉本进程的本地句柄（测试与"模拟进程重启"用；持久行不动）。"""
    with _lock:
        for held in _held.values():
            _cancel_timers(held)
        _held.clear()


# ------------------------------------------------------------------------ 终态 ----


def finish_success(itinerary_id: int) -> None:
    """产品终态已写（COMPLETED/PARTIAL）→ 登记执行结论，租约在句柄脱离时交还。"""
    with _lock:
        held = _held.get(itinerary_id)
        if held is None or held.lost:
            return
        held.outcome = store.FINISHED


def finish_failure(itinerary_id: int, exc: Exception) -> None:
    """失败/取消/超时的收敛：保留已交付天，写产品终态并登记执行结论。

    失权 worker 直接跳过——它已经没有资格再改这趟行程的任何数据（旧结构里这里会照写
    不误，与新持有者的结论互相覆盖）。
    """
    if not holds_task(itinerary_id):
        logger.warning("finish_failure ignored for itinerary %s: lease not held", itinerary_id)
        return
    cancelled = isinstance(exc, TaskCancelled)
    outcome = store.CANCELLED if cancelled else store.EXPIRED if isinstance(exc, TimeoutError) else store.FAILED
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
    with _lock:
        held = _held.get(itinerary_id)
        if held is not None:
            held.outcome = outcome
            held.result_code = code
    itinerary_query.evict_detail(user_id, itinerary_id)


def terminalize_expired(itinerary_id: int, *, completed_days: int) -> None:
    """无持有者时的过期收敛（恢复扫描用），语义见 `_terminalize_without_holder`。"""
    _terminalize_without_holder(
        itinerary_id,
        completed_days=completed_days,
        outcome=store.EXPIRED,
        event_code="TIMEOUT",
        fail_code=None,
        reason="规划任务已超过总时限，请重新生成",
        partial_code="EXPIRED_PARTIAL",
        user_facing=True,
    )


def terminalize_cancelled(itinerary_id: int, *, completed_days: int) -> None:
    """用户已按取消、持有者却当场死掉的收敛：旗子写在持久面，不认它就没人来收敛。

    恢复扫描原先只看租约，这类任务会被每 90s 接管一次、每次都在 `TaskStopped` 上退出，
    CANCELLED 因此是不可达状态（PLAN T1「取消停止后续外呼与写入」+「重启恢复」的交叉洞）。
    """
    _terminalize_without_holder(
        itinerary_id,
        completed_days=completed_days,
        outcome=store.CANCELLED,
        event_code="CANCELLED",
        fail_code="CANCELLED",
        reason="已取消生成",
        partial_code="CANCELLED_PARTIAL",
        user_facing=False,
    )


def _terminalize_without_holder(
    itinerary_id: int,
    *,
    completed_days: int,
    outcome: str,
    event_code: str,
    fail_code: str | None,
    reason: str,
    partial_code: str,
    user_facing: bool,
) -> None:
    """无持有者时的终态收敛：产品终态 + 最终预算 + 执行态一次写完（唯一实现）。

    与 `finish_failure` 同一套交付语义——已交付天保留为 PARTIAL 且必须带最终预算，
    一天没交付才 FAILED。区别只在资格：这里没有活租约需要 CAS（那个 worker 已经没了）。
    """
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            return
        days = main.days
        user_id = int(main.user_id)
    if completed_days:
        budget_engine.recalculate(itinerary_id)
        day_persistence.complete_trip(itinerary_id, False)
        generation_events.complete(
            itinerary_id, "PARTIAL", days, day_persistence.unfinished_day_nos(itinerary_id), None
        )
        store.force_terminal(itinerary_id, outcome, partial_code)
    else:
        day_persistence.fail_trip(itinerary_id, reason, user_facing=user_facing, code=fail_code)
        generation_events.error(itinerary_id, event_code, reason, True)
        store.force_terminal(itinerary_id, outcome, outcome)
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
        row = store.read(session, itinerary_id)
    if row is None:
        raise store.TaskWindowExpired(f"行程 {itinerary_id} 没有任务行")
    if row.state == store.CANCELLED:
        raise ApiError(409, "该行程已取消，请显式重新生成")
    if row.state in store.LIVE_STATES:
        return takeover(user_id, itinerary_id)
    return acquire(user_id, itinerary_id)


def settle_success(itinerary_id: int, task_id: str) -> None:
    """恢复路径把整趟补完之后落执行终态（本地句柄此时已随 `release` 交还）。

    少这一步就会出现"产品 COMPLETED、执行面还挂着 QUEUED/RUNNING（只是租约过期）"，
    于是每轮扫描都重进这条已交付完的行程——终态与可见条目必须一起收敛。
    """
    if not store.set_terminal(itinerary_id, task_id, store.FINISHED, None):
        logger.warning("planning task %s FINISHED not written: lease no longer held", itinerary_id)


def settle_without_holder(itinerary_id: int, outcome: str, code: str | None) -> None:
    """没有活持有者时的执行终态（恢复扫描自己判定"已交付完"）：只收 live 行，不做 CAS。"""
    store.force_terminal(itinerary_id, outcome, code)


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
    with _lock:
        held = _held.get(itinerary_id)
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
