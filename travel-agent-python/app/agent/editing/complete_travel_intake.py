"""NL-only attribute/entity binding against public sandbox taxonomy; never accepts query DSL or typed Gold."""

import json

from app.agent.core.json_utils import parse_llm_json
from app.agent.data.chinatravel import research
from app.schemas.travel_plan import TravelRequirements
from app.schemas.trip import GenerateRequest

SYSTEM = """只根据用户原话和公开候选/分类，把旅行偏好绑定为JSON，不编造额外要求。
属性值必须来自给定taxonomy；required_names/excluded_names用给定候选的完整名，不能把目的城市当必去景点。
例如想看自然景观绑定官方自然风光类别，尝当地特色菜按给定城市候选菜系判断；有歧义写unresolved。
只是想体验某交通方式填required；明确只坐/往返均坐才填allowed_intercity_modes。
主要地铁填required_innercity_modes=["metro"]；不要禁止其步行接驳。
allowed_intercity_modes/required_intercity_modes的值只能是英文train或airplane，禁止填火车/飞机等中文。
required_innercity_modes的值只能是英文walk/metro/taxi。同行者身份、没有偏好、没有孩子年龄不是额外未解决要求。
rooms是房间数，room_beds是每间床数，均只能为整数或null，禁止对象或字符串；大床房为1床，双床房为2床。
房间数和床数只取明确原话；没有就null。没有具体要求的列表为空，不擅加预算/日期/时窗/景点数。
max_hotel_cost_per_person_per_night仅取明确“酒店每人每晚上限”的金额，否则null；
酒店每间价格、住宿全程总额、整趟预算不能冒充该字段。只说总预算时该字段必须null。
人数、天数和总预算已由上层处理，不放入unresolved；无法映射的每间房价要求仍按原话放入unresolved。
未提出偏好不属于未解决要求，绝对不要把“用户未明确某偏好”放入unresolved。
unresolved只能填写用户实际提出、你无法映射的要求的逐字原文片段；没有这种要求时必须为空列表。
输出对象含attraction_types,restaurant_cuisines,hotel_features,required_names,excluded_names,
allowed_intercity_modes,required_intercity_modes,required_innercity_modes,rooms,room_beds,
max_hotel_cost_per_person_per_night,unresolved。
不输出规划、不执行Python、不改变用户人数/天数/总预算。"""


def extract(message, state, client):
    context = research(
        GenerateRequest(
            city=state.city,
            origin_city=state.origin_city,
            days=state.days,
            persons=state.persons,
            requirements_struct=state.requirements,
        )
    )
    taxonomy = {
        "attraction_types": sorted({r["type"] for r in context["candidates"]}),
        "restaurant_cuisines": sorted({r["cuisine"] for r in context["foods"]}),
        "hotel_features": sorted({r["featurehoteltype"] for r in context["hotels"]}),
    }
    names = {}
    requested = [place.name for place in state.requirements.required_places] + state.requirements.excluded_places
    for key in ("candidates", "foods", "hotels"):
        names[key] = [row["name"] for row in context[key][:16]]
        for row in context[key]:
            full = row["name"]
            local = full[len(state.city) :] if full.startswith(state.city) else full
            if full in message or (len(local) >= 3 and local in message):
                names[key].append(full)
        for name in requested:
            names[key].extend(
                row["name"] for row in context[key] if name in row["name"] and row["name"] not in names[key]
            )
        names[key] = list(dict.fromkeys(names[key]))
        if len(names[key]) > 64:
            raise ValueError("explicit entity references exceed the bounded binding view; clarification required")
    raw = client.complete(
        json.dumps(
            {"original_user_request": message, "city": state.city, "taxonomy": taxonomy, "names": names},
            ensure_ascii=False,
        ),
        system_prompt=SYSTEM,
        temperature=0,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )
    result = TravelRequirements.model_validate(parse_llm_json(raw))
    if any(quote not in message for quote in result.unresolved):
        raise ValueError("unresolved requirements must quote actual user text, not invented missing preferences")
    for key, allowed in taxonomy.items():
        if any(value not in allowed for value in getattr(result, key)):
            raise ValueError(f"NL attribute binding produced a non-sandbox value: {key}")
    allowed_names = {name for rows in names.values() for name in rows}
    if any(name not in allowed_names for name in result.required_names + result.excluded_names):
        raise ValueError("NL entity binding produced an unexposed entity")
    for place in state.requirements.required_places:
        matches = [name for name in result.required_names if place.name in name or name in place.name]
        if len(matches) == 1:
            place.name = matches[0]  # NL-to-source binding from the same public candidate view, not Gold.
    return result
