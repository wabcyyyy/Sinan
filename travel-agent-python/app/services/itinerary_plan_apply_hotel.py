"""酒店选项落地：把待确认消息里的候选酒店卡片写到选中晚次（自 itinerary_plan_apply 拆出）。

前置校验（Bean Validation 语义）与 session/待确认消息的获取仍在 `itinerary_plan_apply`
的 `apply_hotel_option`，本模块接收已取好的 main/message 在活动 session 内落地。
依赖方向：本模块 → `itinerary_plan_apply_items`（共享标量 helper），不反向。
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.agent import confirm_thread, resume_confirmation
from app.common.envelope import ApiError
from app.db.models import ItineraryChatMessage, ItineraryDay, ItineraryItem, ItineraryMain
from app.schemas.business.itinerary import HotelOptionRequest
from app.services import budget_engine, itinerary_chat, itinerary_query, itinerary_version, season_price
from app.services.itinerary_plan_apply_items import MAX_POI_NAME, _str_or, _to_decimal

HOTEL_START_TIME = time(20, 0)


def apply_hotel_option(
    session, main: ItineraryMain, message: ItineraryChatMessage, request: HotelOptionRequest
) -> None:
    itinerary_id = main.id
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
    option = _selected_hotel_option(message, hotel_name)
    if option is None:
        raise ApiError(404, "未找到该城市的酒店候选")
    itinerary_chat.validate_hotel_choice(message, hotel_name, request.roomType or "")

    base_planning_revision = itinerary_chat.action_base_planning_revision(message)
    itinerary_version.append_version_in_session(
        session, main.user_id, itinerary_id, "apply_hotel", "应用酒店方案前快照"
    )
    hotel_items, day_by_no, existing_hotel_day_nos, selected_day_nos = _load_hotel_selection(
        session, itinerary_id, request
    )
    if not selected_day_nos or not day_by_no.keys() >= set(selected_day_nos):
        raise ApiError(400, "选择的入住晚次不在当前行程中")

    # 房型价从候选卡片解析：card.roomTypes[].basePrice —— 卡片即报价单，
    # 不存在"回退知识库价"的路径（校验已保证所选项必须出现在卡片里）。
    room = _selected_room(option, request.roomType)
    room_base_price = _to_decimal(room.get("basePrice"))
    room_description = _str_or(room.get("description"), None)
    if room_base_price is None or room_base_price <= 0:
        raise ApiError(400, "所选房型暂无有效参考价")

    _upsert_hotel_items(
        session,
        main,
        option,
        hotel_name,
        request.roomType or "",
        room_base_price,
        room_description,
        selected_day_nos,
        day_by_no,
        hotel_items,
    )

    if set(selected_day_nos) >= existing_hotel_day_nos and request.tier and request.tier.strip():
        main.hotel_tier = request.tier

    budget_engine.recalculate(itinerary_id)
    itinerary_chat.consume_pending_action(message)
    itinerary_version.append_version_in_session(session, main.user_id, itinerary_id, "apply_hotel", "应用酒店方案完成")
    # M4 CAS：并发推进 → 409 整事务回滚
    itinerary_version.bump_planning_revision_with_cas(session, itinerary_id, base_planning_revision)


def _load_hotel_selection(
    session, itinerary_id: int, request: HotelOptionRequest
) -> tuple[list[ItineraryItem], dict[int, ItineraryDay], set[int], list[int]]:
    """载入酒店条目、天索引与用户选择（LinkedHashSet 语义：去重且保持用户选择顺序）。"""
    hotel_items = list(
        session.execute(
            select(ItineraryItem).where(ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.item_type == "hotel")
        )
        .scalars()
        .all()
    )
    days = list(session.execute(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id)).scalars().all())
    day_by_id = {day.id: day for day in days}
    day_by_no = {day.day_no: day for day in days}
    existing_hotel_day_nos = {
        day.day_no for day in (day_by_id.get(item.day_id) for item in hotel_items) if day is not None
    }
    selected_day_nos = list(dict.fromkeys(request.dayNos or []))
    return hotel_items, day_by_no, existing_hotel_day_nos, selected_day_nos


def _selected_hotel_option(message: ItineraryChatMessage, hotel_name: str) -> dict[str, Any] | None:
    return next(
        (
            option
            for option in itinerary_chat.read_json_list(message.hotel_options_json)
            if isinstance(option, dict) and str(option.get("hotelName")) == hotel_name
        ),
        None,
    )


def _selected_room(option: dict[str, Any], room_type: str | None) -> dict[str, Any]:
    rooms = [room for room in option.get("roomTypes") or [] if isinstance(room, dict)]
    room = next((room for room in rooms if str(room.get("roomName")) == room_type), None)
    if room is None:
        raise ApiError(400, "该酒店不存在所选房型")
    return room


def _upsert_hotel_items(
    session,
    main: ItineraryMain,
    option: dict[str, Any],
    hotel_name: str,
    room_type: str,
    room_base_price: Decimal,
    room_description: str | None,
    selected_day_nos: list[int],
    day_by_no: dict[int, ItineraryDay],
    hotel_items: list[ItineraryItem],
) -> None:
    for day_no in selected_day_nos:
        day = day_by_no[day_no]
        item = next((existing for existing in hotel_items if existing.day_id == day.id), None)
        if item is None:
            item = ItineraryItem(
                itinerary_id=main.id,
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
        item.remark = _hotel_price_remark(room_type, room_base_price, stay_date, room_description)


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
