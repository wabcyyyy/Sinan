"""酒店候选生成与回复组织（G-2.6 拆分后：意图识别见 hotel_intent）。

职责：
- 生成可展示的酒店/房型候选卡片（hotel_options / explain_hotel_options）
  与候选目录解析（hotel_catalog / resolve_hotel_names）；
- 组织确认流回复（hotel_proposal_response）与草稿一致性校验（hotel_signature）；
- 从模型决策还原酒店意图（hotel_intent_from_decision）。

实现要点：
- 酒店是高风险操作，必须走独立确认流程（hotel_proposal），绝不直接进 plan_document；
- 意图识别与档次解析已抽到 hotel_intent（本模块导入其原语）。

依赖：document（行程投影）、hotel_intent（意图原语）、data.map_link、schemas.trip。
"""

import logging
from datetime import timedelta
from difflib import SequenceMatcher
from math import ceil

from app.agent.data import map_link
from app.common.season import season_factor, season_label
from app.schemas.trip import (
    ChatTurnRequest,
    ChatTurnResponse,
    HotelOption,
    HotelRoomOption,
)

from .document import _trip_plan_document
from .hotel_intent import (
    _INTENT_LABELS,
    _TIER_RANK,
    HotelIntent,
    _available_hotel_day_nos,
    _current_hotel_names,
    _current_hotel_tier,
    _has_explicit_stay_scope,
    _hotel_tier,
    _mentioned_hotel_names,
    _normalized_hotel_intent,
    _parse_date,
    _with_stay_scope,
)

logger = logging.getLogger(__name__)


def _hotel_base_price(hotel: dict) -> float | None:
    """酒店基准价：ticket_price 优先，回落 avg_cost（联网补池行的 LLM 估价）。

    LA2 后这是**唯一**价格口径：Hotellook 观测价机器已随死端点摘除（LA1 探针
    `dead`），酒店价如实估价 + 候选卡挂核实深链；观测价只在「查实时价」按需
    通道（`tools.hotel_quotes.search_live_hotel_quotes`）出现，不进本函数。
    """
    price = hotel.get("ticket_price")
    if price is None:
        price = hotel.get("avg_cost")
    return price


def _price_reason(hotel: dict) -> str:
    """候选卡的价格口径说明：估价行沿用其描述，不冒充观测价。"""
    return hotel.get("description") or "来自城市酒店知识库"


def _hotel_options(req: ChatTurnRequest, hotels: list[dict], intent: HotelIntent) -> list[HotelOption]:
    if not hotels:
        return []
    current_names = _current_hotel_names(req)
    current_costs = [
        float(item.get("cost"))
        for plan in req.plans
        for item in (plan.get("items") or [])
        if item.get("item_type") == "hotel" and isinstance(item.get("cost"), (int, float))
    ]
    existing_hotel_day_nos = _available_hotel_day_nos(req)
    if intent.requested_day_nos:
        available_day_nos = list(dict.fromkeys(existing_hotel_day_nos + list(intent.requested_day_nos)))
    elif _has_explicit_stay_scope(req.message):
        available_day_nos = list(range(1, req.days + 1))
    else:
        available_day_nos = existing_hotel_day_nos
    rooms = ceil(req.persons / 2)
    start_date = _parse_date(req.start_date)
    factor = season_factor(start_date)
    label = season_label(start_date)
    current_total = req.current_hotel_total
    if current_total is None and current_costs:
        current_total = sum(current_costs) * rooms
    budget_capacity = None
    if req.budget is not None:
        non_hotel_total = max((req.current_total or 0) - (req.current_hotel_total or 0), 0)
        budget_capacity = max(req.budget - non_hotel_total, 0)
    current_cost_by_day = {
        int(plan.get("day_no")): sum(
            float(item.get("cost"))
            for item in (plan.get("items") or [])
            if item.get("item_type") == "hotel" and isinstance(item.get("cost"), (int, float))
        )
        * rooms
        for plan in req.plans
        if isinstance(plan.get("day_no"), int)
    }
    if intent.requested_day_nos:
        current_scope_total = sum(current_cost_by_day.get(day_no, 0) for day_no in intent.requested_day_nos)
    elif current_total is not None and available_day_nos:
        current_scope_total = current_total / len(available_day_nos) * intent.requested_nights
    else:
        current_scope_total = (
            sum(current_cost_by_day.values()) / max(len(available_day_nos), 1) * intent.requested_nights
        )

    # 房型表已随 POI 库退役：一律走"基础房型"合成
    rooms_by_hotel: dict[int, list[dict]] = {}
    priced_day_nos = list(intent.requested_day_nos) or available_day_nos[: intent.requested_nights]
    # LA2：Hotellook 观测价已随死端点摘除——基准价一律估价链路（_hotel_base_price），
    # 核实出口是每张候选卡上的地图搜索深链（search_link，map_link 统一口径）。
    # 泛化“换个酒店”优先保持当前档次；若该档次没有除当前酒店外的可报价候选，
    # 自动退到最近档次，避免只返回一段没有卡片的空话。
    non_current_tiers = set()
    for hotel in hotels:
        base = _hotel_base_price(hotel)
        has_price = base is not None and float(base) > 0
        if (
            has_price
            and hotel.get("name") not in current_names
            and hotel.get("name") not in intent.requested_hotel_names
        ):
            non_current_tiers.add(_hotel_tier(hotel))
    allowed_tiers = {intent.target_tier}
    if not intent.requested_hotel_names and intent.target_tier not in non_current_tiers:
        target_rank = _TIER_RANK[intent.target_tier]
        nearest = sorted(non_current_tiers, key=lambda tier: abs(_TIER_RANK[tier] - target_rank))
        if nearest:
            allowed_tiers.add(nearest[0])
    candidates = []
    for hotel in hotels:
        base = _hotel_base_price(hotel)
        if base is None:
            continue
        try:
            base_price = float(base)
        except (TypeError, ValueError):
            continue
        if base_price <= 0:
            continue
        tier = _hotel_tier(hotel)
        if tier not in allowed_tiers:
            continue
        if hotel.get("name") in current_names and not intent.requested_hotel_names:
            continue
        if intent.requested_hotel_names and hotel.get("name") not in intent.requested_hotel_names:
            continue
        hotel_room_rows = rooms_by_hotel.get(int(hotel.get("id") or 0)) or [
            {
                "id": f"base-{hotel.get('id')}",
                "room_name": "基础房型",
                "base_price": base_price,
                "capacity": 2,
                "bed_type": None,
                "breakfast": None,
                "description": "知识库酒店基础房型参考价",
                "is_default": 1,
            }
        ]
        room_options: list[HotelRoomOption] = []
        for room in hotel_room_rows:
            room_base = float(room.get("base_price") or base_price)
            capacity = max(int(room.get("capacity") or 2), 1)
            room_count = ceil(req.persons / capacity)
            nightly_breakdown = []
            for day_no in priced_day_nos:
                stay_date = start_date + timedelta(days=day_no - 1) if start_date else None
                day_factor = season_factor(stay_date)
                nightly_breakdown.append(
                    {
                        "day_no": day_no,
                        "stay_date": stay_date.isoformat() if stay_date else None,
                        "season_label": season_label(stay_date),
                        "season_factor": day_factor,
                        "nightly_price": round(room_base * day_factor, 2),
                    }
                )
            room_total = round(sum(row["nightly_price"] for row in nightly_breakdown) * room_count, 2)
            projected = round((current_total or 0) - current_scope_total + room_total, 2)
            room_options.append(
                HotelRoomOption(
                    **{
                        "id": str(room.get("id")),
                        "room_name": room.get("room_name") or "基础房型",
                        "base_price": round(room_base, 2),
                        "nightly_price": nightly_breakdown[0]["nightly_price"]
                        if nightly_breakdown
                        else round(room_base * factor, 2),
                        "nights": intent.requested_nights,
                        "rooms": room_count,
                        "total_price": room_total,
                        "price_delta": round(room_total - current_scope_total, 2),
                        "projected_hotel_total": projected,
                        "within_budget": budget_capacity is None or projected <= budget_capacity,
                        "budget_overage": round(max(projected - budget_capacity, 0), 2)
                        if budget_capacity is not None
                        else 0,
                        "capacity": capacity,
                        "bed_type": room.get("bed_type"),
                        "breakfast": room.get("breakfast"),
                        "description": room.get("description"),
                        "is_default": bool(room.get("is_default")),
                        "nightly_breakdown": nightly_breakdown,
                    }
                )
            )
        room_options.sort(key=lambda room: (not room.is_default, room.base_price))
        selected_room = room_options[0]
        candidates.append(
            HotelOption(
                **{
                    "id": str(hotel.get("id") or hotel.get("name")),
                    "hotel_name": hotel.get("name"),
                    "tier": tier,
                    "address": hotel.get("address"),
                    "rating": hotel.get("rating"),
                    "base_price": selected_room.base_price,
                    "season_factor": factor,
                    "season_label": label,
                    "nightly_price": selected_room.nightly_price,
                    "nights": intent.requested_nights,
                    "rooms": selected_room.rooms,
                    "total_price": selected_room.total_price,
                    "price_delta": selected_room.price_delta,
                    "within_budget": selected_room.within_budget,
                    "budget_capacity": round(budget_capacity, 2) if budget_capacity is not None else None,
                    "budget_overage": selected_room.budget_overage,
                    "is_current": False,
                    "reason": _price_reason(hotel),
                    "search_link": map_link.map_search_url(
                        str(hotel.get("name") or ""),
                        req.city,
                        latitude=hotel.get("latitude"),
                        longitude=hotel.get("longitude"),
                    ),
                    "requested_nights": intent.requested_nights,
                    "requested_day_nos": list(intent.requested_day_nos),
                    "available_day_nos": available_day_nos,
                    "room_types": room_options,
                }
            )
        )
    candidates.sort(
        key=lambda item: (
            item.hotel_name not in intent.requested_hotel_names if intent.requested_hotel_names else False,
            not item.within_budget,
            -float(item.rating or 0),
            item.total_price if intent.action == "cheaper" else -item.total_price,
        )
    )
    return candidates[:3]


def _explain_hotel_options(
    req: ChatTurnRequest,
    options: list[HotelOption],
    intent: HotelIntent,
) -> str:
    intent_text = _INTENT_LABELS[intent.action]
    available_nights = len(options[0].available_day_nos) if options else req.days
    if intent.requested_day_nos:
        chosen_nights = "、".join(f"第{day_no}晚" for day_no in intent.requested_day_nos)
        next_step = f"入住晚次已确定为 **{chosen_nights}**，请选择酒店和房型。"
    elif intent.requested_nights >= available_nights:
        next_step = f"本次默认覆盖 **全部 {available_nights} 晚**，请选择酒店和房型。"
    else:
        next_step = f"请选择酒店、房型和 {intent.requested_nights} 个具体入住晚次。"
    if intent.requested_hotel_names:
        requested_hotels = "、".join(intent.requested_hotel_names)
        if intent.requested_day_nos:
            next_step = f"酒店已确定为 **{requested_hotels}**，入住晚次为 **{chosen_nights}**，请选择具体房型。"
        elif intent.requested_nights >= available_nights:
            next_step = f"酒店已确定为 **{requested_hotels}**，默认覆盖全部 {available_nights} 晚，请选择具体房型。"
        else:
            next_step = f"酒店已确定为 **{requested_hotels}**，请选择具体房型和 {intent.requested_nights} 个入住晚次。"

    def money(value: float | None) -> str:
        if value is None:
            return "未设置"
        return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}".rstrip("0").rstrip(".")

    lines = [
        "### 我理解你的需求",
        "",
        f"你希望以当前的 **{intent.base_tier}** 酒店为基准{intent_text}，"
        f"本次提供 **{len(options)} 家{intent.target_tier}酒店** 供比较。",
        "",
        "### 推荐对比",
        "",
    ]
    for option in options:
        room = option.room_types[0] if option.room_types else None
        status = (
            "预算内"
            if room and room.within_budget
            else f"预计超出总预算约 ￥{money(room.budget_overage if room else 0)}"
        )
        room_text = f"{room.room_name}约 ￥{money(room.total_price)}" if room else "暂无可用房型"
        lines.append(f"- **{option.hotel_name}**：{option.reason}；默认房型{room_text}，{status}。")
    lines.extend(
        [
            "",
            "### 下一步",
            "",
            next_step,
            "确认后只替换所选住宿，其他行程保持不变。",
        ]
    )
    return "\n".join(lines)


def _hotel_catalog(hotels: list[dict]) -> list[dict]:
    return [
        {
            "id": str(hotel.get("id") or ""),
            "name": hotel.get("name"),
            "tier": _hotel_tier(hotel),
            "description": hotel.get("description"),
        }
        for hotel in hotels
    ]


def _resolve_hotel_names(values: list, query: str, hotels: list[dict]) -> tuple[str, ...]:
    known = [str(hotel.get("name")) for hotel in hotels if hotel.get("name")]
    resolved = []
    for value in values:
        name = str(value or "").strip()
        if name in known and name not in resolved:
            resolved.append(name)
    if resolved or not query:
        return tuple(resolved)
    normalized = query.replace("安曼", "安缦").strip()
    substring = [name for name in known if normalized in name or name in normalized]
    if substring:
        return (substring[0],)
    scored = sorted(
        ((SequenceMatcher(None, normalized, name).ratio(), name) for name in known),
        reverse=True,
    )
    return (scored[0][1],) if scored and scored[0][0] >= 0.35 else ()


def _hotel_intent_from_decision(req: ChatTurnRequest, hotels: list[dict], data: dict) -> HotelIntent:
    hotel_request = data.get("hotel_request") if isinstance(data.get("hotel_request"), dict) else {}
    base_tier = _current_hotel_tier(req, hotels)
    action = str(hotel_request.get("action") or "same")
    if action not in _INTENT_LABELS:
        action = "same"
    target = str(hotel_request.get("target_tier") or "")
    intent = _normalized_hotel_intent(action, target, base_tier)
    candidate_mode = str(hotel_request.get("candidate_mode") or "recommend")
    names = _resolve_hotel_names(
        hotel_request.get("hotel_names") if isinstance(hotel_request.get("hotel_names"), list) else [],
        str(hotel_request.get("hotel_query") or ""),
        hotels,
    )
    explicitly_mentioned = _mentioned_hotel_names(req.message, hotels)
    # 推荐类请求不能让模型把上下文中的当前酒店误当成用户本轮明确指定的酒店。
    # 只有本轮原话确实出现酒店名，且模型选择 exact/specific 时才收窄为唯一候选。
    if candidate_mode != "exact" or action != "specific" or not explicitly_mentioned:
        names = ()
    else:
        names = tuple(name for name in names if name in explicitly_mentioned) or explicitly_mentioned
    if names:
        tiers = [_hotel_tier(hotel) for hotel in hotels if hotel.get("name") in names]
        intent = HotelIntent("specific", max(tiers, key=lambda tier: _TIER_RANK[tier]), base_tier)

    available = _available_hotel_day_nos(req)
    valid_day_nos = set(range(1, req.days + 1))
    raw_days = hotel_request.get("day_numbers")
    day_numbers = tuple(
        dict.fromkeys(
            int(day_no)
            for day_no in (raw_days if isinstance(raw_days, list) else [])
            if isinstance(day_no, int) and day_no in valid_day_nos
        )
    )
    try:
        night_count = int(hotel_request.get("night_count"))
    except (TypeError, ValueError):
        night_count = 0
    if day_numbers:
        night_count = len(day_numbers)
    night_count = max(1, min(night_count or len(available) or req.days, len(available) or req.days))
    intent = HotelIntent(
        action=intent.action,
        target_tier=intent.target_tier,
        base_tier=base_tier,
        requested_nights=night_count,
        requested_day_nos=day_numbers,
        requested_hotel_names=names,
    )
    # 入住晚次以用户本轮原话为准，不信任模型可能臆测的 day_numbers；同时
    # 统一检查“最后一晚/第 N 晚/住 N 晚”是否落在实际有住宿安排的晚次内。
    return _with_stay_scope(intent, req, hotels)


def _hotel_signature(plans: list[dict]) -> list[tuple[int, str]]:
    return sorted(
        (int(plan.get("day_no")), str(item.get("poi_name")))
        for plan in plans
        if isinstance(plan, dict) and isinstance(plan.get("day_no"), int)
        for item in (plan.get("items") or [])
        if item.get("item_type") == "hotel"
    )


def _hotel_proposal_response(
    req: ChatTurnRequest,
    hotels: list[dict],
    intent: HotelIntent,
    operations: list[dict] | None = None,
    fallback_reply: str | None = None,
) -> ChatTurnResponse:
    # 最终边界：无论意图来自 LLM 还是确定性兜底，都再次用本轮原话校正
    # 入住晚次，防止模型输出的 day_numbers 覆盖“最后一晚/第 N 晚”等明确表达。
    intent = _with_stay_scope(intent, req, hotels)
    if not _available_hotel_day_nos(req) and not _has_explicit_stay_scope(req.message):
        return ChatTurnResponse(
            reply=(
                "### 当前没有可替换的住宿\n\n"
                "当前行程中没有已安排的住宿晚次，因此无法直接执行换酒店。"
                "请先补充入住日期或延长行程，本次没有修改。"
            ),
            plans=[],
            changed=False,
            hotel_options=[],
            requires_confirmation=False,
            plan_document=_trip_plan_document(req),
            operations=operations or [],
        )
    if intent.invalid_scope:
        return ChatTurnResponse(
            reply=(
                "### 入住晚次超出当前行程\n\n"
                f"当前行程只有 **{len(_available_hotel_day_nos(req))} 晚住宿安排**，"
                "请先延长行程，或改为当前行程中的具体入住晚次。行程没有被修改。"
            ),
            plans=[],
            changed=False,
            hotel_options=[],
            requires_confirmation=False,
            plan_document=_trip_plan_document(req),
            operations=operations or [],
        )
    options = _hotel_options(req, hotels, intent)
    pending_action = {
        "type": "replace_hotel",
        "hotel_names": list(intent.requested_hotel_names),
        "target_tier": intent.target_tier,
        "day_numbers": list(intent.requested_day_nos),
        "night_count": intent.requested_nights,
        "requires_confirmation": True,
    }
    if options:
        reply = _explain_hotel_options(req, options, intent)
    else:
        reply = fallback_reply or (
            "### 暂时没有可展示的酒店候选\n\n"
            "当前城市的酒店参考数据不足，暂时无法生成可选择的酒店和房型卡片；"
            "行程没有被修改，请稍后重试或换一个住宿档次。"
        )
    return ChatTurnResponse(
        reply=reply,
        plans=[],
        changed=False,
        hotel_options=options,
        requires_confirmation=bool(options),
        plan_document=_trip_plan_document(req),
        operations=operations or [],
        pending_action=pending_action,
    )
