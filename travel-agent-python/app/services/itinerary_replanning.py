"""P2 操作比较与选案；正式写入复用 apply-plans、CAS、版本与事务。"""

import json
import uuid

from sqlalchemy import select

from app.agent import (
    RunLimits,
    observe_run,
    parse_replan_change,
    run_constraint_replan,
    use_scene,
    use_task_limits,
    validate_replan_schedule,
)
from app.common import cache_store
from app.common.envelope import ApiError
from app.db.models import ItineraryDay
from app.db.session import session_scope
from app.schemas.replanning import ReplanBody, ReplanParseBody, ReplanSelectBody
from app.schemas.trip import MAX_TRIP_DAYS, GenerateRequest
from app.schemas.trip_requirements import TripRequirements
from app.services import itinerary_chat, itinerary_plan_apply, itinerary_query, llm_gateway_service, quota_service

_NAMESPACE = "replan-comparison"


def _main(session, user_id, itinerary_id, revision):
    main = itinerary_query.require_writable_main(session, user_id, itinerary_id)
    if main.planning_revision != revision or main.status != 2 or main.gen_state != "COMPLETED":
        raise ApiError(409, "行程正在变化或版本过期，请刷新后重新比较")
    req = TripRequirements.model_validate(main.requirements_json or {})
    if not req.constraint_planning or not 1 <= main.days <= MAX_TRIP_DAYS:
        raise ApiError(400, f"请先使用已确认目的地口径的 1–{MAX_TRIP_DAYS} 日约束规划")
    return main, req


def _snapshot(session, itinerary_id):
    plans = json.loads(itinerary_chat.canonical_json(itinerary_chat.current_plans(itinerary_id)))
    rows = itinerary_plan_apply._load_item_identity(session, itinerary_id)
    days = list(session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == itinerary_id)))
    metadata = {d.day_no: json.loads(d.metadata_json or "{}") for d in days}
    saved, completed = [], set()
    for day in plans:
        meta = metadata[day["day_no"]]
        report = meta.get("constraintPlanning") or {}
        facts = {f["candidateId"]: f for f in report.get("facts", [])}
        by_candidate = {rows[p["id"]].poi_id: p["id"] for p in day["items"] if rows[p["id"]].poi_id}
        # P1 候选用 cp-*，P2 的求解快照用持久 item ID；只按这两种显式身份关联，不能按同名猜。
        by_candidate.update({f"item:{p['id']}": p["id"] for p in day["items"]})
        completed.update(meta.get("executedItemIds") or [])
        for item in day["items"]:
            row = rows[item["id"]]
            fact = facts.get(f"item:{row.id}") or facts.get(row.poi_id, {})
            item.update(
                cost_currency=row.cost_currency,
                source=row.source,
                fact_evidence=json.loads(row.fact_evidence_json or "{}"),
                open_time=row.open_time,
                country_code=fact.get("countryCode"),
                aliases=fact.get("aliases", []),
                source_fetched_at=fact.get("retrievedAt"),
                source_language=fact.get("sourceLanguage"),
                source_url=fact.get("sourceUrl"),
                source_entity_id=fact.get("sourceEntityId"),
            )
        # 只有仍指向同一原地点 ID 的已保存路段可复用；改名/换地址会清除 poi_id。
        for route in report.get("routes", []):
            if route["fromId"] in by_candidate and route["toId"] in by_candidate:
                saved.append(
                    {
                        "fromItemId": by_candidate[route["fromId"]],
                        "toItemId": by_candidate[route["toId"]],
                        "route": {
                            "duration_min": route["durationMin"],
                            "source": route["source"],
                            "fetched_at": route.get("fetchedAt"),
                            "data_date": route.get("dataDate"),
                            "source_url": route.get("sourceUrl"),
                            "attribution": route.get("attribution"),
                            "mode": route["mode"],
                            "time_dependent": False,
                        },
                    }
                )
    return plans, saved, completed


def _validate_operation(plans, body):
    event = body.change
    ids = {p["id"] for d in plans for p in d["items"]}
    if len(ids) > 20 or not ids:
        raise ApiError(400, "本轮重规划支持 1–20 条已有安排")
    if (
        (event.item_id is not None and event.item_id not in ids)
        or (event.anchor_item_id is not None and event.anchor_item_id not in ids)
        or not (set(body.completed_item_ids) | set(body.optional_item_ids)) <= ids
    ):
        raise ApiError(400, "操作包含不属于当前行程的条目 ID")
    if event.anchor_item_id == event.item_id and event.item_id is not None:
        raise ApiError(400, "不能相对于自己移动")
    if event.day_no is not None and event.day_no not in {d["day_no"] for d in plans}:
        raise ApiError(400, "目标日期不存在")
    if any(
        not p.get("start_time") or not p.get("end_time") or p["start_time"] >= p["end_time"]
        for d in plans
        for p in d["items"]
    ):
        raise ApiError(400, "请先补齐所有条目的有效时段；本轮不猜测原时长")


def compare(user_id, itinerary_id, body: ReplanBody):
    with session_scope() as session:
        main, req = _main(session, user_id, itinerary_id, body.planning_revision)
        base = itinerary_chat.plan_revision(itinerary_chat.current_plans(itinerary_id))
        plans, routes, completed = _snapshot(session, itinerary_id)
        _validate_operation(plans, body)
        completed = sorted(completed | set(body.completed_item_ids))
        request = GenerateRequest(
            city=main.city,
            days=main.days,
            persons=main.persons or 1,
            budget=float(main.budget) if main.budget is not None else None,
            start_date=str(main.start_date) if main.start_date else None,
            requirements_struct=req,
        )
    assert req.constraint_planning is not None  # _main 已确认该目的地口径。
    evidence = req.constraint_planning.active_evidence
    if evidence:
        if evidence.allow_source_search:
            quota_service.enforce_llm_budget(user_id)
        else:
            quota_service.enforce_places_rate(user_id)
    limits = RunLimits(
        user_id=user_id,
        itinerary_id=itinerary_id,
        deadline_seconds=8,
        max_llm_calls=2 if evidence and evidence.allow_source_search else 0,
        max_retrievals=evidence.max_queries * 3 if evidence else 0,
    )
    with (
        llm_gateway_service.route_scope(user_id),
        use_scene("chat"),
        use_task_limits(limits),
        observe_run(request_id=f"replan-{itinerary_id}"),
    ):
        result, proposals = run_constraint_replan(
            request,
            plans,
            body.change,
            completed,
            routes,
            allow_cross_day=body.allow_cross_day,
            optional_item_ids=body.optional_item_ids,
        )
    with session_scope() as session:
        _main(session, user_id, itinerary_id, body.planning_revision)
        if itinerary_chat.plan_revision(itinerary_chat.current_plans(itinerary_id)) != base:
            raise ApiError(409, "比较期间行程已更新，请重新比较")
    result.planning_revision = body.planning_revision
    if proposals:
        # 派生方案临时缓存，不保存第二份正式行程；确认后只认数据库中的服务端草稿。
        token = uuid.uuid4().hex
        cache_store.set_json(
            _NAMESPACE,
            f"{user_id}:{itinerary_id}:{token}",
            {
                "revision": body.planning_revision,
                "base": base,
                "proposals": proposals,
                "titles": {o.key: o.title for o in result.options},
            },
            900,
        )
        result.comparison_id = token
    return result.model_dump(mode="json", by_alias=True)


def select_option(user_id, itinerary_id, body: ReplanSelectBody):
    key = f"{user_id}:{itinerary_id}:{body.comparison_id}"
    cached = cache_store.get_json(_NAMESPACE, key)
    if cached is None:
        raise ApiError(409, "比较结果已过期，请重新比较")
    with session_scope() as session:
        itinerary_chat.lock_main_for_draft(session, itinerary_id)
        _main(session, user_id, itinerary_id, body.planning_revision)
        if cached["revision"] != body.planning_revision or cached["base"] != itinerary_chat.plan_revision(
            itinerary_chat.current_plans(itinerary_id)
        ):
            raise ApiError(409, "行程已更新，该比较不能再选择")
        proposal = cached["proposals"].get(body.choice)
        if proposal is None:
            raise ApiError(400, "该候选不存在或未通过检查")
        plans = itinerary_chat.attach_base_revision(
            proposal, cached["base"], camel_case=False, planning_revision=body.planning_revision
        )
        plans[0]["_confirmationThread"] = f"replan-{body.comparison_id}"
        itinerary_chat.invalidate_in_session(session, user_id, itinerary_id)
        message = itinerary_chat.save_chat_message(
            session,
            user_id,
            itinerary_id,
            "ai",
            f"已选择「{cached['titles'][body.choice]}」。请核对差异，再确认保存；固定与已执行项不会自动放宽。",
            plans,
            [],
            True,
        )
    cache_store.delete(_NAMESPACE, key)
    return {"messageId": message.id, "baseRevision": cached["base"]}


def parse_intent(user_id, itinerary_id, body: ReplanParseBody):
    with session_scope() as session:
        _main(session, user_id, itinerary_id, body.planning_revision)
        plans, _, _ = _snapshot(session, itinerary_id)
    items = [{**p, "day_no": d["day_no"]} for d in plans for p in d["items"]]
    if body.item_id is not None and body.item_id not in {p["id"] for p in items}:
        raise ApiError(400, "所选条目不属于当前行程")
    return parse_replan_change(body.message, items, item_id=body.item_id, day_no=body.day_no).model_dump(
        mode="json", by_alias=True
    )


def validate_draft(plans):
    proof = plans[0].get("_replanProof") if plans else None
    if not proof:
        return
    matrix = {(e["from"], e["to"]): e["route"] for e in proof["matrix"]}
    errors = validate_replan_schedule(
        proof["before"],
        plans,
        proof["operation"],
        proof["protected"],
        matrix,
        proof.get("optionalItemIds", []),
        fact_updates=proof.get("factUpdates", []),
    )
    if errors:
        raise ApiError(409, "重规划草稿校验失败：" + "；".join(errors))
