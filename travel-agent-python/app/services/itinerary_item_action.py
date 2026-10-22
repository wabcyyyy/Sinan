"""条目就地操作：同一事务内锁定、快照、校验、写入和失效草稿。"""

import json
from datetime import time

from sqlalchemy import select

from app.common.envelope import ApiError
from app.db.models import ItineraryDay, ItineraryItem
from app.db.session import session_scope
from app.schemas.business.scenario import ItemActionBody
from app.schemas.trip_requirements import FixedArrangement, TripRequirements, canonical_requirements_payload
from app.services import budget_engine, itinerary_chat, itinerary_plan_apply, itinerary_query, itinerary_version


def fixed_for_item(requirements: TripRequirements, day_no: int, item: ItineraryItem):
    return next(
        (
            f
            for f in requirements.fixed_arrangements
            if f.day_no == day_no and f.name == item.poi_name and f.item_type == item.item_type
        ),
        None,
    )


def _change_fixed(session, main, item, day, request, requirements):
    fixed = fixed_for_item(requirements, day.day_no, item)
    if request.action == "unfix":
        requirements.fixed_arrangements = [f for f in requirements.fixed_arrangements if f != fixed]
    elif request.action == "fix" and fixed is None:
        if not item.start_time or not item.end_time or item.end_time <= item.start_time:
            raise ApiError(400, "请先补齐有效起止时间，再固定安排")
        duplicates = session.scalars(
            select(ItineraryItem).where(
                ItineraryItem.day_id == day.id,
                ItineraryItem.poi_name == item.poi_name,
                ItineraryItem.item_type == item.item_type,
            )
        ).all()
        if len(duplicates) != 1:
            raise ApiError(400, "当天有同名同类条目，请先消除重复再固定")
        if len(requirements.fixed_arrangements) >= 40:
            raise ApiError(400, "最多固定 40 条安排")
        requirements.fixed_arrangements.append(
            FixedArrangement(
                day_no=day.day_no,
                name=item.poi_name,
                item_type=item.item_type,
                start_time=item.start_time.strftime("%H:%M"),
                end_time=item.end_time.strftime("%H:%M"),
            )
        )
    main.requirements_json = canonical_requirements_payload(requirements)


def _edit(session, item, days, request):
    if item.item_type == "hotel":
        raise ApiError(400, "住宿请使用对话中的住宿调整，避免改变房晚口径")
    if request.action == "delete":
        item.deleted = 1
        return
    target = next((d for d in days if d.day_no == request.dayNo), None)
    if target is None or not request.startTime or not request.endTime or request.startTime >= request.endTime:
        raise ApiError(400, "请选择日期及有效起止时间（暂不支持跨午夜）")
    item.day_id = target.id
    item.start_time = time.fromisoformat(request.startTime)
    item.end_time = time.fromisoformat(request.endTime)
    item.duration_min = (item.end_time.hour - item.start_time.hour) * 60 + item.end_time.minute - item.start_time.minute
    session.flush()
    for day in days:
        rows = session.scalars(select(ItineraryItem).where(ItineraryItem.day_id == day.id)).all()
        for index, row in enumerate(sorted(rows, key=lambda p: (p.start_time or time.max, p.sort_no or 0, p.id))):
            row.sort_no = index


def act(user_id: int, itinerary_id: int, item_id: int, request: ItemActionBody) -> dict:
    with session_scope() as session:
        itinerary_chat.lock_main_for_draft(session, itinerary_id)
        main = itinerary_query.require_writable_main(session, user_id, itinerary_id)
        if main.status != 2 or main.planning_revision != request.planningRevision:
            raise ApiError(409, "行程正在生成或已更新，请刷新后再操作")
        item = session.get(ItineraryItem, item_id)
        if item is None or item.itinerary_id != itinerary_id:
            raise ApiError(404, "行程项不存在")
        days = list(session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id)))
        day = next(d for d in days if d.id == item.day_id)
        requirements = TripRequirements.model_validate(main.requirements_json or {})
        if request.action in {"update", "delete"} and fixed_for_item(requirements, day.day_no, item):
            raise ApiError(409, "请先解除固定，再修改这条安排")
        before = itinerary_version.create_snapshot(user_id, itinerary_id, "item_action", "条目修改前")
        if request.action in {"fix", "unfix"}:
            _change_fixed(session, main, item, day, request, requirements)
        else:
            _edit(session, item, days, request)
            session.flush()
            plans = json.loads(itinerary_chat.canonical_json(itinerary_chat.current_plans(itinerary_id)))
            itinerary_plan_apply._validate_plans(plans, requirements=requirements)
            # 旧日路线元数据不能继续描述已变更的点位和时间。
            for changed in {day.id, item.day_id}:
                target = next(d for d in days if d.id == changed)
                metadata = json.loads(target.metadata_json or "{}")
                metadata.pop("miniRoute", None)
                target.metadata_json = json.dumps(metadata, ensure_ascii=False)
        budget_engine.recalculate(itinerary_id)
        itinerary_chat.invalidate_in_session(session, user_id, itinerary_id)
        itinerary_version.bump_planning_revision_with_cas(session, itinerary_id, request.planningRevision)
        itinerary_version.append_version_in_session(session, user_id, itinerary_id, "item_action", "条目修改完成")
    itinerary_query.evict_detail(user_id, itinerary_id)
    return {**itinerary_query.detail(user_id, itinerary_id), "undoVersionId": before["id"]}
