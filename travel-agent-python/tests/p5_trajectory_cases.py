"""受控 P5 的已确认表单、冻结资料和直接数据库 checker；不读 CP 编译变量。"""

import json

from app.agent.generation.content.constraint_replan_check import item_positions, minute, validate_replan_schedule
from app.agent.generation.orchestration.replan_snapshot import effective_requirements
from app.agent.generation.rules.day_policy import day_policy_for
from app.db.models import ItineraryDay, ItineraryItem
from app.db.session import session_scope
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import GenerateRequest
from app.schemas.trip_requirements import BudgetPolicy, DayWindow, FixedArrangement, PlaceRelation, TripRequirements


def setup(profile, intake):
    req = TripRequirements.model_validate(intake["state"]["requirements"])
    # 确认表单是单独一步：任何 NL 漏项在报告中列出，不冒充模型正确。
    req.constraint_planning = ConstraintPlanningOptions(country_code="FR", time_zone="Europe/Paris", currency="EUR")
    req.budget_policy = BudgetPolicy(mode="hard_cap")
    req.max_attractions_per_day = profile["max_places"]
    req.day_windows = [
        DayWindow(day_no=d, kind="full", not_before="09:00", finish_by="18:00") for d in range(1, profile["days"] + 1)
    ]
    if profile["fixed"]:
        req.fixed_arrangements = [
            FixedArrangement(
                day_no=1,
                name="地点0",
                item_type="attraction",
                start_time="09:00" if profile["days"] == 2 else "10:00",
                end_time="10:30" if profile["days"] == 2 else "11:00",
            )
        ]
    if profile["days"] == 2:
        req.place_relations = [
            PlaceRelation(constraint_id="p5-before", kind="before", place_names=["地点4", "地点5"], day_no=2)
        ]
    rows = []
    for i in range(4 if profile["days"] == 1 else 6):
        fact = {
            "provider": "p5-controlled-fixture",
            "source_url": "https://example.com/p5-frozen",
            "retrieved_at": "2026-10-24T08:00:00Z",
            "verification_status": "partially_verified",
            "value_kind": "observed",
        }
        rows.append(
            {
                "name": f"地点{i}",
                "id": f"p5:{i}",
                "source": "p5-controlled-fixture",
                "country_code": "FR",
                "duration_min": profile["duration"],
                "cost": 10,
                "cost_currency": "EUR",
                "fact_evidence": {
                    "cost": fact,
                    "duration_min": fact,
                    "open_time": {
                        **fact,
                        "opening_hours": {
                            "status": "known",
                            "rules": [
                                {"intervals": [{"start": "09:00", "end": "18:00"}], "source_text": "合成冻结窗口"}
                            ],
                        },
                    },
                },
            }
        )
    return req, {"candidates": rows}


def frozen_matrix(points, **_kwargs):
    return {
        (a["poi_id"], b["poi_id"]): {
            "duration_min": 10,
            "source": "frozen-matrix",
            "attribution": "自制冻结矩阵，不是实际路网",
            "mode": "walking",
        }
        for a in points
        for b in points
        if a != b
    }


def _hhmm(value):
    assert value is not None, "保存后的条目缺少时刻"
    return value.strftime("%H:%M")


def read_database(identifier):
    plans = []
    with session_scope() as session:
        days = (
            session.query(ItineraryDay)
            .filter_by(itinerary_id=identifier, deleted=0)
            .order_by(ItineraryDay.day_no)
            .all()
        )
        for day in days:
            items = (
                session.query(ItineraryItem).filter_by(day_id=day.id, deleted=0).order_by(ItineraryItem.sort_no).all()
            )
            plans.append(
                {
                    "day_no": day.day_no,
                    "items": [
                        {
                            "id": p.id,
                            "poi_name": p.poi_name,
                            "item_type": p.item_type,
                            "start_time": _hhmm(p.start_time),
                            "end_time": _hhmm(p.end_time),
                            "duration_min": p.duration_min,
                            "cost": float(p.cost) if p.cost is not None else None,
                            "cost_currency": p.cost_currency,
                            "fact_evidence": json.loads(p.fact_evidence_json or "{}"),
                        }
                        for p in items
                    ],
                }
            )
    return plans


def check_database(before, after, event, requirements, profile):
    positions = item_positions(before)
    protected = [
        key
        for key, (day, _, p) in positions.items()
        if any(f.day_no == day and f.name == p["poi_name"] for f in requirements.fixed_arrangements)
    ]
    matrix = {(f"item:{a}", f"item:{b}"): {"duration_min": 10} for a in positions for b in positions if a != b}
    errors = validate_replan_schedule(before, after, event, protected, matrix)
    request = GenerateRequest(
        city="Paris",
        days=profile["days"],
        persons=1,
        budget=1000,
        start_date="2026-11-03",
        requirements_struct=requirements,
    )
    req = effective_requirements(request, before, event)
    names = {p["poi_name"]: (d["day_no"], i, p) for d in after for i, p in enumerate(d["items"])}
    for day in after:
        policy = day_policy_for(req, day["day_no"])
        if sum(minute(p["end_time"]) - minute(p["start_time"]) for p in day["items"]) < policy.min_active_minutes:
            errors.append("最终数据库不满足交付时长")
        if len(day["items"]) > policy.max_attractions:
            errors.append("最终数据库超出每日上限")
        for item in day["items"]:
            if minute(item["start_time"]) < (policy.window_start_min or 540) or minute(item["end_time"]) > (
                policy.window_end_min or 1080
            ):
                errors.append("最终数据库违反日窗")
            if item["cost_currency"] != "EUR" or item["cost"] != 10:
                errors.append("未授权改价或币种")
    if sum(p["cost"] for d in after for p in d["items"]) > 1000:
        errors.append("超出硬预算")
    for relation in req.place_relations:
        found = [names.get(n) for n in relation.place_names]
        if any(x is None for x in found):
            errors.append("旧先后约束被覆盖")
            continue
        known = [x for x in found if x is not None]
        if any(x[0] != relation.day_no for x in known) or known[0][1] >= known[1][1]:
            errors.append("旧先后约束被覆盖")
    for fixed in req.fixed_arrangements:
        current = names.get(fixed.name)
        if (
            not current
            or current[0] != fixed.day_no
            or current[2]["start_time"] != fixed.start_time
            or current[2]["end_time"] != fixed.end_time
        ):
            errors.append("旧固定时段被覆盖")
    return errors
