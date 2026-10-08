"""离线评测数据面：模拟权威检索结果与确定性 LLM 输出。

LLM-only 口径下生产链路不再有确定性 fallback，因此离线评测改为
mock 开放模式的模型输出（fixture_open_day / fixture_open_trip）：
从固定 fixture 目录按序取点生成合法行程 JSON，不依赖真实 LLM，
但走真实的编排/落地/反思/格式化链路。

C3.2 坐标参数化：fixture 坐标按城市名生成——汉字城市落在国内国界框内
（杭州附近），非汉字城市落海外（巴塞罗那附近，_in_china 框外），
使国内外分流语义在离线评测可观测；`coords=False` 变体剥掉坐标，
覆盖「无坐标项」边界（深链降级/未核实标记）。
"""

from __future__ import annotations

import json
from copy import deepcopy

from app.agent.grounding.existence import VERIFIED, ResolveResult
from app.agent.grounding.grounding_evidence import issue_evidence
from app.agent.runtime.trace import traced


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _base_coords(city: str) -> tuple[float, float]:
    """汉字城市 → 国内框内基准（30, 120）；非汉字 → 海外基准（41.39, 2.17）。"""
    return (30.0, 120.0) if _has_cjk(city) else (41.39, 2.17)


def _attractions(city: str, *, coords: bool = True) -> list[dict]:
    base_lat, base_lng = _base_coords(city)
    return [
        {
            "id": i,
            "name": f"{city}景点{i}",
            "category": "attraction",
            "address": f"{city}示例区{i}",
            # M3 步行 4.5km/h：0.01°(≈1.4km) 邻点在 30 分钟留白下必超时，网格加密到 ≈110m
            "latitude": base_lat + i / 1000 if coords else None,
            "longitude": base_lng + i / 1000 if coords else None,
            "ticket_price": 20 + i * 5,
            "duration_min": 90,
            "open_time": "08:00-18:00",
            "tags": "人文 自然 网红",
            "rating": 4.8,
            "city": city,
            # 真实 tools.search_attractions 的行由 places 打源；fixture 替身必须
            # 带同一个值，否则"权威判定表"与"目录"就分不开（PLAN-A1 D11A）。
            "source": "opentripmap",
        }
        for i in range(1, 13)
    ]


def _foods(city: str, *, coords: bool = True) -> list[dict]:
    base_lat, base_lng = _base_coords(city)
    return [
        {
            "id": 100 + i,
            "name": f"{city}本地餐厅{i}",
            "category": "food",
            "address": f"{city}美食街{i}",
            "latitude": base_lat + 0.002 + i / 1000 if coords else None,
            "longitude": base_lng + 0.002 + i / 1000 if coords else None,
            "ticket_price": 60 + i * 10,
            "duration_min": 60,
            "open_time": "10:00-22:00",
            "tags": "美食",
            "city": city,
            "source": "opentripmap",
        }
        for i in range(1, 5)
    ]


def _hotels(city: str, *, coords: bool = True) -> list[dict]:
    return [
        {
            "id": 200 + i,
            "name": f"{city}舒适酒店{i}",
            "category": "hotel",
            "address": f"{city}市中心{i}",
            "ticket_price": 300 + i * 50,
            "description": "舒适型酒店",
            "tags": "住宿",
            "city": city,
            # fixture 酒店行固定 web.search 源（eval 确定性选择）；真实链路的 OTM
            # 住宿池 2026-09-30 起可用——上游拼 accomodations，此前 400 是拼写错误
            # 而非"免费 key 不支持"。
            "source": "web.search",
        }
        for i in range(1, 3)
    ]


def catalog(city: str, *, coords: bool = True) -> dict:
    pool = {
        "attractions": _attractions(city, coords=coords),
        "foods": _foods(city, coords=coords),
        "hotels": _hotels(city, coords=coords),
    }
    # fixture 目录只回答"外部世界有哪些点"；能不能背书由票决定，所以这里显式
    # 给每行签发一张票（等价于生产里 tools._as_candidate 的当场签发）。
    # 没有票的 fixture 行会走 client-context 降级——那正是 P6 要钉的行为。
    for rows in pool.values():
        for row in rows:
            issue_evidence(row)
    return {
        "attractions": _attractions(city, coords=coords),
        "foods": _foods(city, coords=coords),
        "hotels": _hotels(city, coords=coords),
        "consumption": {"city": city, "meal_price": 60, "transport_price": 35, "hotel_price": 300},
    }


@traced("tool", "fixture.search_attractions")
def search_attractions(city: str, preferences: list[str], limit: int = 30, *, coords: bool = True) -> list[dict]:
    return deepcopy(catalog(city, coords=coords)["attractions"][:limit])


@traced("tool", "fixture.search_foods")
def search_foods(city: str, limit: int = 10, *, coords: bool = True) -> list[dict]:
    return deepcopy(catalog(city, coords=coords)["foods"][:limit])


@traced("tool", "fixture.search_hotels")
def search_hotels(city: str, limit: int = 6, *, coords: bool = True) -> list[dict]:
    return deepcopy(catalog(city, coords=coords)["hotels"][:limit])


@traced("tool", "fixture.get_consumption")
def get_consumption(city: str) -> dict:
    return deepcopy(catalog(city)["consumption"])


def attach_poi_images(plan: list[dict], city: str) -> list[dict]:
    return plan


@traced("tool", "fixture.search_local_poi")
def search_local_poi(city: str, name: str, *, category: str | None = None, coords: bool = True) -> list[dict]:
    """按名称回放 fixture 坐标（近邻/点名检索端点的替身）。"""
    for group in (_attractions(city, coords=coords), _foods(city, coords=coords), _hotels(city, coords=coords)):
        for poi in group:
            if poi["name"] == name:
                return [deepcopy(poi)]
    return []


@traced("tool", "fixture.resolve_poi")
def resolve_poi(name: str, city: str, *, coords: bool = True) -> ResolveResult:
    """存在性判定的离线替身：命中 fixture 目录即产出一张 VERIFIED 判定。

    D11A 解的就是这里：**目录只充当"外部世界有哪些点"的替身**，权威与否由本函数
    显式返回的判定（provider + external_id + 坐标）决定，生产代码不再拿
    "名字在目录里"隐式自证。目录里没有 → UNKNOWN（不是 NOT_FOUND：替身没有
    否证资格，与真实免费 provider 同口径）。
    """
    for group in (_attractions(city, coords=coords), _foods(city, coords=coords), _hotels(city, coords=coords)):
        for poi in group:
            if poi["name"] != name:
                continue
            return ResolveResult(
                state=VERIFIED,
                provider=str(poi.get("source") or "opentripmap"),
                name=name,
                external_id=str(poi.get("id") or name),
                latitude=poi.get("latitude"),
                longitude=poi.get("longitude"),
                address=poi.get("address"),
            )
    return ResolveResult.unknown("fixture_miss")


def plan_research(task) -> dict:
    """确定性研究规划：不扩展检索参数（等价阶段一的确定性行为）。"""
    return {}


def evaluate_research(task, items: list[dict], round_no: int) -> dict:
    """确定性研究评估：结果非空即视为充分（等价阶段一的确定性行为）。"""
    return {"sufficient": True, "extra_keywords": []}


def _take_unused(pool: list[dict], used: set[str], offset: int) -> dict | None:
    for poi in pool[offset:]:
        if poi["name"] not in used:
            return deepcopy(poi)
    return None


def _item(poi: dict, start: str, end: str) -> dict:
    return {
        "item_type": poi["category"],
        "poi_name": poi["name"],
        "start_time": start,
        "end_time": end,
        "duration_min": poi.get("duration_min") or 90,
        "cost": float(poi.get("ticket_price") or 0),
        "tag": poi.get("tags"),
    }


def fixture_open_day(req, used: set[str]) -> dict:
    """确定性单日开放模式输出：2 景点 + 1 餐饮 + 1 酒店（名字来自 fixture）。"""
    city = req.city
    data = catalog(city)
    offset = (req.day_no - 1) * 2
    a1 = _take_unused(data["attractions"], used, offset)
    a2 = _take_unused(data["attractions"], used, offset + 1)
    food = _take_unused(data["foods"], used, (req.day_no - 1) % len(data["foods"]))
    hotel = data["hotels"][(req.day_no - 1) % len(data["hotels"])]
    items = []
    if a1:
        items.append(_item(a1, "09:00", "10:30"))
    if a2:
        items.append(_item(a2, "11:00", "12:30"))
    if food:
        items.append(_item(food, "18:00", "19:00"))
    if req.needs_hotel:
        items.append(_item(hotel, "21:00", "08:00"))
    return {"note": f"{city}第{req.day_no}天行程", "items": items, "suggestions": []}


def fixture_open_trip(req) -> tuple[list[dict], list[dict]]:
    """确定性多日开放模式输出：逐天展开 fixture_open_day（day_no 逐天递增）。"""
    days = req.days or 1
    plans = []
    taken: set[str] = set()
    for day_no in range(1, days + 1):
        day_req = req.model_copy(update={"day_no": day_no})
        plan = fixture_open_day(day_req, taken)
        for item in plan["items"]:
            taken.add(item["poi_name"])
        plan["day_no"] = day_no
        plans.append(plan)
    return plans, []


# ---- editing 域替身（PR-8）：editing 流程的 LLM 出口替身（raw JSON，契约形状） ----


def fixture_edit_reply(ops: list[dict]) -> str:
    """nl_edit（run_edit_ops）出口替身：按给定操作表回 JSON。"""
    return json.dumps({"ops": ops}, ensure_ascii=False)


def fixture_clarify_reply(**fields) -> str:
    """clarify（run_clarify）出口替身：按给定槽位回 JSON，缺省字段为 null（用户没提）。"""
    keys = ("city", "start_date", "days", "stay_nights", "persons", "budget", "hotel_tier", "preferences")
    return json.dumps({key: fields.get(key) for key in keys}, ensure_ascii=False)


def fixture_city_guide_reply(kind: str, city, message: str, suggestions: list[dict]) -> str:
    """city_guide（run_city_guide）出口替身：按给定判定回 JSON。"""
    payload = {"kind": kind, "city": city, "message": message, "suggestions": suggestions}
    return json.dumps(payload, ensure_ascii=False)
