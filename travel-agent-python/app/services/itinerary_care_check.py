"""出发前照看的调度、争抢与收尾（个人 Agent V1，规格 §4.4 步骤 1–4、9 + §3.2 两条入口）。

cron 扫描与「立即检查」共用同一执行入口（execute_check）与同一次条件 UPDATE
争抢（_claim_check）：条件 UPDATE 抢 `check_token`/`checking_until`，抢不到就当
没拿到执行权。判定在 `itinerary_care_risk.evaluate_check`（步骤 1–4，纯判定）；
门槛全过返回 `SuggestionOrder` 后，模型流程与发布短事务（步骤 5–9）在
`itinerary_care_publish.run_suggestion`（函数内懒导入破环：那边复用本模块的
token 条件收尾，这边不在模块级依赖执行面）。

崩溃恢复语义（步骤 9）：令牌过期后可重领，无需恢复中途推理；被顶掉的旧持有者
写入自然落空。所有收尾都以 token 为条件——暂停/关闭照看会清令牌（update_care），
此后本次检查的任何结论都写不进去，规格 §3.2「结果不得再发布」在持久层成立。

依赖：app.services.itinerary_care（状态面）；itinerary_care_risk（判定管线）；
itinerary_chat（有界 chat_pool）；app.common.config。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update

from app.common.config import settings
from app.common.envelope import ApiError
from app.db.models import ItineraryCare, ItineraryMain
from app.db.session import session_scope
from app.services import itinerary_chat, itinerary_query
from app.services.itinerary_care import STATUS_CHECKING, STATUS_FAILED, care_state
from app.services.itinerary_care_risk import (
    CheckOutcome,
    SuggestionOrder,
    evaluate_check,
    parse_iso_date,
    result_payload,
)

logger = logging.getLogger(__name__)

# ---- 检查节奏（规格 §3.2，代码常量不加旋钮）----
#: 同一行程两次检查的间隔：尊重天气适配器 6h 正缓存
CHECK_INTERVAL = timedelta(hours=6)
#: 争抢截止：进程崩溃后过期 token 可重领，无需恢复中途推理（规格 §4.4 步骤 9）
CLAIM_MINUTES = 10
#: cron 每 5 分钟扫描一次，一次最多处理 5 个到期项，余项留后续 tick
SCAN_INTERVAL_SECONDS = 300
MAX_PER_SCAN = 5


def request_check(user_id: int, itinerary_id: int) -> dict:
    """POST /{id}/care/check：同入口争抢并提交一次检查（规格 §4.3），调用方返回 202。

    正在检查（token 在手未过期）返回当前状态，不创建第二份；chat_pool 满载返回
    现有繁忙错误并释放本次争抢（规格 §3.2）。
    """
    itinerary_query.find_owned_main(user_id, itinerary_id)
    if not settings.care_enabled:
        raise ApiError(404, "行程照看功能未开启")
    with session_scope() as session:
        row = session.get(ItineraryCare, itinerary_id)
        if row is None or not row.enabled:
            raise ApiError(400, "请先开启出发前照看")
    state = care_state(user_id, itinerary_id)
    if state["status"] == STATUS_CHECKING:
        return state
    token = _claim_check(itinerary_id)
    if token is None:
        # 竞态对手刚赢下同一次争抢 = 正在检查：同样返回当前状态，不创建第二份
        return care_state(user_id, itinerary_id)
    try:
        itinerary_chat.chat_pool.submit(execute_check, itinerary_id, token)
    except itinerary_chat.TaskRejected:
        _release_check(itinerary_id, token)
        raise ApiError(429, itinerary_chat.BUSY_MESSAGE) from None
    return care_state(user_id, itinerary_id)


def scan_due_checks(limit: int = MAX_PER_SCAN) -> int:
    """cron 入口（main.py 每 5 分钟登记）：到期项串行执行，单次最多 limit 项（规格 §3.2）。

    串行是刻意的：单次检查只有一次城市级天气查询（6h 缓存）加一次 ≤4 调用的有界
    模型流程（90 秒截止，见 itinerary_care_publish），吞吐足够；并发安全完全收敛到
    `_claim_check` 的条件 UPDATE，不需要额外协调。
    """
    if not settings.care_enabled:
        return 0
    now = datetime.now()
    with session_scope() as session:
        due_ids = (
            session.execute(
                select(ItineraryCare.itinerary_id)
                .where(
                    ItineraryCare.enabled.is_(True),
                    ItineraryCare.next_check_at.is_not(None),
                    ItineraryCare.next_check_at <= now,
                )
                .order_by(ItineraryCare.next_check_at)
                .limit(limit)
            )
            .scalars()
            .all()
        )
    claimed = 0
    for itinerary_id in due_ids:
        token = _claim_check(int(itinerary_id))
        if token is None:
            continue  # 立即检查（或未过期令牌）持有执行权：跳过，余项留后续 tick
        claimed += 1
        execute_check(int(itinerary_id), token)
    return claimed


def execute_check(itinerary_id: int, token: str) -> None:
    """持锁执行一次检查（cron 与立即检查共用；规格 §4.4 步骤 1–9）。

    任何意外异常只落简短 failed 状态并条件释放令牌，不吞池/调度线程（步骤 7
    「失败只留下简短可理解状态」）。
    """
    try:
        outcome = evaluate_check(itinerary_id, token)
        if isinstance(outcome, SuggestionOrder):
            from app.services.itinerary_care_publish import run_suggestion  # 懒导入破环（见模块 docstring）

            run_suggestion(itinerary_id, token, outcome)
        elif outcome is not None:
            finalize_check(itinerary_id, token, outcome)
    except Exception:
        logger.exception("care check failed for itinerary %s", itinerary_id)
        try:
            finalize_check(
                itinerary_id,
                token,
                CheckOutcome(
                    status=STATUS_FAILED, result=result_payload(STATUS_FAILED, "检查未能完成，将等待下一窗口")
                ),
            )
        except Exception:
            logger.exception("care check finalize failed for itinerary %s", itinerary_id)


def _claim_check(itinerary_id: int) -> str | None:
    """条件 UPDATE 争抢（规格 §4.4 步骤 1）：enabled 且令牌空闲/过期才发新令牌。

    rowcount=0 = 已暂停、已被占用或令牌未过期——调用方必须当「没拿到执行权」处理。
    """
    token = uuid.uuid4().hex
    now = datetime.now()
    with session_scope() as session:
        result = session.execute(
            update(ItineraryCare)
            .where(
                ItineraryCare.itinerary_id == itinerary_id,
                ItineraryCare.enabled.is_(True),
                or_(
                    ItineraryCare.check_token.is_(None),
                    ItineraryCare.checking_until.is_(None),
                    ItineraryCare.checking_until <= now,
                ),
            )
            .values(check_token=token, checking_until=now + timedelta(minutes=CLAIM_MINUTES))
        )
    return token if getattr(result, "rowcount", 0) == 1 else None


def _release_check(itinerary_id: int, token: str) -> None:
    """条件释放：令牌仍是自己的才清（被暂停/顶掉/重领之后不动别人的锁）。"""
    with session_scope() as session:
        session.execute(
            update(ItineraryCare)
            .where(ItineraryCare.itinerary_id == itinerary_id, ItineraryCare.check_token == token)
            .values(check_token=None, checking_until=None)
        )


def finalize_check(itinerary_id: int, token: str, outcome: CheckOutcome) -> None:
    """收尾（规格 §4.4 步骤 9）：清令牌、推进 next_check_at、落结果——全部以 token 为条件。

    判定面（execute_check）与模型/发布面（itinerary_care_publish）共用的落库收尾，
    公开命名：发布短事务要在同事务里落结论。崩溃恢复语义：令牌过期后可重领，无需
    恢复中途推理；被顶掉的旧持有者写入自然落空。当日 AI 尝试的占用写在判定管线的
    `_claim_ai_day`（外呼前独立事务），不在收尾重复。
    """
    if outcome.release_only:
        with session_scope() as session:
            statement = update(ItineraryCare).where(
                ItineraryCare.itinerary_id == itinerary_id, ItineraryCare.check_token == token
            )
            session.execute(statement.values(check_token=None, checking_until=None))
            return
    with session_scope() as session:
        finalize_check_in_session(session, itinerary_id, token, outcome)


def finalize_check_in_session(session, itinerary_id: int, token: str, outcome: CheckOutcome) -> None:
    """`finalize_check` 的会话内变体：发布短事务在同事务里完成收尾（步骤 8+9 同一事务）。"""
    now = datetime.now()
    statement = update(ItineraryCare).where(
        ItineraryCare.itinerary_id == itinerary_id, ItineraryCare.check_token == token
    )
    values: dict[str, object] = {
        "check_token": None,
        "checking_until": None,
        "last_checked_at": now,
        "next_check_at": None if outcome.stop else now + CHECK_INTERVAL,
    }
    if outcome.status is not None:
        values["last_status"] = outcome.status
    if outcome.result is not None:
        values["last_result_json"] = outcome.result
    if outcome.risk_keys:
        values["notified_risks_json"] = _merged_keys(session, itinerary_id, outcome.risk_keys)
    session.execute(statement.values(**values))


def _merged_keys(session, itinerary_id: int, fresh: list[str]) -> list[str]:
    """既有键合并本次新键，只保留行程日期范围内的键（规格 §4.1 的边界）。"""
    row = session.get(ItineraryCare, itinerary_id)
    main = session.get(ItineraryMain, itinerary_id)
    existing = (
        [str(key) for key in row.notified_risks_json] if row and isinstance(row.notified_risks_json, list) else []
    )
    start = main.start_date if main else None
    end = main.end_date if main else None

    def in_range(key: str) -> bool:
        if start is None or end is None:
            return True
        day = parse_iso_date(key.split(":", 1)[0])
        return day is not None and start <= day <= end

    seen: dict[str, None] = {}
    for key in [*existing, *fresh]:
        if in_range(key):
            seen.setdefault(key, None)
    return list(seen)
