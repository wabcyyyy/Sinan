"""出发前照看（个人 Agent V1）：照看任务的持久状态与归属（S1 状态面）。

规格 §4.1/§4.3：`itinerary_care` 一行 = 一趟行程的照看任务；GET/PUT 只读状态、
保存授权与监控范围，不触发外呼。检查执行（cron 扫描、争抢、风险判定，规格
§4.4 步骤 1–4、9）在 `itinerary_care_check`——cron 与「立即检查」共用同一执行
函数与同一次条件 UPDATE 争抢，本模块只提供它们共用的状态面与资格判定。

边界（规格 §2/§3.2，本模块刻意不做的事）：
- 不伪装生成任务：不写 gen_state、不建 planning_task 行，不调 planning_tasks.acquire；
- 所有者判定一律经 `itinerary_main`（require_owned_main），本表不重复存 user_id；
- 「今天/出发了没」按 Asia/Shanghai 口径（app/common/clock），禁止主机 date.today()；
- 持久时刻按既有约定存应用侧 naive 本地时刻（与 planning_task 同源）；
- lastResult 里的 pendingMessageId 在 GET 时按消息现状派生 pending/applied/stale
  （规格 §4.3，判定与 apply 链同源），存储值本身不复制草稿正文。

依赖：app.services.itinerary_query（归属闸门）；app.common.config/clock；sqlalchemy。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select

from app.agent import CareAffectedDay, CareWatchedItem, care_confirm_thread
from app.common.clock import shanghai_today
from app.common.config import settings
from app.common.envelope import ApiError
from app.common.vo_json import iso_datetime
from app.db.models import ItineraryCare, ItineraryChatMessage, ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.schemas.business.care import CareUpdateBody
from app.services import itinerary_chat, itinerary_query

# 监控范围边界（规格 §3.2）：1–20 个当前行程的 attraction，禁止空范围开启
_MIN_WATCHED = 1
_MAX_WATCHED = 20

# ---- 用户可见状态（last_status，规格 §3.3「简短可理解」；状态面与检查面共用）----
#: 手动暂停（关闭照看保留已呈现草稿，但停止未来工作）
STATUS_DISABLED = "disabled"
#: 争抢令牌在手且未过期——care_state 派生，不落库
STATUS_CHECKING = "checking"
#: 窗口外等待进入（规格 §3.2「在 3 日窗口外保留任务」）
STATUS_WAITING = "waiting"
#: 监控 ID 全部失效，须重新选择
STATUS_NEEDS_RESELECT = "needs_reselect"
#: 天气数据缺失/关闭/适配器 None——不是晴天（规格 §3.3）
STATUS_UNAVAILABLE = "unavailable"
#: 有数据、无影响（含风险日无监控项、风险已提醒过）
STATUS_NO_RISK = "no_risk"
#: 有前台待确认草稿，本轮不发起后台模型调用
STATUS_WAITING_USER = "waiting_for_user"
#: 持久费用硬顶拦截（额度不足）
STATUS_QUOTA = "quota_exhausted"
#: 当日自动 AI 尝试已占用，不重复外呼
STATUS_DEFERRED = "deferred"
#: 建议已发布、待用户确认/忽略（GET 时按消息现状派生为 pending/applied/stale）
STATUS_SUGGESTED = "suggested"
#: 生成期间用户重选监控范围，按旧范围生成的建议作废（发布锁内复核，§4.4 步骤 8）
STATUS_SUPERSEDED = "superseded"
#: 检查执行异常
STATUS_FAILED = "failed"
#: 已出发/归档/删除——照看期结束，离开扫描
STATUS_FINISHED = "finished"
#: 账号禁用
STATUS_ACCOUNT_DISABLED = "account_disabled"


def care_state(user_id: int, itinerary_id: int) -> dict:
    """GET /{id}/care：照看状态投影；只读，不触发外呼，非所有者 404。"""
    main = itinerary_query.find_owned_main(user_id, itinerary_id)
    feature_on = settings.care_enabled and settings.weather_enabled
    with session_scope() as session:
        row = session.get(ItineraryCare, itinerary_id)
        watched: list[int] = [int(item) for item in (row.watched_item_ids_json or [])] if row else []
        current_ids = current_attraction_ids(session, itinerary_id)
        supported = feature_on and eligibility_problem(main, session) is None
        # needsReselect（规格 §3.2）：监控 ID 在行程编辑后全部失效，须重新选择
        needs_reselect = bool(row and row.enabled and watched and not (set(watched) & current_ids))
        # lastResult 不是纯透传：pendingMessageId 存在时按消息现状派生 pending/
        # applied/stale（规格 §4.3，不另建草稿状态机）——清空聊天、被新草稿取代、
        # 已确认应用的消息都不能继续宣称可确认。
        result = _dump_result(row.last_result_json if row else None)
        derived_status = None
        if isinstance(result, dict) and result.get("pendingMessageId") is not None:
            result, derived_status = _derive_message_state(session, int(main.user_id), itinerary_id, result)
        # 「检查中」是争抢令牌的派生状态（token 在手且未过期），不落 last_status——
        # 崩溃残留的 token 过期后自然回到上一个真实状态，不需要清理任务
        status = derived_status or (row.last_status if row else None)
        if (
            row is not None
            and row.check_token
            and row.checking_until is not None
            and row.checking_until > datetime.now()
        ):
            status = STATUS_CHECKING
    return {
        "supported": supported,
        "enabled": bool(row.enabled) if row else False,
        "watchedItemIds": watched,
        "status": status,
        "nextCheckAt": iso_datetime(row.next_check_at if row else None),
        "lastCheckedAt": iso_datetime(row.last_checked_at if row else None),
        "lastResult": result,
        "needsReselect": needs_reselect,
    }


def _derive_message_state(session, owner_id: int, itinerary_id: int, result: dict) -> tuple[dict, str]:
    """按消息现状派生草稿状态（与 apply 链同一判定，itinerary_chat 单一真源）。

    - 消息没了（清空聊天/跨用户不可见）→ stale，不提供消息引用；
    - 草稿已被消费（确认应用/忽略）→ applied；
    - 仍是「当前唯一待确认」→ pending；
    - 被更新的草稿取代（apply 链会 409）→ stale。
    """
    raw_id = result.get("pendingMessageId")
    message_id = int(raw_id) if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
    message = (
        session.execute(
            select(ItineraryChatMessage).where(
                ItineraryChatMessage.id == message_id,
                ItineraryChatMessage.itinerary_id == itinerary_id,
                ItineraryChatMessage.user_id == owner_id,
                ItineraryChatMessage.role == "ai",
            )
        ).scalar_one_or_none()
        if message_id is not None
        else None
    )
    if message is None:
        return {**result, "status": "stale", "pendingMessageId": None}, "stale"
    if not itinerary_chat.has_action_payload(message):
        return {**result, "status": "applied", "pendingMessageId": None}, "applied"
    latest = itinerary_chat.latest_action_message(session, owner_id, itinerary_id)
    if latest is not None and latest.id == message.id:
        return {**result, "status": "pending"}, "pending"
    return {**result, "status": "stale", "pendingMessageId": None}, "stale"


def dismiss_pending(user_id: int, itinerary_id: int) -> dict:
    """POST /{id}/care/dismiss：忽略本表当前 pendingMessageId 对应的 care 草稿（§4.3）。

    只消费这一条草稿（不可再应用），不清空普通聊天草稿；非所有者 404（归属闸门）。
    刻意**不受**全局开关与用户开关约束：关闭照看/功能下线后，已呈现的草稿仍须可
    忽略（§4.3「关闭照看保留已经呈现的草稿供查看或忽略」）。无待忽略对象（无引用、
    消息已删、已应用或线程元数据不是本行程 care 线程）一律幂等返回当前状态。
    """
    itinerary_query.find_owned_main(user_id, itinerary_id)
    with session_scope() as session:
        row = session.get(ItineraryCare, itinerary_id)
        raw = row.last_result_json if row is not None and isinstance(row.last_result_json, dict) else {}
        raw_id = raw.get("pendingMessageId")
        message_id = int(raw_id) if isinstance(raw_id, int) and not isinstance(raw_id, bool) else None
        if message_id is not None:
            message = session.execute(
                select(ItineraryChatMessage).where(
                    ItineraryChatMessage.id == message_id,
                    ItineraryChatMessage.itinerary_id == itinerary_id,
                    ItineraryChatMessage.user_id == user_id,
                    ItineraryChatMessage.role == "ai",
                )
            ).scalar_one_or_none()
            # 只消费确属本行程 care 线程的草稿：该列只由发布写入，这里是防脏数据的
            # 结构性护栏，保证「忽略」永远碰不到用户自己的普通聊天草稿（§4.3/§4.5）
            if (
                message is not None
                and itinerary_chat.has_action_payload(message)
                and itinerary_chat.action_confirmation_thread(message) == care_confirm_thread(itinerary_id)
            ):
                itinerary_chat.consume_pending_action(message)
    return care_state(user_id, itinerary_id)


def update_care(user_id: int, itinerary_id: int, body: CareUpdateBody) -> dict:
    """PUT /{id}/care：保存授权与监控范围；开启须显式 allowBackgroundAi（费用授权）。

    关闭不要求再次授权；关闭保留已呈现结果供查看/忽略，只停止未来工作
    （next_check_at 置空 + 清争抢令牌——在跑检查的结论因令牌被清而无法发布）；
    watched_item_ids 与 last_result 等历史一概保留，不清。
    """
    main = itinerary_query.find_owned_main(user_id, itinerary_id)
    if not settings.care_enabled:
        raise ApiError(404, "行程照看功能未开启")
    watched = _normalize_watched(body.watchedItemIds)
    enabling = bool(body.enabled)
    if body.enabled and body.allowBackgroundAi is not True:
        # 后台 AI 费用授权（规格 §3.2）：开启必须显式 true，缺省/None/false 都不算
        raise ApiError(400, "开启照看前需确认同意后台 AI 建议可能消耗额度")

    with session_scope() as session:
        row = session.get(ItineraryCare, itinerary_id)
        if row is None:
            row = ItineraryCare(itinerary_id=itinerary_id)
            session.add(row)
        if enabling:
            problem = eligibility_problem(main, session)
            if problem:
                raise ApiError(400, problem)
            if not settings.weather_enabled:
                raise ApiError(400, "天气服务未开启，暂时无法照看")
            if watched is None:
                watched = [int(item) for item in (row.watched_item_ids_json or [])]
            if len(watched) < _MIN_WATCHED:
                raise ApiError(400, "请先选择需要关注天气影响的活动")
            stale = [item for item in watched if item not in current_attraction_ids(session, itinerary_id)]
            if stale:
                raise ApiError(400, "监控的活动不存在或已从行程中删除，请重新选择")
            row.enabled = True
            row.watched_item_ids_json = watched
            # 规格 §3.2：开启后 next_check_at 置为当前时间（窗口外等待进入窗口由扫描面判断）
            row.next_check_at = datetime.now()
            row.check_token = None
            row.checking_until = None
        elif body.enabled is False:
            row.enabled = False
            row.next_check_at = None
            row.check_token = None
            row.checking_until = None
            row.last_status = STATUS_DISABLED
            # watched_item_ids 与 last_result/last_status 保留：已呈现草稿仍可查看/忽略
        if watched is not None and not enabling:
            # 仅更新监控范围（无论开关状态）：仍是「当前行程 attraction」的子集校验
            stale = [item for item in watched if item not in current_attraction_ids(session, itinerary_id)]
            if stale:
                raise ApiError(400, "监控的活动不存在或已从行程中删除，请重新选择")
            row.watched_item_ids_json = watched
    return care_state(user_id, itinerary_id)


# ---- 状态面与检查面共用的资格/范围判定（提级为跨模块 API，不带下划线）----


@dataclass
class SuggestionOrder:
    """建议流程门槛全部通过后的执行令（规格 §4.4 步骤 4 的产出）。

    判定管线（itinerary_care_risk）与模型流程/发布（itinerary_care_publish）的
    交接物，放本模块避免两个执行面互相依赖：当日 AI 尝试已被原子占用；去重键
    尚**未**落库——只有发布成功的短事务才写（失败/崩溃不得留键，步骤 8）。
    """

    fresh_keys: list[str]
    #: 受影响日（day_no 升序）连同监控项与观测事实
    affected: list[CareAffectedDay]
    observations: list[dict[str, object]]


def build_suggestion_order(
    *,
    item_day: dict[int, date],
    item_name: dict[int, str],
    day_no_by_date: dict[date, int],
    observations: list[dict[str, object]],
    risk_by_date: dict[date, str],
    affected: list[int],
    fresh_keys: list[str],
) -> SuggestionOrder:
    """受影响日（day_no 升序）连同监控项与观测事实打包（判定管线 → 模型流程）。"""
    observation_by_date: dict[date, dict[str, object]] = {}
    for entry in observations:
        day = _parse_iso(entry.get("date"))
        if day is not None:
            observation_by_date[day] = entry
    affected_days: list[CareAffectedDay] = []
    for risk_day in sorted(risk_by_date):
        if risk_day not in day_no_by_date:
            continue
        items = tuple(
            CareWatchedItem(id=item_id, name=item_name.get(item_id, ""))
            for item_id in affected
            if item_day[item_id] == risk_day
        )
        entry = observation_by_date.get(risk_day, {})
        affected_days.append(
            CareAffectedDay(
                day_no=day_no_by_date[risk_day],
                date=risk_day.isoformat(),
                condition=_as_str(entry.get("condition")),
                precip_prob=_as_float(entry.get("precip_prob")),
                weather_code=_as_int(entry.get("weather_code")),
                items=items,
            )
        )
    return SuggestionOrder(fresh_keys=fresh_keys, affected=affected_days, observations=observations)


def _as_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _as_float(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _parse_iso(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def eligibility_problem(main: ItineraryMain, session) -> str | None:
    """开启资格（规格 §3.2）：有效日期、未出发、完整交付、非归档；不满足返回原因文案。"""
    if main.archived:
        return "已归档的行程不能开启照看"
    if main.start_date is None or main.end_date is None:
        return "行程还没有确定出发日期"
    if main.end_date < main.start_date:
        return "行程日期不合法"
    # 尚未出发：出发当天起停止自动照看（Asia/Shanghai 口径）
    if main.start_date <= shanghai_today():
        return "行程已出发或当天出发，不能再开启照看"
    if main.gen_state != "COMPLETED":
        # 精确 COMPLETED：PARTIAL 是部分交付，不算完整交付（规格 §3.2「完整天交付」）
        return "行程尚未完整生成，暂时不能开启照看"
    day_states = (
        session.execute(select(ItineraryDay.generation_status).where(ItineraryDay.itinerary_id == main.id))
        .scalars()
        .all()
    )
    if not day_states or any(state != "SUCCEEDED" for state in day_states):
        return "行程尚未完整生成，暂时不能开启照看"
    return None


def current_attraction_ids(session, itinerary_id: int) -> set[int]:
    """当前行程仍存在的 attraction item ID（软删行经全局钩子自动排除）。"""
    rows = (
        session.execute(
            select(ItineraryItem.id).where(
                ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.item_type == "attraction"
            )
        )
        .scalars()
        .all()
    )
    return {int(row) for row in rows}


def _normalize_watched(watched: list[int] | None) -> list[int] | None:
    """监控 ID 归一：去重保序、正整数、1–20 个；None=不动现有范围。"""
    if watched is None:
        return None
    seen: dict[int, None] = {}
    for item in watched:
        if not isinstance(item, int) or isinstance(item, bool) or item < 1:
            raise ApiError(400, "监控的活动标识不合法")
        seen.setdefault(item, None)
    unique = list(seen)
    if len(unique) > _MAX_WATCHED:
        raise ApiError(400, f"最多关注 {_MAX_WATCHED} 个活动的天气影响")
    return unique


def _dump_result(raw: object) -> dict | None:
    """last_result_json 透传：非 dict（损坏/历史残留）按无结果处理，不抛给用户。"""
    return raw if isinstance(raw, dict) else None
