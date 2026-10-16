"""成本账本的持久面（V17，上线 v1 T2「建立累计成本预留与结算机制」）。

这里是**唯一**一处把成本写进 MySQL 的实现（`cost_ledger` + `llm_reservation` 两张表）。
两条传输路径共用它，不各写一套：

1. 有规划任务的腿（生成/补齐/恢复）——`RunLimits.persist` 在预算落库前先把本批成本事件交给
   `apply_events`（接缝见 `app/services/planning_tasks.py:_persist`）。
2. 没有任务的腿（澄清、城市引导、对话编辑、NL 编辑、图片/语音意图、POI 补池、agent 直调面）——
   `main.py` lifespan 调 `install_backend()` 把它登记成 `RunLimits` 的成本后端。

于是「每一次消耗平台模型额度的请求」都必然经过 `reserve_llm` → `apply_events`：
准入不是入口自觉调用的闸门，而是外呼接缝本身的性质（忘调 `quota_service` 也照样被挡）。

事务边界刻意用 `own_session()` 而不是 `session_scope()`：预留必须在**请求发出之前**落定，
调用方业务事务的回滚不能把已经发生的成本退掉（否则"失败了所以不算花钱"就是无界重试的门）。
判定的算式全在 `app/common/cost_policy.py`（纯函数、可单测）；本模块只负责把它们写成
一条带守卫的条件更新——**上限比较在 WHERE 里**，所以并发预留落在同一行的 X 锁上排队，
不可能超卖。

本模块只留**账本行的写原语**（预留 / 结算 / 崩溃收口）；入口侧的准入判定（`enforce_windows` /
`admit_repair` / `repair_state` / `record_repair_result`）在同目录 `cost_admission`，它复用这里的
`own_session` / `ensure_row` / `cap_error` / `breaker_error`，不再各写一份事务边界。

权威分工（别处只读、不复制）：账本总额 = `cost_ledger`；单笔预留的生命周期与归属 =
`llm_reservation`；一次交付责任的运行消耗 = `planning_task.consumed_*`（V16）；
每次 HTTP 尝试的明细 = `usage_store`（SQLite，仪表盘/对账用，**不是**准入依据）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent import set_cost_backend
from app.common import cost_policy as policy
from app.common.config import settings
from app.common.cost_policy import CostCeilingError
from app.db.models import CostLedger, LlmReservation
from app.db.session import get_session_factory

logger = logging.getLogger(__name__)

OPEN = policy.OPEN
SETTLED = policy.SETTLED
ABANDONED = policy.ABANDONED


@contextmanager
def own_session() -> Iterator[Session]:
    """成本写自己的事务：提交与否不由调用方的业务事务决定（见模块 docstring 那条取舍）。"""
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def install_backend() -> None:
    """把本模块登记成 `RunLimits` 的成本后端（`main.py` lifespan 调用；agent 不反向依赖 services）。"""
    set_cost_backend(apply_events)


def apply_events(events: list[dict]) -> None:
    """按顺序落一批成本事件（`RunLimits` 在 reserve/settle 时生成）。

    单笔事件一个事务：某笔失败不会把已成功的笔退掉——已经发出去的外呼是真花了钱的。
    """
    for event in events:
        if event.get("op") == "reserve":
            _reserve(event)
        elif event.get("op") == "settle":
            _settle(event)


# ---------------------------------------------------------------------- 预留 ----


def ensure_row(session: Session, scope: policy.Scope) -> None:
    """账本行按需建（并发下让先落库的那条赢，输的继续用同一行——守卫更新才是判据）。"""
    if session.get(CostLedger, scope.key) is not None:
        return
    try:
        with session.begin_nested():
            session.add(
                CostLedger(
                    scope_key=scope.key,
                    scope_kind=scope.kind,
                    window_day=scope.window_day,
                    attribution=scope.attribution,
                )
            )
    except IntegrityError:
        pass


def _guard_reserve(session: Session, scope: policy.Scope, prompt: int, completion: int) -> bool:
    """带守卫的入账：上限写在 WHERE 里，rowcount=0 就是触顶/熔断/预留位已满（不超卖的依据）。"""
    ceiling = policy.ceiling_for(scope.kind, scope.attribution)
    conditions = [CostLedger.scope_key == scope.key, CostLedger.breaker_tripped_at.is_(None)]
    if ceiling.token_cap > 0:
        conditions.append(
            CostLedger.charge_prompt_tokens + CostLedger.charge_completion_tokens + prompt + completion
            <= ceiling.token_cap
        )
    if ceiling.call_cap > 0:
        conditions.append(CostLedger.charged_calls + 1 <= ceiling.call_cap)
    if settings.cost_max_open_reservations > 0:
        conditions.append(CostLedger.open_reservations < settings.cost_max_open_reservations)
    result = session.execute(
        update(CostLedger)
        .where(*conditions)
        .values(
            charge_prompt_tokens=CostLedger.charge_prompt_tokens + prompt,
            charge_completion_tokens=CostLedger.charge_completion_tokens + completion,
            charged_calls=CostLedger.charged_calls + 1,
            open_reservations=CostLedger.open_reservations + 1,
        )
    )
    return int(getattr(result, "rowcount", 0) or 0) > 0


def _rejection(scope: policy.Scope, row: CostLedger | None, prompt: int, completion: int) -> CostCeilingError:
    """把"为什么被挡"说准：熔断 / 预留位满 / token 顶 / 次数顶各给各的稳定码。"""
    if row is not None and row.breaker_tripped_at is not None:
        return breaker_error(scope)
    if row is not None and policy.open_reservations_full(int(row.open_reservations)):
        return CostCeilingError(
            policy.CODE_RESERVATION_FLOOD,
            429,
            f"未结算的模型调用预留已达上限，请稍后再试（{policy.CODE_RESERVATION_FLOOD}）",
        )
    ceiling = policy.ceiling_for(scope.kind, scope.attribution)
    tokens = policy.tokens_of(int(row.charge_prompt_tokens), int(row.charge_completion_tokens)) if row else 0
    calls = int(row.charged_calls) if row else 0
    if ceiling.token_cap > 0 and policy.tokens_of(prompt, completion) + tokens > ceiling.token_cap:
        return cap_error(scope, "token")
    if ceiling.call_cap > 0 and calls + 1 > ceiling.call_cap:
        return cap_error(scope, "call")
    # 两个维度都没超却被守卫拒绝 = 并发里别人刚把窗口填满（或预留位刚被占满），按最贴近的原因回
    return cap_error(scope, "token" if ceiling.token_cap > 0 else "call")


def cap_error(scope: policy.Scope, dimension: str) -> CostCeilingError:
    if scope.kind == policy.PLATFORM:
        return CostCeilingError(
            policy.CODE_PLATFORM_LIMIT,
            429,
            f"平台今日模型{dimension}额度已触顶，已暂停接新请求（{policy.CODE_PLATFORM_LIMIT}）",
        )
    return CostCeilingError(
        policy.CODE_USER_LIMIT,
        429,
        f"今日 AI 用量已达上限，请明天再试（{policy.CODE_USER_LIMIT}）",
    )


def breaker_error(scope: policy.Scope) -> CostCeilingError:
    if scope.kind == policy.PLATFORM:
        return CostCeilingError(
            policy.CODE_PLATFORM_LIMIT,
            429,
            f"平台今日模型用量已熔断，已交付的行程仍可读取与导出（{policy.CODE_PLATFORM_LIMIT}）",
        )
    return CostCeilingError(
        policy.CODE_USER_LIMIT,
        429,
        f"今日 AI 用量已熔断，已交付的行程仍可读取与导出（{policy.CODE_USER_LIMIT}）",
    )


def _trip_breaker(scope: policy.Scope) -> None:
    """触顶即熔断：写进行里，于是"停止发新请求"跨进程、跨重启成立（不是内存标志位）。"""
    with own_session() as session:
        session.execute(
            update(CostLedger)
            .where(CostLedger.scope_key == scope.key, CostLedger.breaker_tripped_at.is_(None))
            .values(breaker_tripped_at=datetime.now())
        )


def _reserve(event: dict) -> None:
    """持久预留：先级联入账（守卫），再落 OPEN 预留行——两者同事务，不会只落一半。

    重放安全：预留行已存在就是这一批事件曾经落定过（`_persist` 的基线只在成功后推进，
    失权/取消会把同一批事件再送一次），此时**不得再扣一遍**，直接当成功返回。
    """
    reservation_id = str(event["id"])
    attribution = str(event.get("attribution") or policy.ATTR_PLATFORM)
    scopes = policy.scopes_for_reserve(event.get("user_id"), attribution)
    prompt = max(0, int(event.get("prompt") or 0))
    completion = max(0, int(event.get("completion") or 0))
    created_at = datetime.now()
    rejection: tuple[policy.Scope, CostLedger | None] | None = None
    with own_session() as session:
        if session.get(LlmReservation, reservation_id) is not None:
            return
        for scope in scopes:
            ensure_row(session, scope)
        for scope in scopes:
            if not _guard_reserve(session, scope, prompt, completion):
                rejection = (scope, session.get(CostLedger, scope.key))
                break
        else:
            session.add(
                LlmReservation(
                    reservation_id=reservation_id,
                    scope_key=scopes[-1].key if scopes else policy.platform_scope(created_at),
                    charged_scopes=",".join(scope.key for scope in scopes),
                    attribution=attribution,
                    attribution_note=event.get("note"),
                    task_id=event.get("task_id"),
                    user_id=event.get("user_id"),
                    itinerary_id=event.get("itinerary_id"),
                    scene=str(event.get("scene") or "other"),
                    reserved_prompt_tokens=prompt,
                    reserved_completion_tokens=completion,
                    state=OPEN,
                    created_at=created_at,
                    expires_at=policy.reservation_expires_at(created_at),
                )
            )
            return
    # 事务已回滚（一笔都没入账），此处只补熔断状态，再抛稳定码
    scope, row = rejection  # type: ignore[misc]
    error = _rejection(scope, row, prompt, completion)
    if error.stable_code in {policy.CODE_PLATFORM_LIMIT, policy.CODE_USER_LIMIT}:
        _trip_breaker(scope)
    raise error


# ---------------------------------------------------------------------- 结算 ----


def _settle(event: dict) -> None:
    """幂等结算：`WHERE state='OPEN'` 的 CAS 保证一笔预留恰好结算一次。

    未命中 = 已被结算或已被崩溃收口（ABANDONED 之后不再接受真值，也不退还额度——
    「结算只一次」与「不得靠崩溃退款」是同一枚硬币的两面）。
    """
    charged_prompt = max(0, int(event.get("prompt") or 0))
    charged_completion = max(0, int(event.get("completion") or 0))
    with own_session() as session:
        row = session.get(LlmReservation, str(event["id"]))
        if row is None or row.state != OPEN:
            return
        result = session.execute(
            update(LlmReservation)
            .where(LlmReservation.reservation_id == row.reservation_id, LlmReservation.state == OPEN)
            .values(
                state=SETTLED,
                charged_prompt_tokens=charged_prompt,
                charged_completion_tokens=charged_completion,
                usage_complete=bool(event.get("usage_complete")),
                settled_at=datetime.now(),
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) == 0:
            return
        _true_up(
            session,
            row.charged_scopes,
            charged_prompt - int(row.reserved_prompt_tokens),
            charged_completion - int(row.reserved_completion_tokens),
            settled=True,
        )


def _true_up(session: Session, scopes: str, delta_prompt: int, delta_completion: int, *, settled: bool) -> None:
    """把每个窗口的账按 (真值 - 预留值) 调整；预留位腾出一格。

    这里**不**带上限守卫：钱已经花了，写不下也得写（守卫只属于预留侧）。
    ABANDONED 走同一函数（delta=0，只腾预留位、记 abandoned），所以 token 账不会凭空消失。
    """
    for key in [item for item in (scopes or "").split(",") if item]:
        column = "settled_count" if settled else "abandoned_reservations"
        values: dict[str, Any] = {
            "charge_prompt_tokens": CostLedger.charge_prompt_tokens + delta_prompt,
            "charge_completion_tokens": CostLedger.charge_completion_tokens + delta_completion,
            "open_reservations": CostLedger.open_reservations - 1,
            column: getattr(CostLedger, column) + 1,
        }
        session.execute(update(CostLedger).where(CostLedger.scope_key == key).values(**values))


def sweep_expired_reservations(limit: int = 200) -> int:
    """崩溃收口：到期仍 OPEN 的预留转 ABANDONED，额度**保留**、预留位腾出、从此不可再结算。

    「有界」的两半都在这里：不结算就不占 `cost_max_open_reservations` 的格子，
    而保留的保守额度让崩溃循环只会更快撞顶，不会变成免费重试。
    """
    now = datetime.now()
    with own_session() as session:
        pending = (
            session.execute(
                select(LlmReservation)
                .where(LlmReservation.state == OPEN, LlmReservation.expires_at <= now)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        targets = [(row.reservation_id, row.charged_scopes) for row in pending]
    swept = 0
    for reservation_id, scopes in targets:
        with own_session() as session:
            result = session.execute(
                update(LlmReservation)
                .where(LlmReservation.reservation_id == reservation_id, LlmReservation.state == OPEN)
                .values(
                    state=ABANDONED,
                    charged_prompt_tokens=LlmReservation.reserved_prompt_tokens,
                    charged_completion_tokens=LlmReservation.reserved_completion_tokens,
                    settled_at=now,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) == 0:
                continue
            _true_up(session, scopes, 0, 0, settled=False)
            swept += 1
    if swept:
        logger.warning("abandoned %d unsettled llm reservations (charged conservatively, not refundable)", swept)
    return swept
