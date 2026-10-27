"""条目就地操作：同一事务内锁定、快照、校验、写入和失效草稿。"""

import json
from datetime import time

from sqlalchemy import select

from app.common.envelope import ApiError
from app.db.models import ItineraryDay, ItineraryItem
from app.db.session import session_scope
from app.schemas.business.scenario import ItemActionBody, ItemCreateBody
from app.schemas.trip_requirements import FixedArrangement, TripRequirements, canonical_requirements_payload
from app.services import (
    budget_engine,
    itinerary_chat,
    itinerary_item_move,
    itinerary_plan_apply,
    itinerary_query,
    itinerary_version,
    stay_hotels,
)


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


def _edit(session, item, days, request, currency):
    if request.action == "delete":
        item.deleted = 1
        return
    target = next((d for d in days if d.day_no == request.dayNo), None)
    if target is None or not request.startTime or not request.endTime or request.startTime >= request.endTime:
        raise ApiError(400, "请选择日期及有效起止时间（暂不支持跨午夜）")
    for field, column in (("poiName", "poi_name"), ("address", "address"), ("cost", "cost"), ("remark", "remark")):
        if field not in request.model_fields_set:
            continue
        value = getattr(request, field)
        if field == "poiName" and (not value or not value.strip()):
            raise ApiError(400, "地点名称不能为空")
        if field in {"poiName", "address"} and value != getattr(item, column):
            # 用户换了地点或地址，旧地点的证据、坐标和图片不能继续背书。
            for attr in (
                "poi_id",
                "latitude",
                "longitude",
                "image_url",
                "open_time",
                "source_updated_at",
                "why_note",
                "intro",
                "tag",
            ):
                setattr(item, attr, None)
            item.fact_evidence_json = "{}"
            item.source = "user"
            item.verification_status = "unverified"
            item.value_kind = "estimated"
            item.freshness_status = "unknown"
            item.review_requirement = "before_departure"
        if field == "cost" and value != item.cost:
            evidence = json.loads(item.fact_evidence_json or "{}")
            evidence.pop("cost", None)
            item.fact_evidence_json = json.dumps(evidence, ensure_ascii=False)
            item.cost_currency = currency
        setattr(item, column, value.strip() if isinstance(value, str) else value)
    original_day_id = item.day_id
    item.day_id = target.id
    item.start_time = time.fromisoformat(request.startTime)
    item.end_time = time.fromisoformat(request.endTime)
    item.duration_min = (item.end_time.hour - item.start_time.hour) * 60 + item.end_time.minute - item.start_time.minute
    session.flush()
    for day in days:
        if day.id not in {original_day_id, target.id}:
            continue
        rows = session.scalars(select(ItineraryItem).where(ItineraryItem.day_id == day.id)).all()
        for index, row in enumerate(sorted(rows, key=lambda p: (p.start_time or time.max, p.sort_no or 0, p.id))):
            row.sort_no = index


def _action_item(
    session, itinerary_id: int, item_id: int | None, request: ItemActionBody | ItemCreateBody, days: list[ItineraryDay]
) -> tuple[ItineraryItem, ItineraryDay]:
    if isinstance(request, ItemCreateBody):
        day = next((d for d in days if d.day_no == request.dayNo), None)
        if day is None:
            raise ApiError(400, "所选日期不存在")
        item = ItineraryItem(
            itinerary_id=itinerary_id,
            day_id=day.id,
            item_type=request.itemType,
            poi_name=request.poiName,
            source="user",
            verification_status="unverified",
            value_kind="estimated",
            review_requirement="before_departure",
            latitude=request.latitude,
            longitude=request.longitude,
            fact_evidence_json=json.dumps(
                {
                    "coordinates": {
                        "sourceRef": "user_map_wgs84",
                        "provider": "user",
                        "verificationStatus": "unverified",
                        "valueKind": "estimated",
                        "freshnessStatus": "unknown",
                        "reviewRequirement": "before_departure",
                    }
                }
            )
            if request.latitude is not None
            else "{}",
        )
    else:
        item = session.get(ItineraryItem, item_id)
        if item is None or item.itinerary_id != itinerary_id:
            raise ApiError(404, "行程项不存在")
        day = next(d for d in days if d.id == item.day_id)
    return item, day


def act(user_id: int, itinerary_id: int, item_id: int | None, request: ItemActionBody | ItemCreateBody) -> dict:
    with session_scope() as session:
        itinerary_chat.lock_main_for_draft(session, itinerary_id)
        main = itinerary_query.require_writable_main(session, user_id, itinerary_id)
        if main.status != 2 or main.planning_revision != request.planningRevision:
            raise ApiError(409, "行程正在生成或已更新，请刷新后再操作")
        days = list(session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id)))
        item, day = _action_item(session, itinerary_id, item_id, request, days)
        requirements = TripRequirements.model_validate(main.requirements_json or {})
        temporal_edit = request.action == "update" and (
            request.dayNo != day.day_no
            or request.startTime != (item.start_time.strftime("%H:%M") if item.start_time else None)
            or request.endTime != (item.end_time.strftime("%H:%M") if item.end_time else None)
        )
        if requirements.constraint_planning and (request.action in {"move", "delete"} or temporal_edit):
            raise ApiError(409, "约束行程的时间调整、移动或移除，请先比较方案再确认保存")
        if request.action in {"update", "delete", "move"} and fixed_for_item(requirements, day.day_no, item):
            raise ApiError(409, "请先解除固定，再修改这条安排")
        if item.item_type == "hotel" and request.action not in {"fix", "unfix", "delete"}:
            nights = main.stay_nights if main.stay_nights is not None else max((main.days or 1) - 1, 0)
            if not request.dayNo or request.dayNo > nights:
                raise ApiError(400, "住宿只能安排在本行程需要住宿的晚次")
            if session.scalar(
                select(ItineraryItem.id)
                .join(ItineraryDay, ItineraryDay.id == ItineraryItem.day_id)
                .where(
                    ItineraryItem.itinerary_id == itinerary_id,
                    ItineraryDay.day_no == request.dayNo,
                    ItineraryItem.item_type == "hotel",
                    ItineraryItem.id != (item.id or 0),
                )
            ):
                raise ApiError(400, "该晚已有住宿，请编辑原住宿或先移除它")
        before = itinerary_version.create_snapshot(user_id, itinerary_id, "item_action", "条目修改前")
        if request.action in {"fix", "unfix"}:
            _change_fixed(session, main, item, day, request, requirements)
        else:
            previous_plans = itinerary_chat.current_plans(itinerary_id)
            if isinstance(request, ItemCreateBody):
                session.add(item)
            if request.action == "move":
                itinerary_item_move.move(session, item, day, days, request, requirements)
            else:
                _edit(
                    session,
                    item,
                    days,
                    request,
                    requirements.constraint_planning.currency if requirements.constraint_planning else "CNY",
                )
            if isinstance(request, ItemCreateBody) and request.fixed:
                _change_fixed(
                    session,
                    main,
                    item,
                    day,
                    ItemActionBody(action="fix", planningRevision=request.planningRevision),
                    requirements,
                )
            session.flush()
            plans = json.loads(itinerary_chat.canonical_json(itinerary_chat.current_plans(itinerary_id)))
            itinerary_plan_apply._validate_plans(plans, requirements=requirements)
            # 旧日路线元数据不能继续描述已变更的点位和时间。
            for changed in {day.id, item.day_id}:
                target = next(d for d in days if d.id == changed)
                metadata = json.loads(target.metadata_json or "{}")
                metadata.pop("miniRoute", None)
                target.metadata_json = json.dumps(metadata, ensure_ascii=False)
                changed_plan = next(p for p in plans if p["day_no"] == target.day_no)
                stay_hotels.sync_day_summary(
                    target, changed_plan, next(p for p in previous_plans if p["day_no"] == target.day_no)
                )
        budget_engine.recalculate(itinerary_id)
        itinerary_chat.invalidate_in_session(session, user_id, itinerary_id)
        itinerary_version.bump_planning_revision_with_cas(session, itinerary_id, request.planningRevision)
        itinerary_version.append_version_in_session(session, user_id, itinerary_id, "item_action", "条目修改完成")
    itinerary_query.evict_detail(user_id, itinerary_id)
    return {**itinerary_query.detail(user_id, itinerary_id), "undoVersionId": before["id"]}
