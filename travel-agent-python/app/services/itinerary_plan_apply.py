"""草稿应用与酒店选项（移植自 Java `ItineraryPlanApplyService`）。

三条不可松手的语义：

1. **只认服务端草稿**：Java 的门面 `ItineraryServiceImpl:131-134` 明确**丢弃**客户端 body 里的
   `plans`，真正落库的内容来自 `actionMessageId` 指向的那条 AI 消息，并用 `baseRevision` 指纹
   做乐观并发（四道 409 见 `itinerary_chat.require_pending_action`）。迁移时若"顺手"改成信任
   `plans`，等于把「AI 建议 → 用户确认」这条审计链关掉：客户端可以借 apply 接口写任意点位。
2. 应用是**整份替换**：草稿里没出现的行程项软删、天数变少则尾部日期软删；日元数据（主题/
   迷你路线/备选/机位/实用提示）按草稿整份覆盖，草稿没带就清空，不给上一版留残影。
3. 项上的 `id` 是**身份校验**：带 id 表示「改这一条」，名称与库里不一致即整单 400、什么都不写。

模块布局：条目级落地与共享标量 helper 在 `itinerary_plan_apply_items`，酒店选项落地在
`itinerary_plan_apply_hotel`；本模块保留两条应用主流程与草稿结构校验（依赖单向，防环）。
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from app.agent import (
    check_constraints,
    confirm_thread,
    day_policy_for,
    resume_confirmation,
)
from app.common.envelope import ApiError
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.schemas.business.itinerary import HotelOptionRequest
from app.schemas.trip import MAX_TRIP_DAYS
from app.schemas.trip_requirements import TripRequirements, canonical_requirements_payload
from app.services import (
    budget_engine,
    itinerary_chat,
    itinerary_plan_apply_hotel,
    itinerary_query,
    itinerary_version,
)
from app.services.itinerary_plan_apply_items import (
    MAX_POI_NAME,
    _apply_plan_item,
    _as_int,
    _is_number,
    _parse_time,
)

logger = logging.getLogger(__name__)

VALID_ITEM_TYPES = ("attraction", "food", "hotel", "transport")
MAX_ITEMS_PER_DAY = 20
DEFAULT_NEW_DAY_NOTE = "宽松安排"


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
        # M4（spec §9.2）：草稿携带的整数修订基准（存量草稿 None → 退化 hash 比对）
        base_planning_revision = itinerary_chat.action_base_planning_revision(message)
        # M4（spec §9.1）：拟变更需求随草稿走——按拟变更需求验证草稿；确认应用时
        # 同事务写入正式需求（未确认前正式行程与正式需求都不改）
        proposed_requirements = _proposed_requirements(plans)
        days = list(
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        _validate_plans(plans, requirements=proposed_requirements)
        existing_items = _load_item_identity(session, itinerary_id)
        retained_item_ids: set[int] = set()
        day_by_no = {day.day_no: day for day in days}
        # M4：校验/快照/条目写/天数同步/revision/消费 pending action 同一事务
        itinerary_version.append_version_in_session(session, user_id, itinerary_id, "apply_plans", "应用草稿前快照")

        _revive_or_create_days(session, main, itinerary_id, plans, day_by_no)
        _apply_days(session, plans, itinerary_id, day_by_no, existing_items, retained_item_ids)

        for item_id, item in existing_items.items():
            if item_id not in retained_item_ids:
                item.deleted = 1  # 同 @TableLogic 的 deleteById
        for day in days:
            if day.day_no > len(plans):
                day.deleted = 1

        _sync_apply_metadata(main, plans, day_by_no)

        if proposed_requirements is not None:
            # 确认即生效：拟变更需求与行程内容同一事务写正式需求（单一真源仍在 V13 列）
            main.requirements_json = canonical_requirements_payload(proposed_requirements)
        budget_engine.recalculate(itinerary_id)
        itinerary_chat.consume_pending_action(message)
        # M4：写后快照也进同一事务（完整状态入版本历史）
        itinerary_version.append_version_in_session(session, user_id, itinerary_id, "apply_plans", "应用草稿完成")
        # M4 CAS：修订基准不匹配（并发写已推进）→ 409，整事务回滚（含快照/条目/草稿消费）
        itinerary_version.bump_planning_revision_with_cas(session, itinerary_id, base_planning_revision)

    itinerary_query.evict_detail(user_id, itinerary_id)
    return itinerary_query.detail(user_id, itinerary_id)


def _proposed_requirements(plans: list[dict[str, Any]]) -> TripRequirements | None:
    """M4（spec §9.1）：草稿首日带 _proposedRequirements 时解析为拟变更需求。"""
    if plans and isinstance(plans[0], dict) and isinstance(plans[0].get("_proposedRequirements"), dict):
        return TripRequirements.model_validate(plans[0]["_proposedRequirements"])
    return None


def _load_item_identity(session, itinerary_id: int) -> dict[int, ItineraryItem]:
    """M4：身份表含软删项——缩天再扩天时草稿会带回旧 id；身份校验放行，

    但恢复日不复活旧条目（_apply_plan_item 对软删项按新条目落地）。
    """
    return {
        item.id: item
        for item in session.execute(
            select(ItineraryItem)
            .execution_options(include_deleted=True)
            .where(ItineraryItem.itinerary_id == itinerary_id)
        )
        .scalars()
        .all()
    }


def _revive_or_create_days(
    session, main: ItineraryMain, itinerary_id: int, plans: list[dict[str, Any]], day_by_no: dict[int, ItineraryDay]
) -> None:
    for day_no in range(1, len(plans) + 1):
        if day_no in day_by_no:
            continue
        # M4 缩扩天（2→1→2）：软删行占着 uk_itinerary_day_no 键位——显式
        # include_deleted 找回并在**原行上复活**，不重复 INSERT；恢复日不复活
        # 旧条目（条目独立软删保持原状）也不复活旧生成动作。
        revived = session.execute(
            select(ItineraryDay)
            .where(ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.day_no == day_no)
            .execution_options(include_deleted=True)
            .limit(1)
        ).scalar_one_or_none()
        if revived is not None:
            revived.deleted = 0
            revived.generation_status = "PENDING"
            revived.generation_action_id = None
            revived.generation_fingerprint = None
            revived.generation_error = None
            revived.travel_date = None if main.start_date is None else main.start_date + timedelta(days=day_no - 1)
            day_by_no[day_no] = revived
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


def _apply_days(
    session,
    plans: list[dict[str, Any]],
    itinerary_id: int,
    day_by_no: dict[int, ItineraryDay],
    existing_items: dict[int, ItineraryItem],
    retained_item_ids: set[int],
) -> None:
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


def _sync_apply_metadata(main: ItineraryMain, plans: list[dict[str, Any]], day_by_no: dict[int, ItineraryDay]) -> None:
    if main.days != len(plans):
        main.days = len(plans)
        main.end_date = None if main.start_date is None else main.start_date + timedelta(days=len(plans) - 1)
        main.title = f"{main.city}{len(plans)}日游"
        # M4：住宿晚数语义——默认 N-1 可重算；用户明确 stay_nights_explicit 不擅动
        explicit_nights = False
        if main.requirements_json:
            lodging = TripRequirements.model_validate(main.requirements_json).lodging
            explicit_nights = bool(lodging and lodging.stay_nights_explicit)
        if not explicit_nights:
            main.stay_nights = max(len(plans) - 1, 0)

    # M4：内容变化后不能冒充完成——存在非 SUCCEEDED 天（复活日/新建日恒 PENDING）→ PARTIAL
    if main.gen_state == "COMPLETED" and any(day.generation_status != "SUCCEEDED" for day in day_by_no.values()):
        main.gen_state = "PARTIAL"


# ---------- 应用酒店房型 ----------


def apply_hotel_option(user_id: int, itinerary_id: int, request: HotelOptionRequest | None) -> dict[str, Any]:
    request = request or HotelOptionRequest()
    _validate_hotel_request(request)
    with session_scope() as session:
        main = itinerary_query.require_writable_main(session, user_id, itinerary_id)
        message = itinerary_chat.require_pending_action(
            session, user_id, itinerary_id, request.actionMessageId, request.baseRevision, True
        )
        itinerary_plan_apply_hotel.apply_hotel_option(session, main, message, request)
    itinerary_query.evict_detail(user_id, itinerary_id)
    return itinerary_query.detail(user_id, itinerary_id)


# ---------- 校验与元数据 ----------


def _validate_hotel_request(request: HotelOptionRequest) -> None:
    """对应 Java DTO 上的 Bean Validation（在进服务之前执行）。"""
    if not (request.hotelName or "").strip():
        raise ApiError(400, "酒店名称不能为空")
    if not (request.roomType or "").strip():
        raise ApiError(400, "请选择房型")
    if not (request.dayNos or []):
        raise ApiError(400, "请选择具体入住晚次")


def _validate_plans(plans: list[dict[str, Any]], *, requirements: TripRequirements | None = None) -> None:
    """草稿结构校验；M4：带拟变更需求时走确定性规则校验（validate_plans 的硬检查）。"""
    if not plans or len(plans) > MAX_TRIP_DAYS:
        raise ApiError(400, "行程草稿必须包含 1 到 7 个完整日期，未应用任何修改")
    valid_days = set(range(1, len(plans) + 1))
    seen_days: set[int] = set()
    for plan in plans:
        if not isinstance(plan, dict):
            raise ApiError(400, "行程草稿日期格式错误，未应用任何修改")
        day_no_raw = plan.get("day_no")
        if not _is_number(day_no_raw):
            raise ApiError(400, "行程草稿日期格式错误，未应用任何修改")
        day_no = int(day_no_raw)
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
    # M4（spec §9.1）：草稿按拟变更需求做确定性硬校验（必去/排除/窗口/冲突）
    if requirements is not None:
        for plan in plans:
            day_no = int(plan.get("day_no") or 0)
            policy = day_policy_for(requirements, day_no)
            report = check_constraints(plan, requirements, day_no, policy)
            if report.has_blocking:
                reasons = "；".join(check.reason for check in report.blocking_checks if check.reason)
                raise ApiError(400, f"草稿违反需求约束：{reasons}，未应用任何修改")


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
