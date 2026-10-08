"""条目级落地：草稿里单条行程项 → `ItineraryItem` 行（自 itinerary_plan_apply 拆出）。

只承载「一条草稿项怎么落库」的纯语义与共享标量解析 helper；apply 主流程编排与
草稿结构校验仍在 `itinerary_plan_apply`，酒店选项落地在 `itinerary_plan_apply_hotel`。
本模块不反向 import 那两者（主模块/酒店模块单向依赖本模块，防环）。
"""

from __future__ import annotations

import logging
from datetime import datetime, time
from decimal import Decimal
from typing import Any, TypeGuard, overload

from app.common.envelope import ApiError
from app.db.models import ItineraryItem

logger = logging.getLogger(__name__)

MAX_POI_NAME = 128


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
        if existing is not None and existing.deleted == 1:
            # 恢复日不复活旧条目（spec §9.2）：软删旧项按全新条目落地，原行保持删除
            existing = None
        elif existing is None or name != existing.poi_name:
            raise ApiError(400, "行程项身份校验失败，未应用任何修改")

    # existing 与 entity 是同一个对象：Java 里紧随其后的 `setCost(existing.getCost())`
    # 是自赋值空操作（成本已被候选 POI 覆盖），这里同样不做保留，别"顺手修好"。
    entity = existing if existing is not None else ItineraryItem()
    entity.day_id = day_id
    entity.itinerary_id = itinerary_id
    _apply_item_fields(entity, raw_item, name)

    entity.sort_no = sort_no
    sort_no += 1
    if existing is None:
        session.add(entity)
    else:
        retained.add(existing.id)
    return sort_no


def _apply_item_fields(entity: ItineraryItem, raw_item: dict[str, Any], name: str) -> None:
    """把草稿项的候选字段覆盖到实体上（仅字段赋值段，供 _apply_plan_item 复用）。"""
    # 语料库退役：候选事实（坐标/地址/估价）以生成期富化后的草稿字段为准，
    # 不再回查 poi_knowledge。poi_id 存外部地点 ID（OTM xid 等）。
    entity.item_type = _str_or(raw_item.get("item_type"), "attraction")
    entity.poi_name = name
    if "poi_id" in raw_item:
        entity.poi_id = _str_or(raw_item.get("poi_id"), None)
    entity.address = _str_or(raw_item.get("address"), entity.address)
    if _is_number(raw_item.get("latitude")):
        entity.latitude = Decimal(str(raw_item["latitude"]))
    if _is_number(raw_item.get("longitude")):
        entity.longitude = Decimal(str(raw_item["longitude"]))
    if _is_number(raw_item.get("cost")):
        entity.cost = Decimal(str(raw_item["cost"]))

    entity.start_time = _parse_time(raw_item.get("start_time"))
    entity.end_time = _parse_time(raw_item.get("end_time"))
    duration_min = raw_item.get("duration_min")
    if _is_number(duration_min):
        entity.duration_min = int(duration_min)
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


def _is_number(value: Any) -> TypeGuard[int | float]:
    """Java 的 `instanceof Number` 不收布尔与字符串数字，这里同口径。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_int(value: Any) -> int | None:
    return int(value) if _is_number(value) else None


def _to_decimal(value: Any) -> Decimal | None:
    """候选卡片里的价格 → 两位小数 Decimal；缺失/非法返回 None（按无参考价拒绝）。"""
    if _is_number(value) and float(value) > 0:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    return None


# 双形态签名：fallback 静态为 str 的调用点（item_type 兜底值、NOT NULL 证据列沿用现值）
# 结果必为 str。新建实体上 fallback 取值运行时可能是未初始化的 None，此时回写 None 也不
# 入库——SQLAlchemy 对 INSERT 省略该列、列 default 照常生效（已实证），不会污染 NOT NULL 列。
@overload
def _str_or(value: Any, fallback: str) -> str: ...


@overload
def _str_or(value: Any, fallback: str | None) -> str | None: ...


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
