"""确定性双方案：保留顺序与时长，只重排当前条目；不查询或编造旅行事实。"""

import copy
import json
from typing import Literal

from app.agent import parse_scenario_intent, use_scene
from app.common.envelope import ApiError
from app.db.session import session_scope
from app.schemas.business.scenario import (
    ScenarioBody,
    ScenarioCompareVO,
    ScenarioItemVO,
    ScenarioOptionVO,
    ScenarioParseBody,
    ScenarioSelectBody,
)
from app.schemas.trip_requirements import DayWindow, TripRequirements, canonical_requirements_payload
from app.services import (
    itinerary_chat,
    itinerary_city,
    itinerary_plan_apply,
    itinerary_query,
    llm_gateway_service,
    quota_service,
)


def parse_intent(user_id: int, itinerary_id: int, request: ScenarioParseBody) -> dict:
    main = itinerary_query.find_owned_main(user_id, itinerary_id)
    if main.planning_revision != request.planningRevision or main.status != 2:
        raise ApiError(409, "行程已变化或尚未完成，请刷新后重试")
    plans = itinerary_chat.current_plans(itinerary_id)
    day = next((d for d in plans if d["day_no"] == request.dayNo), None)
    if day is None:
        raise ApiError(400, "当前日期不存在")
    items = [{k: p.get(k) for k in ("id", "poi_name", "start_time", "end_time")} for p in day["items"]]
    quota_service.enforce_llm_budget(user_id)
    with llm_gateway_service.route_scope(user_id), use_scene("chat"):
        result = itinerary_city.guard_agent_call(
            "比较条件理解失败，请重试或手动填写", lambda: parse_scenario_intent(request.message, request.dayNo, items)
        )
    current = itinerary_query.find_owned_main(user_id, itinerary_id)
    if current.planning_revision != request.planningRevision:
        raise ApiError(409, "理解期间行程已变化，请刷新后重试")
    fixed = TripRequirements.model_validate(current.requirements_json or {}).fixed_arrangements
    protected = {
        p["id"]
        for p in day["items"]
        if any(f.day_no == request.dayNo and f.name == p["poi_name"] and f.item_type == p["item_type"] for f in fixed)
    }
    if protected.intersection(result.optionalItemIds):
        result.questions = ["你想舍弃的条目已有持久固定，请先在条目上解除固定再比较。"]
    return result.model_dump()


def _minutes(value: str) -> int:
    hour, minute = value.split(":")[:2]
    return int(hour) * 60 + int(minute)


def _clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def _schedule(items: list[dict], request: ScenarioBody, omit: bool) -> tuple[list[dict], list[str]]:
    result, conflicts = [], []
    cursor = _minutes(request.startTime)
    for source in items:
        if omit and source["id"] in request.optionalItemIds:
            continue
        item = copy.deepcopy(source)
        if item["item_type"] == "hotel":
            result.append(item)  # 住宿原样保留，不当作白天游玩的时段。
            continue
        start, end = item.get("start_time"), item.get("end_time")
        if not start or not end or _minutes(end) <= _minutes(start):
            conflicts.append(f"「{item['poi_name']}」缺少有效起止时间，请先补齐；未猜测游玩时长")
            result.append(item)
            continue
        duration = _minutes(end) - _minutes(start)
        if item["id"] in request.fixedItemIds:
            if cursor > _minutes(start):
                conflicts.append(
                    f"固定安排「{item['poi_name']}」{start} 开始，前序安排及预留时间最早到 {_clock(cursor)}"
                )
            cursor = max(cursor, _minutes(end))
        else:
            if cursor + duration >= 24 * 60:
                conflicts.append(f"「{item['poi_name']}」将跨日，当前比较不支持跨日排程")
            else:
                item.update(start_time=_clock(cursor), end_time=_clock(cursor + duration), duration_min=duration)
            cursor += duration
        result.append(item)
        cursor += request.transferMinutes
    return result, conflicts


def _prepare(user_id: int, itinerary_id: int, request: ScenarioBody):
    with session_scope() as session:
        main = itinerary_query.require_writable_main(session, user_id, itinerary_id)
        if main.status == 1 or main.gen_state in {"GENERATING", "PARTIAL", "FAILED"}:
            raise ApiError(409, "请先完成当前行程，再比较调整方案")
        if main.planning_revision != request.planningRevision:
            raise ApiError(409, "行程已更新，请刷新后重新比较")
        plans = itinerary_chat.current_plans(itinerary_id)
        original_revision = itinerary_chat.plan_revision(plans)
        plans = json.loads(itinerary_chat.canonical_json(plans))
        day = next((day for day in plans if day["day_no"] == request.dayNo), None)
        if day is None or not day["items"]:
            raise ApiError(400, "这一天没有可比较的安排")
        items = day["items"]
        ids = {item["id"] for item in items}
        fixed, optional = set(request.fixedItemIds), set(request.optionalItemIds)
        if not (fixed | optional) <= ids or fixed & optional:
            raise ApiError(400, "固定项与可舍弃项必须属于当前日期，且不能重叠")
        if any(item["item_type"] == "hotel" and item["id"] in optional for item in items):
            raise ApiError(400, "住宿在本次比较中始终保留")
        if _minutes(request.finishBy) - request.departureBufferMinutes <= _minutes(request.startTime):
            raise ApiError(400, "扣除返程预留后没有可用时间，请调整时间窗口")
        requirements = TripRequirements.model_validate(main.requirements_json or {})
        persistent_ids = {
            p["id"]
            for p in items
            if any(
                f.day_no == request.dayNo and f.name == p["poi_name"] and f.item_type == p["item_type"]
                for f in requirements.fixed_arrangements
            )
        }
        if persistent_ids & optional:
            raise ApiError(400, "固定安排不能舍弃，请先在条目上解除固定")
        request = request.model_copy(update={"fixedItemIds": list(fixed | persistent_ids)})
        previous = next((w for w in requirements.day_windows if w.day_no == request.dayNo), None)
        # 已有要求不能被比较入口悄悄放宽。
        start = max(request.startTime, previous.not_before or "00:00") if previous else request.startTime
        finish = _clock(_minutes(request.finishBy) - request.departureBufferMinutes)
        if previous and previous.finish_by:
            finish = min(finish, previous.finish_by)
        requirements.day_windows = [w for w in requirements.day_windows if w.day_no != request.dayNo]
        requirements.day_windows.append(DayWindow(day_no=request.dayNo, not_before=start, finish_by=finish))
        scoped = request.model_copy(update={"startTime": start})
        currencies = {
            row.id: row.cost_currency
            for row in itinerary_plan_apply._load_item_identity(session, itinerary_id).values()
        }
        options, proposals = _options(plans, scoped, requirements, finish, currencies, main.persons)
        return (
            ScenarioCompareVO(
                planningRevision=main.planning_revision,
                options=options,
                assumptions=[
                    f"沿用现有顺序和游玩时长；每次转场预留 {request.transferMinutes} 分钟，由你提供，不是地图测算。",
                    f"返程额外预留 {request.departureBufferMinutes} 分钟；保留其他日期和全部住宿。",
                    "表单新增固定仅约束本次比较；条目上已有固定持续保留。固定不代表预订；营业和预约仍需核实。",
                ],
            ),
            proposals,
            original_revision,
        )


def _options(
    plans: list[dict],
    request: ScenarioBody,
    requirements: TripRequirements,
    finish: str,
    currencies: dict,
    persons: int,
):
    items = next(day for day in plans if day["day_no"] == request.dayNo)["items"]
    fixed, optional = set(request.fixedItemIds), set(request.optionalItemIds)
    options, proposals = [], {}
    choices: tuple[tuple[Literal["keep", "omit"], str], ...] = (("keep", "保留全部安排"), ("omit", "移除可选安排"))
    for key, title in choices:
        arranged, conflicts = _schedule(items, request, key == "omit")
        remaining = [item for item in arranged if item["item_type"] != "hotel"]
        ends = [_minutes(item["end_time"]) for item in remaining if item.get("end_time")]
        last = max(ends) if ends else None
        slack = _minutes(finish) - last if last is not None else None
        if not remaining:
            conflicts.append("移除后没有游玩或用餐安排，请至少保留一项")
        if last is not None and slack is not None and slack < 0:
            conflicts.append(f"最晚 {_clock(last)} 结束，比可用截止 {finish} 晚 {-slack} 分钟")
        if key == "omit" and not optional:
            conflicts.append("尚未选择可舍弃项；请选择后再比较")
        proposed = copy.deepcopy(plans)
        selected = next(day for day in proposed if day["day_no"] == request.dayNo)
        selected["items"] = arranged
        selected["note"] = "按已列安排与用户预留时间调整；营业、预约和真实交通仍需核实。"
        proposed[0]["_proposedRequirements"] = canonical_requirements_payload(requirements)
        try:
            itinerary_plan_apply._validate_plans(proposed, requirements=requirements)
        except ApiError as exc:
            conflicts.append(str(exc.message))
        removed = [item for item in items if item["id"] not in {p["id"] for p in arranged}]
        # 外币与缺价不冒充节省金额；住宿未变，普通条目按人计。
        cost_delta = (
            None
            if any(p.get("cost") is None or currencies.get(p["id"]) not in {None, "CNY"} for p in removed)
            else -sum(float(p["cost"]) for p in removed) * persons
        )
        options.append(_option_view(key, title, conflicts, arranged, removed, fixed, last, slack, cost_delta))
        proposals[key] = proposed
    return options, proposals


def _option_view(
    key: Literal["keep", "omit"],
    title: str,
    conflicts: list[str],
    arranged: list[dict],
    removed: list[dict],
    fixed: set[int],
    last: int | None,
    slack: int | None,
    cost_delta: float | None,
):
    return ScenarioOptionVO(
        key=key,
        title=title,
        feasible=not conflicts,
        conflicts=list(dict.fromkeys(conflicts)),
        items=[
            ScenarioItemVO(
                id=p["id"],
                name=p["poi_name"],
                startTime=p.get("start_time"),
                endTime=p.get("end_time"),
                fixed=p["id"] in fixed or p["item_type"] == "hotel",
            )
            for p in arranged
        ],
        removed=[p["poi_name"] for p in removed],
        finishTime=_clock(last) if last is not None else None,
        slackMinutes=slack,
        costDelta=cost_delta,
    )


def compare(user_id: int, itinerary_id: int, request: ScenarioBody) -> dict:
    return _prepare(user_id, itinerary_id, request)[0].model_dump()


def select_option(user_id: int, itinerary_id: int, request: ScenarioSelectBody) -> dict:
    with session_scope() as session:
        itinerary_chat.lock_main_for_draft(session, itinerary_id)
        result, proposals, revision = _prepare(user_id, itinerary_id, request)
        option = next(option for option in result.options if option.key == request.choice)
        if not option.feasible:
            raise ApiError(400, "该方案存在冲突，未生成可应用草稿")
        plans = itinerary_chat.attach_base_revision(
            proposals[request.choice], revision, camel_case=False, planning_revision=request.planningRevision
        )
        # 独立的确定性草稿，不恢复另一条 AI 对话中可能遗留的确认检查点。
        plans[0]["_confirmationThread"] = f"scenario-{itinerary_id}-{request.planningRevision}"
        itinerary_chat.invalidate_in_session(session, user_id, itinerary_id)
        message = itinerary_chat.save_chat_message(
            session,
            user_id,
            itinerary_id,
            "ai",
            f"已选择「{option.title}」，请核对差异后确认。本次使用现有安排计算，未调用模型。",
            plans,
            [],
            True,
        )
        return {"messageId": message.id, "baseRevision": revision}
