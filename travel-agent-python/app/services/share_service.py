"""公开分享（SPEC v2.3 §6.6）：只读 capability URL + 脱敏 VO。

四条口径（做错即泄露或扩大攻击面）：
1. **统一 404**：不存在 / 已过期 / 已撤销 / 已软删一律「链接不存在或已失效」，
   不给枚举者任何区分信号；`share_expires_at < now` 视为不存在；
2. **白名单出参**：`SharedItineraryVO` 只挑字段（见 `_shared_vo`），**绝不照抄详情 dict**——
   userId / 内部主键 / chat / trace / qualityReport 一律不出；
3. **按 IP 限流**：匿名访问面必须限流（E14），Redis 滑动窗口 + 进程内兜底；
4. **token 只在本模块生成**（`token_urlsafe(24)` ≈32 字符），禁止散落路由；
   重复创建会轮换 token——旧链接立即失效，这是有意的安全属性。
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.common.config import settings
from app.common.envelope import ApiError
from app.common.vo_json import iso_date, iso_datetime, iso_time, number
from app.db.models import BudgetDetail, ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.services import itinerary_query, state_and_sessions

logger = logging.getLogger(__name__)

ALLOWED_EXPIRE_DAYS = (None, 7, 30)
SHARE_RATE_WINDOW_SECONDS = 60
INVALID_LINK_MESSAGE = "链接不存在或已失效"


# ---------- owner 面（authenticated） ----------


def create_share(user_id: int, itinerary_id: int, expire_days: int | None) -> dict[str, Any]:
    if expire_days not in ALLOWED_EXPIRE_DAYS:
        raise ApiError(400, "expireDays 仅支持 7 或 30")
    main = itinerary_query.find_owned_main(user_id, itinerary_id)  # 归属失败一律 404
    if main.archived:
        raise ApiError(400, "已归档行程不能创建分享")

    token = secrets.token_urlsafe(24)
    expires_at = None if expire_days is None else datetime.now() + timedelta(days=expire_days)
    _write_share_columns(user_id, itinerary_id, token=token, expires_at=expires_at)
    return {
        "shareToken": token,
        "shareUrl": f"/s/{token}",
        "shareExpiresAt": iso_datetime(expires_at),
    }


def get_share(user_id: int, itinerary_id: int) -> dict[str, Any]:
    main = itinerary_query.find_owned_main(user_id, itinerary_id)
    if not main.share_token:
        return {"shared": False}
    return {
        "shared": True,
        "shareToken": main.share_token,
        "shareUrl": f"/s/{main.share_token}",
        "shareExpiresAt": iso_datetime(main.share_expires_at),
    }


def remove_share(user_id: int, itinerary_id: int) -> None:
    """撤销：清空 token 与过期时间，链接立即失效（已落盘封面文件不回收，同 §6.3 无 GC）。"""
    itinerary_query.find_owned_main(user_id, itinerary_id)
    _write_share_columns(user_id, itinerary_id, token=None, expires_at=None)


def _write_share_columns(user_id: int, itinerary_id: int, *, token: str | None, expires_at: datetime | None) -> None:
    with session_scope() as session:
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(share_token=token, share_expires_at=expires_at)
        )
    itinerary_query.evict_detail(user_id, itinerary_id)  # 详情含 shareToken，必须精确失效


# ---------- 匿名面 ----------


def enforce_rate_limit(client_ip: str) -> None:
    count = state_and_sessions.sliding_hit(f"share:view:{client_ip}", SHARE_RATE_WINDOW_SECONDS)
    if count > settings.share_rate_limit_per_minute:
        raise ApiError(429, "请求过于频繁，请稍后再试")


def view_shared(token: str) -> dict[str, Any]:
    normalized = (token or "").strip()
    if not normalized:
        raise ApiError(404, INVALID_LINK_MESSAGE)
    with session_scope() as session:
        # 软删行程自动不可见（do_orm_execute 钩子注入 deleted=0），与撤销共用同一 404
        main = session.execute(
            select(ItineraryMain).where(ItineraryMain.share_token == normalized)
        ).scalar_one_or_none()
        if main is None or _expired(main.share_expires_at):
            raise ApiError(404, INVALID_LINK_MESSAGE)
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == main.id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        items = (
            session.execute(
                select(ItineraryItem)
                .where(ItineraryItem.itinerary_id == main.id)
                .order_by(ItineraryItem.day_id, ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        )
        budgets = session.execute(select(BudgetDetail).where(BudgetDetail.itinerary_id == main.id)).scalars().all()
        return _shared_vo(main, days, items, budgets)


def _expired(expires_at: datetime | None) -> bool:
    return expires_at is not None and expires_at < datetime.now()


# ---------- 脱敏 VO（白名单） ----------


def _shared_vo(
    main: ItineraryMain,
    days: list[ItineraryDay],
    items: list[ItineraryItem],
    budgets: list[BudgetDetail],
) -> dict[str, Any]:
    items_by_day: dict[int, list[ItineraryItem]] = {}
    for item in items:
        items_by_day.setdefault(item.day_id, []).append(item)

    day_list: list[dict[str, Any]] = []
    for day in days:
        day_items = [_shared_item(item) for item in items_by_day.get(day.id, [])]
        day_total = sum((entry["cost"] or 0.0) for entry in day_items)
        metadata = _loads_object(day.metadata_json) or {}
        day_list.append(
            {
                "dayNo": day.day_no,
                "travelDate": iso_date(day.travel_date),
                "theme": metadata.get("theme"),
                "note": day.note,
                "dayTotalAmount": round(day_total, 2),
                "items": day_items,
            }
        )

    budget_list = [
        {"category": budget.category, "amount": number(budget.amount), "itemCount": budget.item_count}
        for budget in budgets
    ]
    total_amount = sum((entry["amount"] or 0.0) for entry in budget_list)
    return {
        "city": main.city,
        "title": main.title,
        "days": main.days,
        "persons": main.persons,
        "startDate": iso_date(main.start_date),
        "endDate": iso_date(main.end_date),
        "budget": number(main.budget),
        "hotelTier": main.hotel_tier,
        "tripTheme": main.trip_theme,
        "coverUrl": main.cover_url,
        "coverCredit": _loads_object(main.cover_credit),
        "totalAmount": round(total_amount, 2),
        "dayList": day_list,
        "budgetList": budget_list,
        "planNote": main.plan_note,
    }


def _shared_item(item: ItineraryItem) -> dict[str, Any]:
    # 白名单十键，刻意不含 DB 主键（分享页无写接口；前端锚点用 D{n}-{index} 虚拟键）
    return {
        "poiName": item.poi_name,
        "itemType": item.item_type,
        "address": item.address,
        "startTime": iso_time(item.start_time),
        "endTime": iso_time(item.end_time),
        "cost": number(item.cost),
        "image": item.image_url,
        "latitude": number(item.latitude),
        "longitude": number(item.longitude),
        "source": item.source,
    }


def _loads_object(raw: str | None) -> dict[str, Any] | None:
    """损坏的可选 JSON 列不应阻塞分享读取（与详情读路径同口径）。"""
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("share json column parse failed, returned null instead")
        return None
    return parsed if isinstance(parsed, dict) else None
