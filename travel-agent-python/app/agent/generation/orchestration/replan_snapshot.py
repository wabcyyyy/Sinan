"""已保存条目与字段来源构造 P2 快照；数据库仍是业务真源。"""

from dataclasses import replace
from datetime import date, timedelta

from app.agent.data.route_service import RouteService
from app.agent.generation.content.constraint_facts import (
    align_requirement_names,
    planning_candidate,
    prepare_candidates,
)
from app.agent.generation.content.constraint_problem import DayPlanningProblem, minute
from app.agent.generation.content.constraint_replan_model import PreviousVisit
from app.common import clock
from app.schemas.trip_requirements import DayWindow, FixedArrangement


def effective_requirements(request, plans, event):
    req = request.requirements_struct.model_copy(deep=True)
    target = next((p for d in plans for p in d["items"] if p["id"] == event.item_id), None)
    if event.kind == "finish_by":
        previous = next((w for w in req.day_windows if w.day_no == event.day_no), None)
        finish = min(previous.finish_by or event.end_time, event.end_time) if previous else event.end_time
        req.day_windows = [w for w in req.day_windows if w.day_no != event.day_no]
        req.day_windows.append(
            DayWindow(
                day_no=event.day_no,
                kind="departure",
                not_before=previous.not_before if previous else None,
                finish_by=finish,
            )
        )
    if event.kind == "appointment":
        if target is None:
            raise ValueError("新增预约的目标不在原行程中")
        fixed = FixedArrangement(
            day_no=event.day_no,
            name=target["poi_name"],
            item_type=target["item_type"],
            start_time=event.start_time,
            end_time=event.end_time,
        )
        if fixed not in req.fixed_arrangements:
            req.fixed_arrangements.append(fixed)
    return req


def _prepared(plans, options, event):
    prepared, previous = [], []
    for day in plans:
        for order, raw in enumerate(day["items"]):
            start, end = minute(raw["start_time"]), minute(raw["end_time"])
            context = {
                "candidates": [
                    {**raw, "name": raw["poi_name"], "duration_min": end - start, "poi_id": f"item:{raw['id']}"}
                ]
            }
            candidate = prepare_candidates(context, options)[0]
            candidate.fact.candidate_id = f"item:{raw['id']}"
            candidate.item.item_type = raw["item_type"]
            candidate.item.poi_id = raw.get("poi_id")
            if raw.get("source_entity_id"):
                candidate.fact.source_entity_id = raw["source_entity_id"]
            if (
                event.kind in {"move", "appointment"}
                and event.end_time
                and event.start_time
                and event.item_id == raw["id"]
            ):
                duration = minute(event.end_time) - minute(event.start_time)
                if candidate.item.duration_min != duration:
                    candidate.item.fact_evidence.pop("duration_min", None)
                    candidate.gaps.append("时长来自本轮用户调整，未作为场馆事实核实")
                candidate.item.duration_min = duration
            prepared.append(candidate)
            previous.append(
                PreviousVisit(
                    raw["id"],
                    candidate.fact.candidate_id,
                    day["day_no"],
                    start,
                    end,
                    order,
                    raw["item_type"] == "hotel",
                )
            )
    return prepared, previous


def create_snapshot(request, plans, event, completed, saved_routes):
    options = request.requirements_struct.constraint_planning
    req = effective_requirements(request, plans, event)
    prepared, previous = _prepared(plans, options, event)
    protected = set(completed)
    for old, c in zip(previous, prepared, strict=True):
        if old.protected or any(
            f.day_no == old.day_no and f.name == c.item.poi_name and f.item_type == c.item.item_type
            for f in request.requirements_struct.fixed_arrangements
        ):
            protected.add(old.item_id)
    previous = [replace(v, protected=v.item_id in protected) for v in previous]
    points = [
        {"poi_id": c.fact.candidate_id, "latitude": c.item.latitude, "longitude": c.item.longitude} for c in prepared
    ]
    # P2 只复用已保存事实；新路段明确估算，不在交互修改中启动付费/在线取证。
    matrix = RouteService().matrix(points, mode=req.transport_preference or "walking")
    for edge in saved_routes:
        matrix[f"item:{edge['fromItemId']}", f"item:{edge['toItemId']}"] = edge["route"]
    start = date.fromisoformat(request.start_date) if request.start_date else clock.destination_today(options.time_zone)
    compiled_req = align_requirement_names(req, prepared)
    for fixed in compiled_req.fixed_arrangements:
        if fixed.cost is not None:
            fixed.cost *= request.persons
    problems = []
    for day in plans:
        candidates = []
        for c in prepared:
            candidate = planning_candidate(c, options, start + timedelta(days=day["day_no"] - 1), request.persons)
            if c.item.item_type == "hotel":
                candidate = replace(candidate, price_minor=None)  # 活动预算不伪装成含房费的整趟总额。
            if event.kind == "prefer" and c.fact.candidate_id == f"item:{event.item_id}":
                candidate = replace(candidate, utility=10)
            candidates.append(candidate)
        problems.append(
            DayPlanningProblem(compiled_req, tuple(candidates), matrix, options.currency, day_no=day["day_no"])
        )
    return problems, prepared, previous, protected, req, matrix, start


def neighborhoods(plans, change, requirements, allow_cross_day):
    ids = {p["id"] for d in plans for p in d["items"]}
    source = next((d for d in plans if any(p["id"] == change.item_id for p in d["items"])), None)
    days = {n for n in (source["day_no"] if source else None, change.day_no) if n is not None}
    local = set()
    dependencies = []
    for day in plans:
        if day["day_no"] not in days:
            continue
        rows = day["items"]
        indices = [i for i, p in enumerate(rows) if p["id"] in {change.item_id, change.anchor_item_id}]
        if not indices:
            indices = [0] if change.kind == "move" else list(range(len(rows)))
        for index in indices:
            segment = rows[max(0, index - 1) : min(len(rows), index + 2)]
            local.update(p["id"] for p in segment)
            dependencies.extend(f"activity:{p['id']} → route/window:day-{day['day_no']}" for p in segment)
    while True:
        names = {p["poi_name"] for d in plans for p in d["items"] if p["id"] in local}
        additions = set()
        for relation in requirements.place_relations:
            if names.intersection(relation.place_names):
                related = {p["id"] for d in plans for p in d["items"] if p["poi_name"] in relation.place_names}
                additions.update(related)
                dependencies.append(f"constraint:{relation.constraint_id} → activities:{sorted(related)}")
        if additions <= local:
            break
        local.update(additions)
    day_ids = {p["id"] for d in plans if d["day_no"] in days for p in d["items"]}
    scopes = [("local", local)]
    if day_ids != local:
        scopes.append(("day", day_ids))
    if allow_cross_day and len(plans) > 1:
        scopes.append(("trip", ids))
    return scopes, list(dict.fromkeys(dependencies))
