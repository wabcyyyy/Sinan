"""plan_days 的约束分支：复用日锁、动作指纹、持久化、事件及预算重算。"""

import json
import logging
from uuid import uuid4

from sqlalchemy import select

from app.agent import check_task_write, run_constraint_planning
from app.common.envelope import ApiError
from app.db.models import ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.schemas.planning import ConstraintPlanningAssessment
from app.schemas.replanning import ConstraintRepairBody, ConstraintRepairVO
from app.schemas.trip import DailyPlan
from app.schemas.trip_requirements import RequirementSource, TripRequirements
from app.services import constraint_context, day_persistence, generation_events, generation_gate, itinerary_query

logger = logging.getLogger(__name__)


def revise_requirements(user_id: int, itinerary_id: int, body: ConstraintRepairBody) -> ConstraintRepairVO:
    """用户选中服务端冲突建议后返回待确认需求；不写原行程、不重开费用窗口。"""
    with session_scope() as session:
        main = itinerary_query.require_owned_main(session, user_id, itinerary_id)
        if main.planning_revision != body.planning_revision or main.status == 1:
            raise ApiError(409, "行程版本已变化，请重新核对冲突")
        req = TripRequirements.model_validate(main.requirements_json or {})
        reports = [
            ConstraintPlanningAssessment.model_validate(raw)
            for day in session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id))
            if (raw := json.loads(day.metadata_json or "{}").get("constraintPlanning"))
        ]
        selected = next(
            (
                s
                for r in reports
                if r.status == "INFEASIBLE" and r.planning_revision == body.planning_revision
                for s in r.repair_suggestions
                if s.constraint_id == body.constraint_id and s.action == body.action
            ),
            None,
        )
        if selected is None:
            raise ApiError(409, "该修订不属于当前冲突建议，请重新规划")
        if body.action == "soften_relation":
            relation = next((r for r in req.place_relations if r.constraint_id == body.constraint_id), None)
            if relation is None or relation.strength != "hard":
                raise ApiError(409, "关系要求已变化")
            relation.strength = "soft"
        else:
            place = next((p for p in req.required_places if p.constraint_id == body.constraint_id), None)
            if place is None or any(f.name == place.name for f in req.fixed_arrangements):
                raise ApiError(409, "固定预约不能通过取消必去来解除")
            req.required_places.remove(place)
        req.requirement_sources.append(
            RequirementSource(
                source_text=f"用户确认修订：{selected.label}", target="confirmed_repair", disposition="mapped"
            )
        )
        req.requirement_sources = req.requirement_sources[-60:]
    return ConstraintRepairVO(
        requirements=req,
        base_revision=body.planning_revision,
        notice="修订已进入待确认需求；原行程未改写，确认后请重新开始规划",
    )


def _retained_days(itinerary_id):
    with session_scope() as session:
        days = session.scalars(
            select(ItineraryDay).where(
                ItineraryDay.itinerary_id == itinerary_id, ItineraryDay.generation_status == "SUCCEEDED"
            )
        ).all()
        return [
            DailyPlan.model_validate(
                {
                    "dayNo": day.day_no,
                    "items": [
                        itinerary_query.item_vo(item, {})
                        for item in session.scalars(
                            select(ItineraryItem).where(ItineraryItem.day_id == day.id).order_by(ItineraryItem.sort_no)
                        )
                    ],
                }
            )
            for day in days
        ]


def _stamp_revision(itinerary_id, written):
    check_task_write()
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            raise ApiError(404, "行程不存在")
        for day_id in written:
            day = session.get(ItineraryDay, day_id)
            if day is None:
                raise ApiError(404, "已保存的行程日不存在")
            metadata = json.loads(day.metadata_json or "{}")
            if metadata.get("constraintPlanning"):
                metadata["constraintPlanning"]["planningRevision"] = main.planning_revision
                day.metadata_json = json.dumps(metadata, ensure_ascii=False)
        check_task_write()


def plan_trip(user_id, itinerary_id, command, context, fingerprint, timing):
    run_id = f"constraint-{uuid4().hex}"
    previews = []
    settled = set()
    written = []

    def _resolve_candidates(*, final=False, failure=False):
        itinerary_query.evict_detail(user_id, itinerary_id)
        detail = itinerary_query.detail(user_id, itinerary_id)
        # 进度流可被协作者订阅；只广播公共排程投影，不带 owner 的需求、分享令牌或角色。
        snapshot = {key: detail[key] for key in ("id", "planningRevision", "dayList")}
        with session_scope() as session:
            rows = session.scalars(
                select(ItineraryItem).where(
                    ItineraryItem.itinerary_id == itinerary_id, ItineraryItem.day_id.in_(written)
                )
            ).all()
            by_candidate = {item.poi_id: item for item in rows if item.poi_id}
            mappings, withdrawn = [], []
            for ordinal, candidate_id in previews:
                if ordinal in settled:
                    continue
                item = by_candidate.get(candidate_id)
                if item is not None:
                    mappings.append(
                        {
                            "previewId": f"{run_id}:0:{ordinal}",
                            "candidateId": candidate_id,
                            "itemId": item.id,
                            "poiName": item.poi_name,
                        }
                    )
                elif final:
                    withdrawn.append(
                        {
                            "previewId": f"{run_id}:0:{ordinal}",
                            "candidateId": candidate_id,
                            "reason": "未完成保存" if failure else "未产生新的安排，以已保存行程为准",
                        }
                    )
                else:
                    continue
                settled.add(ordinal)
        if mappings or withdrawn:
            generation_events.day_preview_mapping(
                itinerary_id, run_id, 0, mappings, snapshot=snapshot, withdrawn=withdrawn
            )

    def resolve_candidates(*, final=False, failure=False):
        check_task_write()
        try:
            _resolve_candidates(final=final, failure=failure)
        except Exception:
            # 预览读回与事件沿用尽力交付；不可把已经提交的行程误判为规划失败。
            logger.exception("candidate reconciliation unavailable: itinerary=%s", itinerary_id)
            generation_events.degraded(itinerary_id, "progress", "候选接续暂不可用", "以行程详情为准")

    def show_candidates(items):
        for ordinal, item in enumerate(items, 1):
            # dayNo=0 是未分配日期的研究池；不携带时间、费用或正式条目ID。
            previews.append((ordinal, item.poi_id))
            generation_events.item_preview(
                itinerary_id,
                run_id,
                0,
                ordinal,
                {"poi_name": item.poi_name, "item_type": item.item_type, "candidate_id": item.poi_id},
            )
            timing.mark("first_candidate")

    try:
        plans = run_constraint_planning(
            command.to_generate_request(),
            context,
            stay_nights=command.stay_nights,
            frozen_days=_retained_days(itinerary_id),
            on_candidates=show_candidates,
            on_snapshot=lambda snapshot: constraint_context.save_context(user_id, itinerary_id, fingerprint, snapshot),
        )
    except BaseException:
        for ordinal, _ in previews:
            generation_events.item_preview_withdrawn(itinerary_id, run_id, 0, ordinal, "求解失败，候选未保存")
        raise
    for plan in plans:
        day_no = plan.day_no
        action_id = f"day-{itinerary_id}-{day_no}"
        if not generation_gate.try_day_lock(itinerary_id, day_no):
            raise ApiError(409, "约束规划写入时日锁被占用，请稍后重试")
        try:
            day = day_persistence.find_day(itinerary_id, day_no)
            if day is None:
                raise ApiError(404, "行程日不存在")
            if day.generation_status == "SUCCEEDED":
                generation_gate.verify_action(day, action_id, fingerprint)
                continue
            day_persistence.mark_running(day.id, action_id, fingerprint)
            generation_events.day_start(itinerary_id, day_no)
            day_persistence.persist(itinerary_id, command, day_no, plan, action_id, fingerprint)
            written.append(day.id)
            resolve_candidates()
            timing.mark("first_day")
            generation_events.day_done(itinerary_id, day_no, plan.theme, len(plan.items), plan.note)
            itinerary_query.evict_detail(user_id, itinerary_id)
        except Exception:
            resolve_candidates(final=True, failure=True)
            raise
        finally:
            generation_gate.release_day_lock(itinerary_id, day_no)
    _stamp_revision(itinerary_id, written)
    resolve_candidates(final=True)
