"""Whole-trip generation with explicit official tool grounding; no added activities or time repair."""

import json

from app.agent.core.json_utils import parse_llm_json
from app.agent.data import chinatravel
from app.agent.generation.content.constraint_candidate_policy import (
    COMPLETE_CATALOG_LIMITS,
    COMPLETE_OUTPUT_TOKENS,
    hotel_matches,
    requested_names,
)
from app.schemas.travel_plan import TravelHeader, TravelPlan
from app.schemas.trip import DailyPlan, TripItem

SYSTEM = """你是完整旅行规划器，按用户原话和冻结官方事实安排全程，JSON输出。
不添加用户没有给的具体日期、每日游玩时长或景点数要求，day是相对行程日。
包含首日出发地→目的地的火车/飞机和末日返回；考虑车站/机场接驳、餐饮、跨夜住宿、人数和总预算。
不得编造候选或改变源价格/车次时刻。城际源Cost是每张票价，活动price为单价，cost为总价；
景点/城际票数tickets、酒店rooms与room_type(床数)、每段交通的票数或车数必须由你明确选择。
返回原始官方形状：{"people_number":整数,"start_city":"城市","target_city":"城市",
"itinerary":[{"day":1,"activities":[{"type":"train|airplane|attraction|breakfast|lunch|dinner|accommodation",
"start_time":"HH:MM","end_time":"HH:MM","price":数字,"cost":数字,"transports":[],
"position":"POI完整名(景点/餐厅/酒店)","TrainID":"火车ID(仅火车)","FlightID":"航班ID(仅飞机)",
"start":"城际出发站(仅城际)","end":"城际到达站(仅城际)","tickets":票数(适用时),
"rooms":房间数(仅酒店),"room_type":床数(仅酒店),
"transfer_request":{"mode":"walk|metro|taxi","departure":"HH:MM","tickets":地铁票数或null,"cars":出租车数或null}}]}]}。
不要输出不适用字段。train/airplane绝对不能含position，景点/餐饮/酒店必须有position且不能含start/end/TrainID/FlightID。
所有活动必须有transports字段，第一项无接驳也明确写[]。例如火车只能以start/end描述车站，不能另加position。
transfer_request是活动对象的独立字段，不能放进transports数组；transports始终先写[]，由工具返回事实填充。
transfer_request是你选择的、从上一项所在地点前往本项地点的官方goto工具请求；
执行器只按这项请求查原始接驳事实，不改活动时间、不添加活动、不替你选择方式或数量。
除第一项无接驳外，其余位置变化时提供transfer_request；实际路程可能比估计长，要留充足时间。
火车/飞机时刻严格抄源，最后一班返回后不能再安排活动。午餐11–14点、晚餐17–20点，餐厅需营业。
早餐06–09点；在已选酒店吃早餐价格和总价均为0。同一家餐厅全程不要重复，各日餐次不能重复。
所有景点/火车/飞机都要明确tickets=用户人数，cost=price*tickets；餐饮cost=price*人数，酒店cost=price*rooms。
若到达已过午餐时段，就不要硬塞一顿迟到午餐。车次用train+TrainID，航班用airplane+FlightID，不能混用。
如信息不足仍输出你的最佳完整方案；执行和独立checker会保存失败，不要宣称已保证可行。"""


def catalog(context, requirements=None):
    # Same deterministic research exposure for A/B/C; full provider remains queryable locally.
    result = {"origin_city": context["origin_city"], "target_city": context["target_city"]}
    limits = dict(COMPLETE_CATALOG_LIMITS)
    constraints = requirements.travel_constraints if requirements else None
    wanted = requested_names(requirements) if requirements else set()
    for key, limit in limits.items():
        attribute, field = {
            "candidates": ("attraction_types", "type"),
            "foods": ("restaurant_cuisines", "cuisine"),
            "hotels": ("hotel_features", "featurehoteltype"),
        }[key]

        def priority(row, key=key, field=field, attribute=attribute):
            named = row["name"] in wanted or bool(
                key == "hotels"
                and requirements
                and requirements.lodging
                and hotel_matches(row, requirements.lodging.locked_hotel_identity)
            )
            matches = int(bool(constraints and row[field] in getattr(constraints, attribute)))
            matches += int(
                bool(
                    constraints
                    and key == "hotels"
                    and constraints.room_beds is not None
                    and int(row["numbed"]) == constraints.room_beds
                )
            )
            return -int(named), -matches

        result[key] = sorted(context[key], key=priority)[:limit]

    def timetable(rows):
        # Cover departure times without knowing a solution; fixed before any model results.
        buckets = {}
        for row in rows:
            bucket = int(row["BeginTime"].split(":")[0]) // 4
            if len(buckets.setdefault(bucket, [])) < 2:
                buckets[bucket].append(row)
        return [row for key in sorted(buckets) for row in buckets[key]]

    result["intercity"] = {
        direction: {mode: timetable(rows) for mode, rows in modes.items()}
        for direction, modes in context["intercity"].items()
    }
    result["exposure"] = {
        "selection": (
            "NL-bound names then attribute-match count, stable official order within ties; no Gold/outcome selection"
        ),
        "total": {key: len(context[key]) for key in limits},
        "limits": limits,
        "intercity_per_mode_direction": "first2 per 4-hour departure bucket",
    }
    return result


def bind_routes(data):
    """Execute only producer-selected goto requests; retain failures instead of guessing a route."""
    previous = None
    receipts = []
    for day in data.get("itinerary", []):
        for activity in day.get("activities", []):
            request = activity.get("transfer_request")
            target = (
                activity.get("start") if activity.get("type") in {"train", "airplane"} else activity.get("position")
            )
            if request is not None:
                try:
                    if previous is None or not target:
                        raise ValueError("no previous or target location for transfer")
                    legs = chinatravel.route(
                        data["target_city"], previous, target, request["departure"], request["mode"]
                    )
                    grounded = []
                    for leg in legs:
                        row = dict(leg)
                        row["price"] = row["cost"]
                        if row["mode"] == "metro":
                            row["tickets"] = request["tickets"]
                            row["cost"] = round(row["cost"] * request["tickets"], 2)
                        elif row["mode"] == "taxi":
                            row["cars"] = request["cars"]
                            row["cost"] = round(row["cost"] * request["cars"], 2)
                        grounded.append(row)
                    activity["transports"] = grounded
                    receipts.append({"from": previous, "to": target, "request": request, "legs": grounded})
                except Exception as exc:
                    receipts.append({"from": previous, "to": target, "request": request, "error": str(exc)})
                    activity["transports"] = []
            previous = (
                activity.get("end") if activity.get("type") in {"train", "airplane"} else activity.get("position")
            )
    return data, receipts


def project(plan):
    days = []
    for day in plan.itinerary:
        items = []
        for a in day.activities:
            kind = {
                "accommodation": "hotel",
                "breakfast": "food",
                "lunch": "food",
                "dinner": "food",
                "train": "transport",
                "airplane": "transport",
            }.get(a.type, "attraction")
            items.append(
                TripItem(
                    item_type=kind,
                    poi_name=a.position or a.train_id or a.flight_id or "交通",
                    start_time=a.start_time,
                    end_time=a.end_time,
                    cost=a.price,
                    cost_currency="CNY",
                    source="ChinaTravel-Sandbox",
                    value_kind="observed",
                )
            )
        days.append(
            DailyPlan(
                day_no=day.day,
                items=items,
                travel_activities=day.activities,
                travel_header=TravelHeader.model_validate(plan.model_dump()),
            )
        )
    return days


def prompt(request):
    facts = catalog(request.context, request.requirements_struct)
    user = json.dumps(
        {
            "original_user_request": request.intent or request.requirements,
            "facts": facts,
            "feedback": request.feedback or None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return SYSTEM, user


def generate(request, client):
    system, user = prompt(request)
    raw = client.complete(
        user,
        system_prompt=system,
        temperature=0,
        max_tokens=COMPLETE_OUTPUT_TOKENS,
        response_format={"type": "json_object"},
    )
    data, receipts = bind_routes(parse_llm_json(raw))
    request.context["travel_last_output"] = data
    request.context["travel_route_receipts"] = receipts
    plan = TravelPlan.model_validate(data)
    request.context["travel_raw_bound_output"] = data
    request.context["travel_last_output"] = plan.official()
    return [day.model_dump() for day in project(plan)], []
