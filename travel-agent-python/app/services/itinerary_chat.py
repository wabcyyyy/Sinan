"""对话记忆与乐观并发（移植自 Java `ItineraryChatService` 的读/失效部分）。

`baseRevision` 是对当前 plans 的 SHA-256 指纹，写在进行中的草稿里；应用方案时若指纹
与当前行程不符即 409（草稿过期）。因此 **canonical JSON 的形状必须与 Java 一致**：
键顺序按下表逐字段固定、Decimal 按 BigDecimal 的原样数字输出（`30.220000` 不缩成
`30.22`）、时间用 `LocalTime.toString()` 的省略规则。做不到完全一致也不会损坏数据，
后果是"跨服务时草稿被判失效、用户重新生成一次"（fail-closed），比误判为有效安全。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.agent import confirm_thread, observe_run, run_chat_turn, use_scene
from app.common import cache_store, event_hub, event_publisher
from app.common.envelope import ApiError, user_reason
from app.common.task_pool import SlotExecutor, TaskRejected
from app.db.models import (
    BudgetDetail,
    ItineraryChatMessage,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
)
from app.db.session import session_scope
from app.schemas.trip import ChatTurnRequest
from app.schemas.trip_requirements import TripRequirements
from app.services import expense_service, itinerary_city, itinerary_query, llm_gateway_service

logger = logging.getLogger(__name__)

# agent 侧 history 上限（ChatTurnRequest.max_length=20），与 Java 的 subList 同式
HISTORY_WINDOW = 20

# 预算口径币种：全站预算与 BudgetDetail 均为 CNY；账目可记其他币种，但不与预算
# 直接比较（C3.4 只聚合 CNY 进对话上下文，其他币种仅列币种码提示用户）。
BUDGET_CURRENCY = "CNY"


def _escape(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            # 与 Jackson 默认一致：非 ASCII 不转义，直接输出 UTF-8
            out.append(ch)
    out.append('"')
    return "".join(out)


def canonical_json(value: Any) -> str:
    """确定性 JSON：键序即插入序、Decimal 原样输出（保留标度）、非 ASCII 不转义。

    标准库做不到"把 Decimal 写成裸数字"（`default=` 的返回值仍会被加引号），
    而 Java 侧 `BigDecimal(30.220000)` 序列化正是不带引号且保留尾零的，
    指纹要跨语言一致就必须自己写。
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError("计划指纹不接受 float，请用 Decimal 以保持与 BigDecimal 一致")
    if isinstance(value, datetime):
        return _escape(value.isoformat())
    if isinstance(value, (date, time)):
        return _escape(value.isoformat())
    if isinstance(value, str):
        return _escape(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{canonical_json(str(k))}:{canonical_json(v)}" for k, v in value.items()) + "}"
    return _escape(str(value))


def plan_revision(plans: Iterable[dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(list(plans)).encode("utf-8")).hexdigest()


def read_json_list(raw: str | None) -> list[Any]:
    if not raw or not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def current_plans(itinerary_id: int) -> list[dict[str, Any]]:
    """按 Java `currentPlans` 的字段顺序生成 plans（指纹依赖该顺序）。"""
    with session_scope() as session:
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        items = (
            session.execute(
                select(ItineraryItem)
                .where(ItineraryItem.itinerary_id == itinerary_id)
                .order_by(ItineraryItem.day_id, ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        )

    by_day: dict[int, list[ItineraryItem]] = {}
    for item in items:
        by_day.setdefault(item.day_id, []).append(item)

    plans: list[dict[str, Any]] = []
    for day in days:
        rows = [
            {
                "id": item.id,
                "item_type": item.item_type,
                "poi_name": item.poi_name,
                "poi_id": item.poi_id,
                "address": item.address,
                "latitude": item.latitude,
                "longitude": item.longitude,
                "start_time": None if item.start_time is None else _java_time(item.start_time),
                "end_time": None if item.end_time is None else _java_time(item.end_time),
                "duration_min": item.duration_min,
                "cost": item.cost,
                "tag": item.tag,
                "remark": item.remark,
                "sort_no": item.sort_no,
            }
            for item in by_day.get(day.id, [])
        ]
        plans.append({"day_no": day.day_no, "note": day.note, "items": rows})
    return plans


def _java_time(value: time) -> str:
    """LocalTime.toString()：秒与纳秒全为 0 时省略秒。"""
    return value.strftime("%H:%M") if (value.second == 0 and value.microsecond == 0) else value.strftime("%H:%M:%S")


def action_base_revision(message: ItineraryChatMessage) -> str | None:
    plans = read_json_list(message.plans_json)
    if plans and isinstance(plans[0], dict):
        revision = plans[0].get("_baseRevision")
        return None if revision is None else str(revision)
    options = read_json_list(message.hotel_options_json)
    if options and isinstance(options[0], dict):
        revision = options[0].get("baseRevision")
        return None if revision is None else str(revision)
    return None


def action_base_planning_revision(message: ItineraryChatMessage) -> int | None:
    """草稿携带的整数规划修订号（M4 CAS 基准）；存量草稿没有该键 → None（退化 hash 比对）。"""
    plans = read_json_list(message.plans_json)
    if plans and isinstance(plans[0], dict):
        value = plans[0].get("_basePlanningRevision")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    options = read_json_list(message.hotel_options_json)
    if options and isinstance(options[0], dict):
        value = options[0].get("basePlanningRevision")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def has_action_payload(message: ItineraryChatMessage) -> bool:
    return (
        message.changed == 1
        or bool(read_json_list(message.plans_json))
        or bool(read_json_list(message.hotel_options_json))
    )


def consume_pending_action(message: ItineraryChatMessage) -> None:
    message.plans_json = "[]"
    message.hotel_options_json = "[]"
    message.changed = 0


def require_pending_action(
    session,
    user_id: int,
    itinerary_id: int,
    message_id: int | None,
    base_revision: str | None,
    hotel_action: bool = False,
) -> ItineraryChatMessage:
    """取回「当前唯一可应用」的 AI 草稿并过四道 409 关卡（同 Java `requirePendingAction`）。

    由调用方传 session：应用链路是一个事务（校验 → 落库 → 消费草稿），返回的消息对象随后
    还要被 `consume_pending_action` 改写。`current_plans()` 走同一个上下文会话，所以指纹算的
    是本事务里**尚未提交**的最新形状，与 Java 在同一事务内读自己写入的语义一致。
    """
    if message_id is None or not (base_revision or "").strip():
        raise ApiError(409, "该方案缺少版本信息，请重新生成后再应用")

    message = session.execute(
        select(ItineraryChatMessage)
        .where(
            ItineraryChatMessage.id == message_id,
            ItineraryChatMessage.itinerary_id == itinerary_id,
            ItineraryChatMessage.user_id == user_id,
            ItineraryChatMessage.role == "ai",
        )
        .limit(1)
    ).scalar_one_or_none()
    payload = read_json_list(message.hotel_options_json if hotel_action else message.plans_json) if message else []
    if message is None or not payload:
        raise ApiError(409, "该方案已失效，请使用最新建议")

    candidates = (
        session.execute(
            select(ItineraryChatMessage)
            .where(
                ItineraryChatMessage.itinerary_id == itinerary_id,
                ItineraryChatMessage.user_id == user_id,
                ItineraryChatMessage.role == "ai",
            )
            .order_by(ItineraryChatMessage.id.desc())
        )
        .scalars()
        .all()
    )
    latest = next((row for row in candidates if has_action_payload(row)), None)
    if latest is None or latest.id != message.id:
        raise ApiError(409, "该方案已被更新的建议取代，请使用最新方案")

    if base_revision != action_base_revision(message) or base_revision != plan_revision(current_plans(itinerary_id)):
        consume_pending_action(message)
        raise ApiError(409, "行程已发生变化，该方案已失效，请重新生成建议")
    return message


def read_plans(message: ItineraryChatMessage) -> list[dict[str, Any]]:
    return [row for row in read_json_list(message.plans_json) if isinstance(row, dict)]


def validate_hotel_choice(message: ItineraryChatMessage, hotel_name: str, room_name: str) -> None:
    """所选酒店/房型必须真的出现在这份草稿里，不能凭空指定一家酒店来定价。"""
    for option in read_json_list(message.hotel_options_json):
        if not isinstance(option, dict) or hotel_name != str(option.get("hotelName")):
            continue
        rooms = option.get("roomTypes")
        if isinstance(rooms, list) and any(
            isinstance(room, dict) and room_name == str(room.get("roomName")) for room in rooms
        ):
            return
    raise ApiError(409, "所选酒店或房型不属于当前有效方案，请重新获取建议")


def clear_history(user_id: int, itinerary_id: int) -> None:
    """清空对话记忆：先归属校验；该表无 deleted 列，故为物理删除（同 Java）。"""
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None or main.user_id != user_id:
            raise ApiError(404, "行程不存在")
        rows = (
            session.execute(
                select(ItineraryChatMessage).where(
                    ItineraryChatMessage.itinerary_id == itinerary_id,
                    ItineraryChatMessage.user_id == user_id,
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.delete(row)


def invalidate_pending_actions(user_id: int, itinerary_id: int) -> None:
    """任何手工编辑都会让未应用的 AI 草稿失效（避免把旧方案应用到已改过的行程上）。"""
    with session_scope() as session:
        messages = (
            session.execute(
                select(ItineraryChatMessage).where(
                    ItineraryChatMessage.itinerary_id == itinerary_id,
                    ItineraryChatMessage.user_id == user_id,
                    ItineraryChatMessage.role == "ai",
                )
            )
            .scalars()
            .all()
        )
        for message in messages:
            if has_action_payload(message):
                consume_pending_action(message)


def chat_history(user_id: int, itinerary_id: int) -> list[dict[str, Any]]:
    """最近 100 条，DB 按 id 倒序取、返回前翻正（与 Java 一致）。"""
    with session_scope() as session:
        itinerary_query.require_main(session, user_id, itinerary_id)
        messages = (
            session.execute(
                select(ItineraryChatMessage)
                .where(
                    ItineraryChatMessage.itinerary_id == itinerary_id,
                    ItineraryChatMessage.user_id == user_id,
                )
                .order_by(ItineraryChatMessage.id.desc())
                .limit(100)
            )
            .scalars()
            .all()
        )
        for message in messages:
            session.expunge(message)
    return [
        {
            "id": message.id,
            "role": message.role,
            "content": message.content,
            "plans": read_json_list(message.plans_json),
            "hotelOptions": read_json_list(message.hotel_options_json),
            "changed": message.changed == 1,
            "baseRevision": action_base_revision(message),
            "createdAt": message.created_at.isoformat() if message.created_at else None,
        }
        for message in reversed(messages)
    ]


# ---------- 对话改行程（M7-a：与 /chat-edit/stream 共用同一条上下文与收尾路径） ----------


@dataclass(frozen=True)
class ChatTurnContext:
    """chatTurn 请求上下文：`chat_body` 发给 agent，`base_revision` 供草稿一致性校验。

    M4：`base_planning_revision` 是读上下文时的整数规划修订号，随草稿嵌入，
    apply 端做原子条件更新（CAS）。
    """

    chat_body: dict[str, Any]
    base_revision: str
    base_planning_revision: int | None = None


def _spent_summary(user_id: int, itinerary_id: int) -> tuple[float | None, dict[str, float] | None, list[str] | None]:
    """实际花费聚合（C3.4）：只汇总与预算同币种的账目，跨币种不换算不相加。

    无任何账目时三个值全空——chat_body 不带 spent 字段，prompt 与历史行为零漂移。
    """
    totals = expense_service.list_expenses(user_id, itinerary_id)["totals"]
    if not totals:
        return None, None, None
    domestic = [item for item in totals if item["currency"] == BUDGET_CURRENCY]
    others = sorted({item["currency"] for item in totals if item["currency"] != BUDGET_CURRENCY})
    by_category: dict[str, float] = {}
    for item in domestic:
        by_category[item["category"]] = round(by_category.get(item["category"], 0.0) + float(item["amount"]), 2)
    return (
        round(sum(float(item["amount"]) for item in domestic), 2),
        by_category or None,
        others or None,
    )


def build_chat_turn_context(
    user_id: int, itinerary_id: int, message: str, history: list[dict[str, Any]] | None
) -> ChatTurnContext:
    """构建发给 agent 的请求体——阻塞版与流式版的「同参构造」入口，两条路径输入必须一致。"""
    main = itinerary_query.find_writable_main(user_id, itinerary_id)
    persisted = chat_history(user_id, itinerary_id)
    if persisted:
        # 库里已有记忆时以它为准，并压成 {role, content} 两键；只有空历史才用客户端传的
        effective = [{"role": item.get("role", "ai"), "content": item.get("content", "")} for item in persisted]
    else:
        effective = history or []
    persisted_plans = current_plans(itinerary_id)
    base_revision = plan_revision(persisted_plans)
    # 有未应用的"调整行程天数"草稿时，后续对话继续基于草稿天数，而不是数据库旧值
    plans = latest_pending_plans(user_id, itinerary_id, base_revision) or persisted_plans
    with session_scope() as session:
        budgets = session.execute(select(BudgetDetail).where(BudgetDetail.itinerary_id == itinerary_id)).scalars().all()
        current_total = float(sum((b.amount or Decimal("0") for b in budgets), Decimal("0")))
        hotel_total = float(sum((b.amount or Decimal("0") for b in budgets if b.category == "酒店"), Decimal("0")))
    spent_total, spent_by_category, spent_other_currencies = _spent_summary(user_id, itinerary_id)
    return ChatTurnContext(
        {
            "city": main.city,
            "days": len(plans),
            "persons": 1 if main.persons is None else main.persons,
            "budget": None if main.budget is None else float(main.budget),
            "current_total": current_total,
            "current_hotel_total": hotel_total,
            "spent_total": spent_total,
            "spent_by_category": spent_by_category,
            "spent_other_currencies": spent_other_currencies,
            "start_date": None if main.start_date is None else str(main.start_date),
            "end_date": None if main.end_date is None else str(main.end_date),
            "preferences": [] if not main.preferences else main.preferences.split(","),
            "hotel_tier": main.hotel_tier,
            # M1b（spec §6.4）：主表需求快照（V13）作为编辑硬约束上下文——已确认
            # 需求优先于模型对聊天历史的再猜测；主表当前值即最新生效修订（M4 前）
            "requirements_struct": TripRequirements.model_validate(main.requirements_json)
            if main.requirements_json
            else None,
            "plans": _json_numbers(plans),
            "history": effective[-HISTORY_WINDOW:],
            "message": message or "",
        },
        base_revision,
        main.planning_revision,
    )


def _json_numbers(value: Any) -> Any:
    """把 Decimal 换成 float：Java 侧这些值经 HTTP JSON 序列化后本来就是 float，
    同进程直调若继续传 Decimal，agent 里的 `float + Decimal` 会直接 TypeError。"""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_numbers(item) for item in value]
    return value


def latest_pending_plans(user_id: int, itinerary_id: int, base_revision: str) -> list[dict[str, Any]]:
    """最近一条"仍与当前行程同版本"的 AI 草稿 plans（同 Java `latestPendingPlans`）。"""
    with session_scope() as session:
        messages = (
            session.execute(
                select(ItineraryChatMessage)
                .where(
                    ItineraryChatMessage.itinerary_id == itinerary_id,
                    ItineraryChatMessage.user_id == user_id,
                    ItineraryChatMessage.role == "ai",
                    ItineraryChatMessage.changed == 1,
                )
                .order_by(ItineraryChatMessage.id.desc())
            )
            .scalars()
            .all()
        )
        for message in messages:
            if base_revision == action_base_revision(message):
                return read_plans(message)
    return []


def attach_base_revision(
    rows: list[Any],
    base_revision: str,
    camel_case: bool,
    planning_revision: int | None = None,
) -> list[Any]:
    """给草稿逐项打上版本指纹：plans 用 `_baseRevision`（下划线=内部字段），酒店用 `baseRevision`。

    M4：同时嵌整数规划修订号（`_basePlanningRevision`/`basePlanningRevision`），
    apply 端做原子条件更新（CAS）——hash 只做可读校验身份，不代替 CAS。
    """
    key = "baseRevision" if camel_case else "_baseRevision"
    rev_key = "basePlanningRevision" if camel_case else "_basePlanningRevision"
    attached = []
    for row in rows:
        item = dict(row)
        item[key] = base_revision
        if planning_revision is not None:
            item[rev_key] = planning_revision
        attached.append(item)
    return attached


def save_chat_message(
    session,
    user_id: int,
    itinerary_id: int,
    role: str,
    content: str,
    plans: list[Any],
    hotel_options: list[Any],
    changed: bool,
) -> ItineraryChatMessage:
    message = ItineraryChatMessage(
        itinerary_id=itinerary_id,
        user_id=user_id,
        role=role,
        content=content or "",
        plans_json=json.dumps(plans or [], ensure_ascii=False),
        hotel_options_json=json.dumps(hotel_options or [], ensure_ascii=False),
        changed=1 if changed else 0,
    )
    session.add(message)
    session.flush()  # 需要 id 回填给响应的 messageId
    return message


def invalidate_in_session(session, user_id: int, itinerary_id: int) -> None:
    messages = (
        session.execute(
            select(ItineraryChatMessage).where(
                ItineraryChatMessage.itinerary_id == itinerary_id,
                ItineraryChatMessage.user_id == user_id,
                ItineraryChatMessage.role == "ai",
            )
        )
        .scalars()
        .all()
    )
    for message in messages:
        if has_action_payload(message):
            consume_pending_action(message)


def finalize_chat_turn(
    user_id: int, itinerary_id: int, message: str, ctx: ChatTurnContext, turn: dict[str, Any]
) -> dict[str, Any]:
    """把 chatTurn 结果组装成 /chat-edit 的出参并落库两条对话记忆。

    流式与非流式必须走这同一条收尾路径：不落库就没有历史、`requirePendingAction` 也找不到
    待确认动作（酒店方案的应用依赖落库消息 id）。
    """
    base_revision = ctx.base_revision
    plans = attach_base_revision(
        turn.get("plans") or [], base_revision, camel_case=False, planning_revision=ctx.base_planning_revision
    )
    hotel_options = attach_base_revision(
        turn.get("hotelOptions") or [], base_revision, camel_case=True, planning_revision=ctx.base_planning_revision
    )
    out: dict[str, Any] = {
        "reply": turn.get("reply", "已更新草稿"),
        "changed": bool(turn.get("changed", False)),
        "plans": plans,
        "hotelOptions": hotel_options,
        "baseRevision": base_revision,
        "requiresConfirmation": bool(turn.get("requiresConfirmation", False)),
        "planDocument": turn.get("planDocument"),
        "operations": turn.get("operations") or [],
        "pendingAction": turn.get("pendingAction"),
    }
    with session_scope() as session:
        save_chat_message(session, user_id, itinerary_id, "user", message, [], [], False)
        if out["changed"] or hotel_options:
            invalidate_in_session(session, user_id, itinerary_id)
        ai_message = save_chat_message(
            session, user_id, itinerary_id, "ai", str(out["reply"]), plans, hotel_options, out["changed"]
        )
        out["messageId"] = ai_message.id
    return out


# ---------- turn 级幂等（M6，spec §12 编辑入口） ----------

#: 键命名空间：完整键 = `py:chat-turn:{user_id}:{itinerary_id}:{turn_id}`（Redis/内存
#: 双后端沿用 cache_store 既有设施，**不新建表**；TTL 1 小时后自动可重用）。
TURN_IDEM_NAMESPACE = "chat-turn"
TURN_TTL_SECONDS = 3600
#: 失败记录只保留短窗口：status=error 不是终态闸（重试必须放行），留 60s 仅为
#: 让「仍在等待上一份」的并发请求读到明确的失败原因，而不是撞上已删除的键。
TURN_ERROR_TTL_SECONDS = 60
#: turnId 由前端生成 UUID；只做长度上限防御（与 generate 的 idempotency key 同口径），
#: 不强制 UUID 格式——键值本来就只在本服务内自比。
MAX_TURN_ID_LEN = 64

#: done 重放的固定空变更响应文案：诚实告知"刚处理过"，不伪造一轮新回复。
TURN_REPLAY_REPLY = "这条刚刚已经处理过了，行程未变化"


def normalize_turn_id(raw: object) -> str | None:
    """X-Turn-Id 头的规范化：空白视为未携带，超长截断防键滥用。

    参数类型放宽到 object：路由协程被测试直调时 x_turn_id 缺省是未解析的
    Header 对象而非 None，非 str 一律按未携带处理。
    """
    if not isinstance(raw, str):
        return None
    turn_id = raw.strip()
    if not turn_id:
        return None
    return turn_id[:MAX_TURN_ID_LEN]


@dataclass(frozen=True)
class TurnClaim:
    """一次 turn 争抢的结果：`replay` 非 None 表示撞上了 done 记录，直接重放。"""

    turn_id: str
    request_hash: str
    replay: dict[str, Any] | None = None


def turn_request_hash(message: str, ctx: ChatTurnContext) -> str:
    """turn 幂等的请求指纹：SHA-256(message + plans 指纹 + requirements 指纹)。

    plans 指纹复用 `base_revision`（current_plans 的 SHA-256，Java 同式）——
    chat_body 里的 plans 已被 `_json_numbers` 转成 float，而 canonical_json 有意
    拒绝 float（Java BigDecimal 跨语言契约），不能直接对它做指纹；草稿天数的差异
    用 `days` 补钉。requirements 对 model_dump(mode="json") 做 canonical_json。
    """
    requirements = ctx.chat_body.get("requirements_struct")
    payload = {
        "message": message or "",
        "plansRevision": ctx.base_revision,
        "days": ctx.chat_body.get("days"),
        "requirements": (canonical_json(requirements.model_dump(mode="json")) if requirements is not None else None),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _turn_key(user_id: int, itinerary_id: int, turn_id: str) -> str:
    return f"{user_id}:{itinerary_id}:{turn_id}"


def _replay_response(ctx: ChatTurnContext) -> dict[str, Any]:
    """done 记录的重放响应：与 finalize 出参同形状的**空变更**回合。

    不新增消息/草稿/版本，也绝不再调模型——键形状对齐让前端按普通回合消费。
    """
    return {
        "reply": TURN_REPLAY_REPLY,
        "changed": False,
        "plans": [],
        "hotelOptions": [],
        "baseRevision": ctx.base_revision,
        "requiresConfirmation": False,
        "planDocument": None,
        "operations": [],
        "pendingAction": None,
        "messageId": None,
    }


def claim_chat_turn(user_id: int, itinerary_id: int, message: str, ctx: ChatTurnContext, turn_id: str) -> TurnClaim:
    """进入模型调用**前**原子争抢 turn 记录（cache_store.reserve = SET NX EX）。

    - 占位成功 → 获得执行权（记录 status=running）；
    - 已存在且 request_hash 不同 → 409（同一 turnId 已用于不同请求）；
    - running → 409（同一指令仍在处理中——网络超时≠后台取消，重试引导等待，
      绝不执行第二份）；
    - done → 重放空变更响应（不调模型不落库）；
    - error → 上一次相同请求已失败：释放记录后重新争抢（重试必须放行）。
    """
    request_hash = turn_request_hash(message, ctx)
    key = _turn_key(user_id, itinerary_id, turn_id)
    for _ in range(2):  # error 释放后重争抢一次；两轮都失败按 running 拒（理论不可达）
        record = {"status": "running", "request_hash": request_hash}
        if cache_store.reserve(TURN_IDEM_NAMESPACE, key, record, TURN_TTL_SECONDS):
            return TurnClaim(turn_id=turn_id, request_hash=request_hash)
        existing = cache_store.get_json(TURN_IDEM_NAMESPACE, key)
        if not isinstance(existing, dict) or existing.get("request_hash") != request_hash:
            raise ApiError(409, "同一 turnId 已用于不同请求，请更换后重试")
        status = existing.get("status")
        if status == "running":
            raise ApiError(409, "同一指令仍在处理中，请等待完成或稍后查询")
        if status == "done":
            return TurnClaim(turn_id=turn_id, request_hash=request_hash, replay=_replay_response(ctx))
        cache_store.delete(TURN_IDEM_NAMESPACE, key)
    raise ApiError(409, "同一指令仍在处理中，请等待完成或稍后查询")


def settle_chat_turn(
    user_id: int,
    itinerary_id: int,
    claim: TurnClaim | None,
    *,
    ok: bool,
    result: dict[str, Any] | None = None,
) -> None:
    """回写 turn 记录终态：成功 done（附结果摘要），失败 error（短窗，放行重试）。

    claim=None（未启用 turn 幂等）是no-op。网络断开不回写——流式断开时工作线程
    仍会跑完并落到这里，running 记录存在期间的重试一律 409，不执行第二份。
    """
    if claim is None:
        return
    key = _turn_key(user_id, itinerary_id, claim.turn_id)
    if not ok:
        cache_store.set_json(
            TURN_IDEM_NAMESPACE,
            key,
            {"status": "error", "request_hash": claim.request_hash},
            TURN_ERROR_TTL_SECONDS,
        )
        return
    summary_result = result or {}
    cache_store.set_json(
        TURN_IDEM_NAMESPACE,
        key,
        {
            "status": "done",
            "request_hash": claim.request_hash,
            "result": {
                "reply": str(summary_result.get("reply", ""))[:200],
                "changed": bool(summary_result.get("changed", False)),
                "messageId": summary_result.get("messageId"),
            },
        },
        TURN_TTL_SECONDS,
    )


def prepare_chat_turn(
    user_id: int, itinerary_id: int, message: str, history: list[dict[str, Any]] | None, turn_id: str | None = None
) -> tuple[ChatTurnContext, TurnClaim | None]:
    """构建上下文 + turn 争抢（阻塞路径直接用；流式路由在**建流之前**调用，
    让 409 以真实 HTTP 状态返回，而不是流中间的 error 事件）。"""
    ctx = build_chat_turn_context(user_id, itinerary_id, message, history)
    if not turn_id:
        return ctx, None
    return ctx, claim_chat_turn(user_id, itinerary_id, message, ctx, turn_id)


def chat_edit(
    user_id: int, itinerary_id: int, message: str, history: list[dict[str, Any]] | None, turn_id: str | None = None
) -> dict[str, Any]:
    """阻塞版对话改行程（同 Java `chatEdit`）。

    M6（spec §12）：带 `turn_id`（X-Turn-Id 头）时进入模型调用前原子争抢 turn 记录——
    running 期间的重试 409、done 重放空变更响应（不再调模型/不再落库）、同 turnId 不同
    请求 409；无 turn_id 完全走旧行为。
    """
    ctx, claim = prepare_chat_turn(user_id, itinerary_id, message, history, turn_id)
    if claim is not None and claim.replay is not None:
        return claim.replay
    # BYOK 路由：请求线程内直接跑 agent，route_scope 在此进入（与 use_scene 同位）。
    # 落库收尾也在保障窗内：finalize 失败 = 本轮无任何持久化结果，error 记录放行重试。
    try:
        with llm_gateway_service.route_scope(user_id):
            turn = run_chat_turn_in_process(ctx, itinerary_id)
        out = finalize_chat_turn(user_id, itinerary_id, message, ctx, turn)
    except BaseException:
        settle_chat_turn(user_id, itinerary_id, claim, ok=False)
        raise
    settle_chat_turn(user_id, itinerary_id, claim, ok=True, result=out)
    return out


def run_chat_turn_in_process(ctx: ChatTurnContext, itinerary_id: int) -> dict[str, Any]:
    """同进程调 agent，但保住 HTTP 时代的两件事：场景记账与 502 文案。

    `use_scene("chat")` 决定 token 落进哪个场景、`observe_run` 决定这次调用有没有 trace 与
    预算——两者都挂在 ContextVar 上，只能在真正执行调用的那个线程/上下文里进。
    """
    request = ctx.chat_body
    with use_scene("chat"), observe_run(request_id=f"itinerary-{itinerary_id}"):
        response = itinerary_city.guard_agent_call(
            "行程助手暂不可用",
            lambda: run_chat_turn(
                ChatTurnRequest.model_validate(request),
                confirmation_thread=confirm_thread(itinerary_id),
            ),
        )
    return response.model_dump(by_alias=True)


# Java 与 PDF 导出共用 taskExecutor(core4/max8/queue50)。这里拆开：一排队渲染的 PDF 不该把
# 对话流的错误率抬起来（对话池满的语义是 AGENT_BUSY，导出池满的语义是就地渲染）。
CHAT_SLOTS = 8 + 50
chat_pool = SlotExecutor("travel-chat", 8, CHAT_SLOTS)
# 打字机节奏：与 Java `CHAT_TOKEN_CHUNK_SIZE` 同值。**仍是假流式**——
# `run_chat_turn` 是多步编排（意图→改计划/酒店→校验→措辞），reply 在最后一步才成形，
# 真·逐 token 需要 agent 侧把措辞那一步换成流式回调，属于产品切片而非迁移能顺手带上的改动。
CHAT_TOKEN_CHUNK_SIZE = 40
BUSY_MESSAGE = "行程助手繁忙，请稍后重试"


def chunk_reply(text: str, size: int = CHAT_TOKEN_CHUNK_SIZE) -> list[str]:
    """reply 按 size 字符切片（最后一片可能更短）；空文本不产帧（同 Java `chunk`）。"""
    if not text or size <= 0:
        return []
    return [text[i : i + size] for i in range(0, len(text), size)]


async def chat_edit_stream(
    user_id: int,
    itinerary_id: int,
    message: str,
    history: list[dict[str, Any]] | None,
    prepared: tuple[ChatTurnContext, TurnClaim | None] | None = None,
) -> AsyncIterator[str]:
    """流式版对话改行程：产出**信封 JSON 字符串**，由路由层包成 SSE 帧。

    与阻塞版共用 `build_chat_turn_context` + `finalize_chat_turn`，因此两条路径发给 agent 的
    输入与落库的记忆完全一致；`chat_draft` 携带 /chat-edit 出参的全部字段（`reply` 除外，
    它已按 `chat_token` 送达），前端可复用同一套草稿与酒店卡片逻辑。
    归属校验必须在**首帧之前**由调用方做完（HTTP 404 不能变成流中间的 error 事件）。
    M6：带 turn_id 时由路由先跑 `prepare_chat_turn`（409 在建流前抛出），把结果经
    `prepared` 传入；done 重放按空变更回合发帧（不调模型、不落库、不占池）。

    异步生成器 + asyncio.Queue：客户端断开时这条流会被取消，而不是把线程池工作线程
    按在 `queue.get()` 上直到模型跑完。
    """
    if prepared is None:
        # 上下文构建是一串索引查询：放在线程里跑，别占着事件循环
        try:
            ctx = await run_in_threadpool(build_chat_turn_context, user_id, itinerary_id, message, history)
        except Exception as exc:
            logger.warning("chat stream context failed for itinerary %s: %s", itinerary_id, exc)
            # AICHAIN-1：错误帧文案走 R2-F3 判据（ApiError 保留用户文案，其余中性引导），不裸透 str(exc)
            yield event_publisher.error_envelope(itinerary_id, "AGENT_ERROR", user_reason(exc))
            return
        claim = None
    else:
        ctx, claim = prepared
        if claim is not None and claim.replay is not None:
            # done 重放：固定空变更回合的帧形；与阻塞重放同源（_replay_response），
            # 不再调模型、不再落库（"不新增消息/草稿/版本"）
            for delta in chunk_reply(claim.replay["reply"]):
                yield event_publisher.publish_local(
                    itinerary_id, "chat_token", {"messageId": claim.turn_id[:8], "delta": delta}
                )
            draft = {key: value for key, value in claim.replay.items() if key != "reply"}
            yield event_publisher.publish_local(itinerary_id, "chat_draft", draft)
            yield event_publisher.publish_local(itinerary_id, "chat_done", {"messageId": claim.turn_id[:8]})
            return

    # 短 id 只用于前端归并同一条回复的 token 流，与落库的 messageId 无关
    loop = asyncio.get_running_loop()
    stream_id = uuid.uuid4().hex[:8]
    outcomes: asyncio.Queue = asyncio.Queue(maxsize=1)
    try:
        chat_pool.submit(_stream_turn, outcomes, loop, user_id, itinerary_id, message, ctx, claim)
    except TaskRejected as exc:
        # 池满背压：与生成任务的 429 同语义，但 SSE 下只能转成 error 事件
        logger.warning("chat pool saturated for itinerary %s: %s", itinerary_id, exc)
        yield event_publisher.error_envelope(itinerary_id, "AGENT_BUSY", BUSY_MESSAGE)
        return

    while True:
        try:
            out, error = await asyncio.wait_for(outcomes.get(), event_hub.HEARTBEAT_SECONDS)
            break
        except TimeoutError:
            # 模型跑得久时靠心跳帧保持连接不被代理层掐掉（Java 的本地 emitter 不注册进
            # 网关心跳表，所以那段等待窗口是完全静默的）
            yield event_publisher.heartbeat_envelope(itinerary_id)

    if error is not None:
        yield event_publisher.error_envelope(itinerary_id, "AGENT_ERROR", error)
        return
    for delta in chunk_reply(str(out["reply"])):
        yield event_publisher.publish_local(itinerary_id, "chat_token", {"messageId": stream_id, "delta": delta})
    draft = dict(out)
    draft.pop("reply", None)
    yield event_publisher.publish_local(itinerary_id, "chat_draft", draft)
    yield event_publisher.publish_local(itinerary_id, "chat_done", {"messageId": stream_id})


def _stream_turn(
    outcomes: asyncio.Queue,
    loop: asyncio.AbstractEventLoop,
    user_id: int,
    itinerary_id: int,
    message: str,
    ctx: ChatTurnContext,
    claim: TurnClaim | None = None,
) -> None:
    """工作线程主体：跑完一轮并落库，结果（或错误文本）投回事件循环。

    始终经 `call_soon_threadsafe` 非阻塞投递：客户端断开后循环可能已经关掉，
    阻塞 put 会把池线程永久 park 住。客户端断开**不会取消**本任务：跑完照常落库
    并回写 turn 记录（M6：网络超时≠后台取消，重试在 running 记录存在期间一律 409）。
    """
    try:
        # BYOK 路由：chat_pool 工作线程不继承请求线程的 contextvars，user_id 是
        # 显式参数——路由上下文必须在这里进入（研究/生成同理见 plan_days）。
        with llm_gateway_service.route_scope(user_id):
            out = finalize_chat_turn(user_id, itinerary_id, message, ctx, run_chat_turn_in_process(ctx, itinerary_id))
        settle_chat_turn(user_id, itinerary_id, claim, ok=True, result=out)
        _offer(outcomes, loop, (out, None))
    except Exception as exc:
        settle_chat_turn(user_id, itinerary_id, claim, ok=False)
        logger.warning("chat stream turn failed for itinerary %s: %s", itinerary_id, exc, exc_info=True)
        # AICHAIN-1：同上——错误帧共用 user_reason 判据，原始异常文本只进日志
        _offer(outcomes, loop, (None, user_reason(exc)))


def _offer(outcomes: asyncio.Queue, loop: asyncio.AbstractEventLoop, outcome) -> None:
    def deliver() -> None:
        try:
            outcomes.put_nowait(outcome)
        except asyncio.QueueFull:
            logger.warning("chat stream result dropped: consumer gone")

    try:
        loop.call_soon_threadsafe(deliver)
    except RuntimeError:
        logger.warning("chat stream result dropped: event loop closed")
