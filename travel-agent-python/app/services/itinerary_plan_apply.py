"""草稿应用与酒店选项（移植自 Java `ItineraryPlanApplyService`）。

三条不可松手的语义：

1. **只认服务端草稿**：Java 的门面 `ItineraryServiceImpl:131-134` 明确**丢弃**客户端 body 里的
   `plans`，真正落库的内容来自 `actionMessageId` 指向的那条 AI 消息，并用 `baseRevision` 指纹
   做乐观并发（四道 409 见 `itinerary_chat.require_pending_action`）。迁移时若"顺手"改成信任
   `plans`，等于把「AI 建议 → 用户确认」这条审计链关掉：客户端可以借 apply 接口写任意点位。
2. 应用是**整份替换**：草稿里没出现的行程项软删、天数变少则尾部日期软删；日元数据（主题/
   迷你路线/备选/机位/实用提示）按草稿整份覆盖，草稿没带就清空，不给上一版留残影。
3. 项上的 `id` 是**身份校验**：带 id 表示「改这一条」，名称与库里不一致即整单 400、什么都不写。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.agent import confirm_thread, resume_confirmation
from app.common.envelope import ApiError
from app.db.models import ItineraryDay, ItineraryItem
from app.db.session import session_scope
from app.schemas.business.itinerary import HotelOptionRequest
from app.schemas.trip import MAX_TRIP_DAYS
from app.services import budget_engine, itinerary_chat, itinerary_query, itinerary_version, season_price

logger = logging.getLogger(__name__)

VALID_ITEM_TYPES = ("attraction", "food", "hotel", "transport")
MAX_ITEMS_PER_DAY = 20
MAX_POI_NAME = 128
DEFAULT_NEW_DAY_NOTE = "宽松安排"
HOTEL_START_TIME = time(20, 0)


# ---------- 应用 AI 草稿 ----------


def apply_plans(
    user_id: int, itinerary_id: int, action_message_id: int | None, base_revision: str | None
) -> dict[str, Any]:
    with session_scope() as session:
        main = itinerary_query.require_writable_main(session, user_id, itinerary_id)
        message = itinerary_chat.require_pending_action(
            session, user_id, itinerary_id, action_message_id, base_revision, False
        )
        # PR-6 确认流薄适配：有待确认提案时以 Command(resume) 续跑，服务端复核
        # 「确认的确实是要应用的那份草稿」；无待确认提案（超时清理 / 存量消息）
        # 回退既有 message 级语义，不新增硬失败。
        verdict = resume_confirmation(confirm_thread(itinerary_id), {"action": "apply_plans"})
        if verdict is not None and not verdict.confirmed:
            raise ApiError(409, f"确认与提案不一致：{verdict.reason}")
        plans = itinerary_chat.read_plans(message)
        days = list(
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        _validate_plans(plans)
        existing_items = {
            item.id: item
            for item in session.execute(select(ItineraryItem).where(ItineraryItem.itinerary_id == itinerary_id))
            .scalars()
            .all()
        }
        retained_item_ids: set[int] = set()
        day_by_no = {day.day_no: day for day in days}
        itinerary_version.create_snapshot(user_id, itinerary_id, "apply_plans", "应用草稿前快照")

        for day_no in range(1, len(plans) + 1):
            if day_no in day_by_no:
                continue
            created = ItineraryDay(
                itinerary_id=itinerary_id,
                day_no=day_no,
                city=main.city,
                travel_date=None if main.start_date is None else main.start_date + timedelta(days=day_no - 1),
                note=DEFAULT_NEW_DAY_NOTE,
            )
            session.add(created)
            session.flush()
            day_by_no[day_no] = created

        for plan in plans:
            day = day_by_no.get(_as_int(plan.get("day_no")) or 0)
            if day is None:
                continue
            note = plan.get("note")
            if isinstance(note, str) and note.strip():
                day.note = note
            _persist_day_metadata(day, plan)
            sort_no = 0
            for raw_item in plan.get("items") or []:
                if not isinstance(raw_item, dict):
                    continue
                name = str(raw_item.get("poi_name"))
                if not name.strip() or name == "null":
                    continue
                sort_no = _apply_plan_item(
                    session, raw_item, name, itinerary_id, day.id, existing_items, retained_item_ids, sort_no
                )

        for item_id, item in existing_items.items():
            if item_id not in retained_item_ids:
                item.deleted = 1  # 同 @TableLogic 的 deleteById
        for day in days:
            if day.day_no > len(plans):
                day.deleted = 1

        if main.days != len(plans):
            main.days = len(plans)
            main.end_date = None if main.start_date is None else main.start_date + timedelta(days=len(plans) - 1)
            main.title = f"{main.city}{len(plans)}日游"

        budget_engine.recalculate(itinerary_id)
        itinerary_chat.consume_pending_action(message)
        itinerary_version.record_snapshot_or_log(user_id, itinerary_id, "apply_plans", "应用草稿完成")

    itinerary_query.evict_detail(user_id, itinerary_id)
    return itinerary_query.detail(user_id, itinerary_id)


def _apply_plan_item(
    session,
    raw_item: dict[str, Any],
    name: str,
    itinerary_id: int,
    day_id: int,
    existing_items: dict[int, ItineraryItem],
    retained: set[int],
    sort_no: int,
) -> int:
    existing: ItineraryItem | None = None
    item_id = raw_item.get("id")
    if _is_number(item_id):
        existing = existing_items.get(int(item_id))
        if existing is None or name != existing.poi_name:
            raise ApiError(400, "行程项身份校验失败，未应用任何修改")

    # 语料库退役：候选事实（坐标/地址/估价）以生成期富化后的草稿字段为准，
    # 不再回查 poi_knowledge。poi_id 存外部地点 ID（OTM xid 等）。
    item_type = _str_or(raw_item.get("item_type"), "attraction")

    # existing 与 entity 是同一个对象：Java 里紧随其后的 `setCost(existing.getCost())`
    # 是自赋值空操作（成本已被候选 POI 覆盖），这里同样不做保留，别"顺手修好"。
    entity = existing if existing is not None else ItineraryItem()
    entity.day_id = day_id
    entity.itinerary_id = itinerary_id
    entity.item_type = item_type
    entity.poi_name = name
    entity.poi_id = _str_or(raw_item.get("poi_id"), None) or _str_or(raw_item.get("id"), None)
    entity.address = _str_or(raw_item.get("address"), entity.address)
    if _is_number(raw_item.get("latitude")):
        entity.latitude = Decimal(str(raw_item["latitude"]))
    if _is_number(raw_item.get("longitude")):
        entity.longitude = Decimal(str(raw_item["longitude"]))
    if _is_number(raw_item.get("cost")):
        entity.cost = Decimal(str(raw_item["cost"]))

    entity.start_time = _parse_time(raw_item.get("start_time"))
    entity.end_time = _parse_time(raw_item.get("end_time"))
    if _is_number(raw_item.get("duration_min")):
        entity.duration_min = int(raw_item["duration_min"])
    entity.tag = _str_or(raw_item.get("tag"), entity.tag)
    entity.remark = _str_or(raw_item.get("remark"), entity.remark)
    # 草稿自带的证据字段优先；没带就沿用候选 POI 的权威来源，
    # 不把一次用户编辑降级成「无来源事实」。
    entity.open_time = _str_or(raw_item.get("open_time"), entity.open_time)
    entity.source = _str_or(raw_item.get("source"), entity.source)
    entity.verification_status = _str_or(raw_item.get("verification_status"), entity.verification_status)
    entity.value_kind = _str_or(raw_item.get("value_kind"), entity.value_kind)
    entity.freshness_status = _str_or(raw_item.get("freshness_status"), entity.freshness_status)
    entity.review_requirement = _str_or(raw_item.get("review_requirement"), entity.review_requirement)
    source_updated_at = raw_item.get("source_updated_at")
    if isinstance(source_updated_at, str) and source_updated_at.strip():
        entity.source_updated_at = _parse_datetime(source_updated_at)
    fact_evidence = raw_item.get("fact_evidence_json")
    if isinstance(fact_evidence, str) and fact_evidence.strip():
        entity.fact_evidence_json = fact_evidence

    entity.sort_no = sort_no
    sort_no += 1
    if existing is None:
        session.add(entity)
    else:
        retained.add(existing.id)
    return sort_no


# ---------- 应用酒店房型 ----------


def apply_hotel_option(user_id: int, itinerary_id: int, request: HotelOptionRequest | None) -> dict[str, Any]:
    request = request or HotelOptionRequest()
    _validate_hotel_request(request)
    with session_scope() as session:
        main = itinerary_query.require_writable_main(session, user_id, itinerary_id)
        message = itinerary_chat.require_pending_action(
            session, user_id, itinerary_id, request.actionMessageId, request.baseRevision, True
        )
        hotel_name = request.hotelName or ""
        # PR-6 确认流薄适配：有待确认提案时以 Command(resume) 续跑，服务端复核
        # 「确认的酒店确实出自当时提案的候选卡片」；无待确认提案（超时清理 / 存量
        # 消息）回退既有 message 级语义，不新增硬失败。
        verdict = resume_confirmation(
            confirm_thread(itinerary_id), {"action": "replace_hotel", "hotel_name": request.hotelName or ""}
        )
        if verdict is not None and not verdict.confirmed:
            raise ApiError(409, f"确认与提案不一致：{verdict.reason}")
        if not hotel_name.strip() or len(hotel_name) > MAX_POI_NAME:
            raise ApiError(400, "酒店名称不合法")
        # 语料库退役：酒店事实与房价一律以**待确认消息里的候选卡片**为准
        # （卡片由 chat_draft 生成时写入 hotel_options_json），不回查 poi_knowledge。
        option = next(
            (
                option
                for option in itinerary_chat.read_json_list(message.hotel_options_json)
                if isinstance(option, dict) and str(option.get("hotelName")) == hotel_name
            ),
            None,
        )
        if option is None:
            raise ApiError(404, "未找到该城市的酒店候选")
        itinerary_chat.validate_hotel_choice(message, hotel_name, request.roomType or "")

        itinerary_version.create_snapshot(user_id, itinerary_id, "apply_hotel", "应用酒店方案前快照")
        hotel_items = list(
            session.execute(
                select(ItineraryItem).where(
                    ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.item_type == "hotel"
                )
            )
            .scalars()
            .all()
        )
        days = list(
            session.execute(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id)).scalars().all()
        )
        day_by_id = {day.id: day for day in days}
        day_by_no = {day.day_no: day for day in days}
        existing_hotel_day_nos = {
            day.day_no for day in (day_by_id.get(item.day_id) for item in hotel_items) if day is not None
        }
        # LinkedHashSet 语义：去重且保持用户选择顺序
        selected_day_nos = list(dict.fromkeys(request.dayNos or []))
        if not selected_day_nos or not day_by_no.keys() >= set(selected_day_nos):
            raise ApiError(400, "选择的入住晚次不在当前行程中")

        # 房型价从候选卡片解析：card.roomTypes[].basePrice —— 卡片即报价单，
        # 不存在"回退知识库价"的路径（校验已保证所选项必须出现在卡片里）。
        rooms = [room for room in option.get("roomTypes") or [] if isinstance(room, dict)]
        room = next(
            (room for room in rooms if str(room.get("roomName")) == request.roomType),
            None,
        )
        if room is None:
            raise ApiError(400, "该酒店不存在所选房型")
        room_base_price = _to_decimal(room.get("basePrice"))
        room_description = _str_or(room.get("description"), None)
        if room_base_price is None or room_base_price <= 0:
            raise ApiError(400, "所选房型暂无有效参考价")

        for day_no in selected_day_nos:
            day = day_by_no[day_no]
            item = next((existing for existing in hotel_items if existing.day_id == day.id), None)
            is_new = item is None
            if is_new:
                item = ItineraryItem(
                    itinerary_id=itinerary_id,
                    day_id=day.id,
                    item_type="hotel",
                    start_time=HOTEL_START_TIME,
                    sort_no=itinerary_query.next_sort(session, day.id),
                )
                session.add(item)
            stay_date = day.travel_date or main.start_date
            item.poi_id = _str_or(option.get("id"), hotel_name)
            item.poi_name = hotel_name
            item.address = _str_or(option.get("address"), None)
            item.cost = season_price.apply(room_base_price, stay_date)
            item.remark = _hotel_price_remark(request.roomType, room_base_price, stay_date, room_description)

        if set(selected_day_nos) >= existing_hotel_day_nos and request.tier and request.tier.strip():
            main.hotel_tier = request.tier

        budget_engine.recalculate(itinerary_id)
        itinerary_chat.consume_pending_action(message)
        itinerary_version.record_snapshot_or_log(user_id, itinerary_id, "apply_hotel", "应用酒店方案完成")

    itinerary_query.evict_detail(user_id, itinerary_id)
    return itinerary_query.detail(user_id, itinerary_id)


def _hotel_price_remark(
    room_type: str, base_price: Decimal, stay_date: date | None, room_description: str | None
) -> str:
    remark = (
        f"房型：{room_type}；基准价￥{base_price}；按{season_price.label(stay_date)}"
        f"系数×{season_price.factor(stay_date)}"
    )
    if room_description and room_description.strip():
        remark += f"；{room_description}"
    return remark


# ---------- 校验与元数据 ----------


def _validate_hotel_request(request: HotelOptionRequest) -> None:
    """对应 Java DTO 上的 Bean Validation（在进服务之前执行）。"""
    if not (request.hotelName or "").strip():
        raise ApiError(400, "酒店名称不能为空")
    if not (request.roomType or "").strip():
        raise ApiError(400, "请选择房型")
    if not (request.dayNos or []):
        raise ApiError(400, "请选择具体入住晚次")


def _validate_plans(plans: list[dict[str, Any]]) -> None:
    if not plans or len(plans) > MAX_TRIP_DAYS:
        raise ApiError(400, "行程草稿必须包含 1 到 7 个完整日期，未应用任何修改")
    valid_days = set(range(1, len(plans) + 1))
    seen_days: set[int] = set()
    for plan in plans:
        if not isinstance(plan, dict) or not _is_number(plan.get("day_no")):
            raise ApiError(400, "行程草稿日期格式错误，未应用任何修改")
        day_no = int(plan["day_no"])
        if day_no not in valid_days or day_no in seen_days:
            raise ApiError(400, "行程草稿日期重复或越界，未应用任何修改")
        seen_days.add(day_no)
        items = plan.get("items")
        if not isinstance(items, list) or len(items) > MAX_ITEMS_PER_DAY:
            raise ApiError(400, "单日行程项数量或格式不合法，未应用任何修改")
        for raw_item in items:
            if not isinstance(raw_item, dict):
                raise ApiError(400, "行程项格式错误，未应用任何修改")
            name = str(raw_item.get("poi_name"))
            item_type = str(raw_item.get("item_type"))
            if not name.strip() or name == "null" or len(name) > MAX_POI_NAME or item_type not in VALID_ITEM_TYPES:
                raise ApiError(400, "行程项名称或类型不合法，未应用任何修改")
            _validate_number_range(raw_item.get("cost"), 0, 1_000_000, "行程项费用不合法，未应用任何修改")
            _validate_number_range(raw_item.get("duration_min"), 0, 1440, "行程项时长不合法，未应用任何修改")
            _validate_optional_time(raw_item.get("start_time"))
            _validate_optional_time(raw_item.get("end_time"))


def _validate_number_range(value: Any, low: float, high: float, message: str) -> None:
    if value is None:
        return
    if not _is_number(value) or not (low <= float(value) <= high):
        raise ApiError(400, message)


def _validate_optional_time(value: Any) -> None:
    if value is None or not str(value).strip() or value == "null":
        return
    if _parse_time(value) is None:
        raise ApiError(400, "时间格式不合法，未应用任何修改")


_METADATA_KEYS = {
    "theme": "theme",
    "mini_route": "miniRoute",
    "backup_plan": "backupPlan",
    "photo_spots": "photoSpots",
    "practical_notes": "practicalNotes",
}


def _persist_day_metadata(day: ItineraryDay, plan: dict[str, Any]) -> None:
    """草稿是该日期的完整替代读模型：未携带的元数据要清掉，不留上一版本。"""
    metadata: dict[str, Any] = {}
    for source_key, output_key in _METADATA_KEYS.items():
        value = plan.get(source_key)
        if value is None:
            value = plan.get(output_key)  # 历史草稿有 camelCase 变体
        if value is not None:
            metadata[output_key] = value
    try:
        day.metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
    except (TypeError, ValueError) as exc:
        logger.warning("day metadata serialization failed, dropped metadata json instead: %s", exc)
        day.metadata_json = None


def _is_number(value: Any) -> bool:
    """Java 的 `instanceof Number` 不收布尔与字符串数字，这里同口径。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_int(value: Any) -> int | None:
    return int(value) if _is_number(value) else None


def _to_decimal(value: Any) -> Decimal | None:
    """候选卡片里的价格 → 两位小数 Decimal；缺失/非法返回 None（按无参考价拒绝）。"""
    if _is_number(value) and float(value) > 0:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    return None


def _str_or(value: Any, fallback: str | None) -> str | None:
    if value is None:
        return fallback
    text = str(value)
    return fallback if not text.strip() or text == "null" else text


def _parse_time(value: Any) -> time | None:
    """`LocalTime.parse` 语义：解析失败按 null 处理（校验阶段已挡过一轮）。

    必须是带冒号的 ISO 时钟时间：`time.fromisoformat` 在 3.11+ 还收 `0930` 这种
    基本格式，`strptime("%H:%M")` 又收 `9:30`，两者都比 Java 宽松。
    """
    if value is None:
        return None
    text = str(value).strip()
    if ":" not in text:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError:
        return None


def _parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        logger.debug("datetime value unparsable, left empty instead: %s", exc)
        return None
