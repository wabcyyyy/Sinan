"""预算重算（移植自 Java `BudgetEngineImpl`）。

口径必须与前端「每日费用」明细一致，两条曾经踩过的坑保留原注释：
- 餐饮/门票 = 条目单价 × 人数；**不再**做"按城市基准价封顶"的展示层钳制，
  那会让分类合计与明细对不上、改价后总价看起来"不动"；离谱价格在 Agent 生成侧钳制。
- 酒店按所选房型的容量算房间数（`ceil(persons / capacity)`），不假定每间住 2 人。
- 餐饮/酒店被模型写成 0 视为无效，回落知识库权威价；免费景点保留 0。
"""

from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, select

from app.db.models import (
    BudgetDetail,
    CityConsumption,
    ItineraryItem,
    ItineraryMain,
)
from app.db.session import session_scope

DEFAULT_TRANSPORT_PER_DAY = Decimal("35.00")
ZERO = Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def recalculate(itinerary_id: int) -> list[BudgetDetail]:
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            return []
        persons = main.persons or 1
        days = main.days or 1
        items = session.execute(select(ItineraryItem).where(ItineraryItem.itinerary_id == itinerary_id)).scalars().all()

        ticket = meal = hotel = ZERO
        ticket_count = meal_count = hotel_count = 0

        for item in items:
            item_type = item.item_type
            # 语料库退役后无"知识库权威价"回落：条目成本缺失或为 0 即不计入，如实缺省
            unit = Decimal(item.cost) if item.cost is not None and not _is_zero_cost_for(item_type, item.cost) else None
            if unit is None:
                continue
            if item_type == "attraction":
                ticket += unit
                ticket_count += 1
            elif item_type == "food":
                meal += unit
                meal_count += 1
            elif item_type == "hotel":
                # 房型表已退役：容量统一按每间 2 人计
                rooms = math.ceil(persons / 2)
                hotel += unit * Decimal(rooms)
                hotel_count += 1

        consumption = session.execute(
            select(CityConsumption).where(CityConsumption.city == main.city)
        ).scalar_one_or_none()
        transport_per_day = (
            consumption.transport_price if consumption and consumption.transport_price else DEFAULT_TRANSPORT_PER_DAY
        )
        transport = Decimal(transport_per_day) * Decimal(days) * Decimal(persons)
        transport_remark = "按城市系数×天数×人数"
        # L14：有观测到的往返报价时，城际大交通用**观测价**（最低一条 × 人数）
        # 并如实注明来源与观测时点；没有报价则维持城市系数估算（estimated 叙事）。
        observed = _observed_flight_total(main.flight_quotes, persons)
        if observed is not None:
            transport, transport_remark = observed
        ticket *= Decimal(persons)
        meal *= Decimal(persons)

        hotel_remark = "按每晚房费×房间数合计"

        session.execute(delete(BudgetDetail).where(BudgetDetail.itinerary_id == itinerary_id))
        created: list[BudgetDetail] = []
        for category, amount, count, remark in (
            ("门票", ticket, ticket_count, "按景点票价×人数"),
            ("餐饮", meal, meal_count, "按餐饮单价×人数"),
            ("交通", transport, days, transport_remark),
            ("酒店", hotel, hotel_count, hotel_remark),
        ):
            detail = BudgetDetail(
                itinerary_id=itinerary_id,
                category=category,
                amount=_money(amount),
                item_count=count,
                remark=remark,
            )
            session.add(detail)
            created.append(detail)
        session.flush()
        return created


def _is_zero_cost_for(item_type: str | None, cost: Decimal) -> bool:
    if item_type not in ("food", "hotel"):
        return False
    return cost == ZERO


def _observed_flight_total(raw: object, persons: int) -> tuple[Decimal, str] | None:
    """报价列 → (往返观测总价 × 人数, 证据 remark)；无可用报价返回 None。

    只取**最低一条**报价作预算口径：报价列可能有多个日期/航司选项，预算是
    "这趟大概要花多少"，取最低价与前端 QuoteStrip 的首选一致。证据 remark
    带上 provider 与观测时点（BudgetDetail 没有结构化证据字段，这是本表能
    承载的最诚实形式）；时点缺失就不写时点，不编造。
    """
    rows = _loads_quotes(raw)
    priced = [row for row in rows if _positive_price(row.get("price")) is not None]
    if not priced:
        return None
    best = min(priced, key=lambda row: _positive_price(row.get("price")) or 0.0)
    price = _positive_price(best.get("price"))
    if price is None:
        return None
    provider = str(best.get("provider") or "unknown").strip() or "unknown"
    observed_at = str(best.get("retrieved_at") or "").strip()
    stamp = f"（{observed_at} 观测，出发前请核实）" if observed_at else "（出发前请核实）"
    return _money(Decimal(str(price)) * Decimal(persons)), f"按 {provider} 观测往返价×人数{stamp}"


def _loads_quotes(raw: object) -> list[dict]:
    """报价列容错解析：MySQL JSON 可能给回 list，存量/方言可能给回字符串。"""
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
        if not text.strip():
            return []
        try:
            raw = json.loads(text)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _positive_price(value: object) -> float | None:
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


# 房型表已退役：酒店容量统一按每间 2 人计。
