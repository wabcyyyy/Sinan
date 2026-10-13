"""逐日生成结果的落库与行程终态写回（移植自 Java `ItineraryDayPersistenceService`）。

事务边界刻意做小：**远程 Agent 调用全在事务外**，只有「一天的完整结果」就绪时才在一个短事务里
清旧草稿 → 写全部 items → 置 SUCCEEDED。反过来（把 LLM 调用包进事务）会长时间占住连接池连接，
而 `DB_POOL_MAX` 默认只有 10。预算重算同样移出本服务（由编排层提交到富化池）。

四处必须与 Java 一致的细节：
1. `itemMapper.delete(day_id)` 是**软删**（@TableLogic），用来清掉失败重试/续跑留下的半成品；
2. `generation_status` 只有 SUCCEEDED 才算齐备，`markRunning`/`markFailed` 都先过幂等门；
3. `hotel` 项在 `dayNo > stayNights` 时**跳过且不占 sort_no**（最后一天不住店）；
4. 行程级终态一律走**单列 UPDATE**，禁止整行回写——富化/生成是异步的，整行回写会用陈旧实体
   覆盖用户在这期间的并发编辑。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from app.agent import sync_schedule_summary
from app.common.envelope import FAIL_TRIP_NOTE, ApiError
from app.common.vo_json import parse_time_safe
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.schemas.trip import DailyPlan, FactEvidence
from app.services import generation_gate, itinerary_version, stay_hotels
from app.services.day_delivery_gate import hard_delivery_blockers

logger = logging.getLogger(__name__)

LEGACY_RESUME_MARKER = "[已自动续跑一次]"
MAX_ERROR_LENGTH = 500
FALLBACK_DAY_ERROR = "每日生成失败"


def find_day(itinerary_id: int, day_no: int) -> ItineraryDay | None:
    with session_scope() as session:
        return session.execute(
            select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.day_no == day_no)
        ).scalar_one_or_none()


def persist(
    itinerary_id: int,
    request: Any,
    day_no: int,
    plan: DailyPlan,
    action_id: str,
    fingerprint: str,
    allow_overwrite: bool = False,
) -> None:
    """把一天的完整结果写库：清旧 item → 写新 item → 置 SUCCEEDED。"""
    with session_scope() as session:
        day = _require_day(session, itinerary_id, day_no)
        generation_gate.verify_action(day, action_id, fingerprint)
        if day.generation_status == "SUCCEEDED" and not allow_overwrite:
            return  # 幂等：已成功且不强制覆盖时一字不改

        stay_nights = _stay_nights(request)
        stay_hotels.fill_day_hotel(session, itinerary_id, day_no, stay_nights, plan)
        plan.items = [item for item in plan.items if item.item_type != "hotel" or day_no <= stay_nights]
        sync_schedule_summary(plan)
        day.generation_action_id = action_id
        day.generation_fingerprint = fingerprint
        day.generation_status = "RUNNING"
        day.generation_error = None
        if plan.note is not None:
            day.note = plan.note
        day.metadata_json = _day_metadata_json(plan)

        # 清掉上一次失败/续跑留下的半成品
        session.execute(
            update(ItineraryItem).where(ItineraryItem.day_id == day.id, ItineraryItem.deleted == 0).values(deleted=1)
        )

        for sort_no, item in enumerate(plan.items or []):
            session.add(_build_item(itinerary_id, day.id, item, sort_no))

        blockers = hard_delivery_blockers(day_no, plan, requirements=getattr(request, "requirements_struct", None))
        if blockers:
            logger.warning("day %s kept PENDING (hard blockers): %s", f"{itinerary_id}.{day_no}", blockers)
            day.generation_status = "PENDING"
            day.generation_error = "；".join(blockers)[:MAX_ERROR_LENGTH] or None
        else:
            day.generation_status = "SUCCEEDED"
            day.generation_error = None
        itinerary_version.bump_planning_revision(session, itinerary_id)


def _build_item(itinerary_id: int, day_id: int, item: Any, sort_no: int) -> ItineraryItem:
    return ItineraryItem(
        day_id=day_id,
        itinerary_id=itinerary_id,
        item_type=item.item_type,
        poi_name=item.poi_name,
        poi_id=item.poi_id,
        address=item.address,
        latitude=_decimal(item.latitude),
        longitude=_decimal(item.longitude),
        start_time=parse_time_safe(item.start_time),
        end_time=parse_time_safe(item.end_time),
        duration_min=item.duration_min,
        cost=_decimal(item.cost),
        cost_currency=item.cost_currency,
        tag=item.tag,
        remark=item.remark,
        why_note=item.why_this,  # 契约 why_this → 列 why_note（PDF/VO 直接读列）
        open_time=item.open_time,
        image_url=item.image,  # 契约 image → 列 image_url
        source=item.source,
        source_updated_at=_parse_datetime_safe(item.source_updated_at),
        verification_status=_default_str(item.verification_status, "unverified"),
        value_kind=_default_str(item.value_kind, "generated"),
        freshness_status=_default_str(item.freshness_status, "unknown"),
        review_requirement=_default_str(item.review_requirement, "before_departure"),
        fact_evidence_json=_fact_evidence_json(item.fact_evidence),
        sort_no=sort_no,
    )


def mark_running(day_id: int, action_id: str, fingerprint: str) -> None:
    """开始生成：只动状态三列，不碰 note/metadata（留给落库那一步）。"""
    with session_scope() as session:
        day = _require_day_by_id(session, day_id)
        generation_gate.verify_action(day, action_id, fingerprint)
        day.generation_action_id = action_id
        day.generation_fingerprint = fingerprint
        day.generation_status = "RUNNING"
        day.generation_error = None


def mark_failed(day_id: int, action_id: str, fingerprint: str, error: str | None) -> None:
    with session_scope() as session:
        day = _require_day_by_id(session, day_id)
        generation_gate.verify_action(day, action_id, fingerprint)
        day.generation_status = "FAILED"
        day.generation_error = (error or FALLBACK_DAY_ERROR)[:MAX_ERROR_LENGTH]


def reset_day_pending(day_id: int, action_id: str, fingerprint: str, reason: str | None = None) -> None:
    """终检不过的天退回 PENDING（整段流式主路径的后置终检用）。

    只动状态列，不删 items：落库的违规行程仍是当前最新事实，逐日兜底循环重生成时由 `persist` 的清旧写新覆盖；
    若重生成再失败，用户至少还能看到这一版（与 day_patch 覆盖路径的中间态口径一致）。同一动作指纹内重置，过幂等门。
    """
    with session_scope() as session:
        day = _require_day_by_id(session, day_id)
        generation_gate.verify_action(day, action_id, fingerprint)
        day.generation_status = "PENDING"
        day.generation_error = (reason or "").strip()[:MAX_ERROR_LENGTH] or None
        itinerary_version.bump_planning_revision(session, day.itinerary_id)


_Point = tuple[str, str, float | None, float | None]


def _query_points(stmt: Any) -> list[_Point]:
    """执行点位查询 → (名字, 类型, 纬度, 经度)；空名剔除（P1-B 跨天判重口径）。"""
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return [(name, str(t or ""), lat, lng) for name, t, lat, lng in rows if name and name.strip()]


def append_existing_items(day_id: int, used_names: list[str], used_points: list[_Point] | None = None) -> None:
    """把某天已有的点位（名字+类型+坐标）就地登记进跨天去重表（按 sort_no 升序）。

    坐标同场收集（P1-B）：只带名字时，名称变体（"都厅展望室"→"都厅南展望室"）绕过
    坐标通道；带坐标登记后变体落地即被近距离判重抓到。
    """
    fresh = _query_points(
        select(ItineraryItem.poi_name, ItineraryItem.item_type, ItineraryItem.latitude, ItineraryItem.longitude)
        .where(ItineraryItem.day_id == day_id)
        .order_by(ItineraryItem.sort_no)
    )
    used_names.extend(point[0] for point in fresh)
    if used_points is not None:
        used_points.extend(fresh)


def existing_hotel(day_id: int) -> str | None:
    with session_scope() as session:
        return (
            session.execute(
                select(ItineraryItem.poi_name).where(ItineraryItem.day_id == day_id, ItineraryItem.item_type == "hotel")
            )
            .scalars()
            .first()
        )


def complete_trip(itinerary_id: int, all_succeeded: bool) -> None:
    """按天状态汇总写终态；`status` 仍是 2（用户可见语义不变）。"""
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            return
        plan_note = main.plan_note
        if plan_note and LEGACY_RESUME_MARKER in plan_note:
            plan_note = plan_note.replace(LEGACY_RESUME_MARKER, "").strip()
        if all_succeeded and plan_note and plan_note.startswith((FAIL_TRIP_NOTE, "生成失败：")):
            plan_note = None
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(
                status=2,
                gen_state="COMPLETED" if all_succeeded else "PARTIAL",
                gen_finished_at=datetime.now(),
                gen_resumed=False if all_succeeded else ItineraryMain.gen_resumed,  # PARTIAL 保留续跑旗，防无限重拉
                title=f"{main.city}{main.days}日游",
                plan_note=plan_note or None,
            )
        )


def fail_trip(itinerary_id: int, message: str | None, *, user_facing: bool = False, code: str | None = None) -> None:
    """行程级失败终态；保留 gen_resumed 防无限续跑，plan_note 仅存用户文案。
    code 来自 envelope.error_code，只透出异常类别，供用户与运维关联，不透出原始异常。
    """
    text = (message or "").strip()
    logger.warning("itinerary %s failed: %s", itinerary_id, text[:MAX_ERROR_LENGTH])
    note = f"生成失败：{text}" if user_facing and text else FAIL_TRIP_NOTE
    note = f"{note}（错误码 {code}）" if code else note
    with session_scope() as session:
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(status=3, gen_state="FAILED", gen_finished_at=datetime.now(), plan_note=note)
        )


def set_trip_theme(itinerary_id: int, theme: str | None) -> None:
    """整趟主题单列写回（幂等重写；禁止整行 update 以免覆盖并发编辑）。"""
    if not theme or not theme.strip():
        return
    with session_scope() as session:
        session.execute(update(ItineraryMain).where(ItineraryMain.id == itinerary_id).values(trip_theme=theme.strip()))


def mark_generating(itinerary_id: int) -> None:
    with session_scope() as session:
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(gen_state="GENERATING", gen_started_at=datetime.now())
        )


def save_flight_quotes(itinerary_id: int, quotes: list[dict[str, Any]] | None) -> None:
    """往返报价落库（L14）：单列 UPDATE；`None`/空列表一律写 NULL。

    为什么空就写 NULL 而不是保留旧值：报价是**观测事实**，这一轮没查到就是这一轮没有（工具层已留下缺席原因）；
    留着上一轮的旧价，详情页会显示一份本行程从未观测过的价格——那正是本计划要消灭的"冒充"。
    """
    payload = json.dumps(quotes, ensure_ascii=False) if quotes else None
    with session_scope() as session:
        session.execute(update(ItineraryMain).where(ItineraryMain.id == itinerary_id).values(flight_quotes=payload))


def save_origin_city(itinerary_id: int, origin_city: str | None) -> None:
    """出发地单列写回（L14）：只在非空时写，绝不把已填的出发地清成 NULL。

    用户建行程时填了出发地，之后的一次生成/续跑没带上它（例如前端旧版本、或 chat 重排路径不传 origin），清空会让
    "查实时价"从可用变不可用——而出发地是**用户输入的事实**，不该被一次生成流程抹掉。
    """
    text = str(origin_city or "").strip()
    if not text:
        return
    with session_scope() as session:
        session.execute(update(ItineraryMain).where(ItineraryMain.id == itinerary_id).values(origin_city=text[:64]))


def all_days_succeeded(itinerary_id: int) -> bool:
    with session_scope() as session:
        statuses = (
            session.execute(select(ItineraryDay.generation_status).where(ItineraryDay.itinerary_id == itinerary_id))
            .scalars()
            .all()
        )
    return all(status == "SUCCEEDED" for status in statuses)


def unfinished_day_nos(itinerary_id: int) -> list[int]:
    with session_scope() as session:
        rows = session.execute(
            select(ItineraryDay.day_no, ItineraryDay.generation_status).where(ItineraryDay.itinerary_id == itinerary_id)
        ).all()
    return sorted(day_no for day_no, status in rows if status != "SUCCEEDED")


# ---------- 内部工具 ----------


def _require_day(session, itinerary_id: int, day_no: int) -> ItineraryDay:
    day = session.execute(
        select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.day_no == day_no)
    ).scalar_one_or_none()
    if day is None:
        raise ApiError(500, "行程日不存在")
    return day


def _require_day_by_id(session, day_id: int) -> ItineraryDay:
    day = session.get(ItineraryDay, day_id)
    if day is None:
        raise ApiError(500, "行程日不存在")
    return day


def _stay_nights(request: Any) -> int:
    nights = getattr(request, "stay_nights", None)
    if nights is None:
        return max((getattr(request, "days", 0) or 0) - 1, 0)
    return int(nights)


_METADATA_FIELDS = (
    ("theme", None),
    ("mini_route", "miniRoute"),
    ("backup_plan", "backupPlan"),
    ("photo_spots", "photoSpots"),
    ("practical_notes", "practicalNotes"),
    ("day_options", "dayOptions"),
)


def _day_metadata_json(plan: DailyPlan) -> str | None:
    """键插入顺序即 JSON 键顺序（与 Java 的 LinkedHashMap 一致，diff 会看顺序）。"""
    metadata: dict[str, Any] = {}
    for attribute, output_key in _METADATA_FIELDS:
        value = getattr(plan, attribute, None)
        key = output_key or attribute
        if value is None:
            continue
        if output_key and isinstance(value, (list, dict)) and not value:
            continue  # 空集合不落库（Java 判 isEmpty）
        metadata[key] = _plain(value)
    if not metadata:
        return None
    try:
        return json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("day metadata serialization failed, dropped metadata json instead: %s", exc)
        return None


def _fact_evidence_json(evidence: dict[str, Any]) -> str | None:
    if not evidence:
        return None
    payload = {
        key: (value.model_dump(by_alias=True) if isinstance(value, FactEvidence) else _plain(value))
        for key, value in evidence.items()
    }
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        logger.warning("fact evidence serialization failed, stored null instead: %s", exc)
        return None


def _plain(value: Any) -> Any:
    """Pydantic 模型 → 可 JSON 序列化的普通结构（camel 键，与 wire 契约同形）。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


def _decimal(value: Any) -> Decimal | None:
    """float/Decimal 一律经 str 转 Decimal，避免 29.99 这类值以二进制浮点写进 DECIMAL 列。"""
    return None if value is None else value if isinstance(value, Decimal) else Decimal(str(value))


def _default_str(value: str | None, fallback: str) -> str:
    return fallback if value is None or not value.strip() else value


def _parse_datetime_safe(value: str | None) -> datetime | None:
    if value is None or not (text := value.strip()):
        return None
    try:
        # 带偏移的串：按原始偏移折算成 LocalDateTime，不做时区换算（同 OffsetDateTime.toLocalDateTime）
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        logger.debug("datetime value unparsable, left empty instead: %s", value)
        return None
