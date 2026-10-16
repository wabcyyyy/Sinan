"""成本准入（V17/T2 的入口侧）：读账本判"今天还能不能用"，写账本记"补齐额度与退避"。

与 `cost_ledger` 的分工：那边是**账本行的写原语**（预留/结算/收口，钱怎么落账），
这边是**准入判定**（要不要放这一次外呼进去）。两边都不编造人民币金额，算式全在
`app/common/cost_policy.py`。

两条职责边界要留住：

- `enforce_windows` 是**只读**的硬顶检查，不做预留。真预留仍在 `reserve_llm` 里逐笔做
  （守卫写在 UPDATE 的 WHERE 里，见 `cost_ledger`）；这里只是让入口能干净地回 429，
  而不是等外呼中途炸。与 `quota_service` 的滑动窗分工：滑动窗管速率（Redis 可降级），
  本函数管钱（MySQL 账本，Redis 熔断或切回都动不了它）。
- `admit_repair` / `record_repair_result` 写在同一张 `cost_ledger` 行的 `repairs` /
  `repair_failures` / `backoff_until` 三列上：行程级累计额度跨 attempt、跨用户点击只增，
  免扣的是用户"每日生成次数"（P0-3 产品硬线），**不免扣**补齐额度与窗口成本。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.common import cost_policy as policy
from app.common.config import settings
from app.common.cost_policy import CostCeilingError
from app.db.models import CostLedger
from app.services import cost_ledger


def _read_row(session: Session, key: str) -> CostLedger | None:
    return session.get(CostLedger, key)


def enforce_windows(user_id: int) -> None:
    """烧模型额度入口的**持久**硬顶检查（只读，不预留）。

    与 `quota_service` 的滑动窗分工：滑动窗管"每分钟点几次"（速率，Redis 可降级、丢了只是
    放松速率），本函数管"今天到底花了多少"（钱，MySQL 账本，Redis 熔断或切回都动不了它）。
    真预留仍在 `reserve_llm` 里逐笔做——这里是让入口能干净地回 429，而不是等外呼中途炸。
    """
    at = datetime.now()
    platform = policy.platform_window(at)
    with cost_ledger.own_session() as session:
        row = _read_row(session, platform.key)
        if row is not None and row.breaker_tripped_at is not None:
            raise cost_ledger.breaker_error(platform)
        for attribution in (policy.ATTR_PLATFORM, policy.ATTR_BYOK):
            scope = policy.user_window(user_id, at, attribution)
            user_row = _read_row(session, scope.key)
            if user_row is None:
                continue
            if user_row.breaker_tripped_at is not None:
                raise cost_ledger.breaker_error(scope)
            ceiling = policy.ceiling_for(scope.kind, attribution)
            tokens = policy.tokens_of(int(user_row.charge_prompt_tokens), int(user_row.charge_completion_tokens))
            if ceiling.token_cap > 0 and tokens >= ceiling.token_cap:
                raise cost_ledger.cap_error(scope, "token")
            if ceiling.call_cap > 0 and int(user_row.charged_calls) >= ceiling.call_cap:
                raise cost_ledger.cap_error(scope, "call")


def repair_state(itinerary_id: int) -> dict:
    """行程补齐额度现状（端点/运营读取；不含任何人民币）。"""
    scope = policy.repair_window(itinerary_id)
    with cost_ledger.own_session() as session:
        row = _read_row(session, scope.key)
        if row is None:
            return {
                "repairs": 0,
                "allowance": settings.trip_repair_allowance,
                "repairFailures": 0,
                "backoffUntil": None,
            }
        return {
            "repairs": int(row.repairs),
            "allowance": settings.trip_repair_allowance,
            "repairFailures": int(row.repair_failures),
            "backoffUntil": row.backoff_until.isoformat() if row.backoff_until else None,
        }


def admit_repair(user_id: int, itinerary_id: int) -> None:
    """补齐准入：行程累计额度 + 连续失败退避（持久、原子），并先过窗口硬顶。

    免扣的是用户「每日生成次数」（P0-3 产品硬线，`quota_service.enforce_llm_rate_only`
    原样保留），**不免扣**补齐额度与窗口成本——这就是 PLAN §5 T2 要的
    「保留失败天补齐免扣日配额，但增加行程累计补齐额度、失败退避、平台日总额及熔断」。
    """
    enforce_windows(user_id)
    at = datetime.now()
    scope = policy.repair_window(itinerary_id)
    rejected: CostLedger | None = None
    with cost_ledger.own_session() as session:
        cost_ledger.ensure_row(session, scope)
        row = _read_row(session, scope.key)
        assert row is not None
        problem = policy.repair_problem(int(row.repairs), row.backoff_until, at)
        if problem is None:
            conditions = [
                CostLedger.scope_key == scope.key,
                (CostLedger.backoff_until.is_(None)) | (CostLedger.backoff_until <= at),
            ]
            if settings.trip_repair_allowance > 0:
                conditions.append(CostLedger.repairs < settings.trip_repair_allowance)
            result = session.execute(
                update(CostLedger).where(*conditions).values(repairs=CostLedger.repairs + 1, updated_at=at)
            )
            if int(getattr(result, "rowcount", 0) or 0) > 0:
                return
            rejected = _read_row(session, scope.key)
        else:
            rejected = row
    raise _repair_error(rejected, at)


def _repair_error(row: CostLedger | None, at: datetime) -> CostCeilingError:
    """把补齐被挡的原因说准：额度用尽 vs 退避未到，用户文案与等待秒数都不同。"""
    backoff_until = row.backoff_until if row is not None else None
    failures = int(row.repair_failures) if row is not None else 0
    if backoff_until is not None and at < backoff_until:
        wait = int((backoff_until - at).total_seconds()) + 1
        return CostCeilingError(
            policy.CODE_REPAIR_BACKOFF,
            429,
            f"这一趟行程已连续 {failures} 次补齐失败，请 {wait} 秒后再试（{policy.CODE_REPAIR_BACKOFF}）",
        )
    allowance = settings.trip_repair_allowance
    return CostCeilingError(
        policy.CODE_REPAIR_LIMIT,
        429,
        f"这一趟行程的补齐次数已达上限 {allowance} 次，请用对话调整或重新生成整趟行程（{policy.CODE_REPAIR_LIMIT}）",
    )


def record_repair_result(itinerary_id: int, *, succeeded: bool) -> None:
    """补齐结果登记：成功清零连续失败（退避随之解除），失败按指数退避推后下一次。"""
    scope = policy.repair_window(itinerary_id)
    at = datetime.now()
    with cost_ledger.own_session() as session:
        cost_ledger.ensure_row(session, scope)
        if succeeded:
            session.execute(
                update(CostLedger)
                .where(CostLedger.scope_key == scope.key)
                .values(repair_failures=0, backoff_until=None, updated_at=at)
            )
            return
        row = _read_row(session, scope.key)
        failures = int(row.repair_failures if row is not None else 0) + 1
        delay = policy.backoff_seconds(failures)
        session.execute(
            update(CostLedger)
            .where(CostLedger.scope_key == scope.key)
            .values(repair_failures=failures, backoff_until=at + timedelta(seconds=delay), updated_at=at)
        )
