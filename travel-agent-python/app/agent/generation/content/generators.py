"""生成链路的确定性排布与工具函数（G-2.3 拆分后的剩余核心）。

职责：
- fallback_generate：确定性排布，**仅供离线消融评测（eval_baselines）使用**，
  不在生产链路中调用（LLM-only 原则：行程内容 100% 由 LLM 生成）；
- pick_hotels / dedupe_daily_plans / _estimate_budget 及其私有辅助：被
  fallback 排布与上层共用的小工具。

兄弟模块（按蓝图分出的邻居）：budget（档位与约束句/餐价钳制）、
suggestions（备选池）、reference_pool（参考资料与权威判定）、
day_prompts（Prompt 组装与约束句）、narrative（叙事清洗）、grounding（事实落地）。

依赖：tools（检索候选）、geo（最近邻排序）、trace。
"""

import logging
from math import ceil

from app.agent.core.geo import nearest_neighbor_order
from app.agent.generation.rules.budget import TIER_KEYWORDS
from app.agent.tools import impl as tools

logger = logging.getLogger(__name__)

TIME_SLOTS = [
    ("09:00", "11:30"),
    ("13:30", "16:00"),
    ("19:00", "20:30"),
]
THREE_ATTRACTION_SLOTS = [
    ("09:00", "11:00"),
    ("13:30", "15:30"),
    ("16:00", "18:00"),
]


# 低质/与旅行体验无关的场所：在 prompt 层约束模型不要选入行程与备选池
def _daily_attraction_target(days: int) -> int:
    """确定性兜底的每日景点数：总天数越长，每日安排越精简、节奏越舒适。"""
    # 两个景点能为跨城移动和用餐保留足够缓冲；短途也不以堆景点换取数量。
    return 2


def pick_hotels(hotels: list[dict] | None, tier: str | None, count: int) -> list[dict]:
    """按档次关键词优先挑选（tier 可为「经济型、豪华型」多选拼接），凑不满则用其余补齐。"""
    if not hotels:
        return []
    keywords: tuple[str, ...] = ()
    for part in (tier or "").replace("，", "、").split("、"):
        if not part:
            continue
        keywords = keywords + TIER_KEYWORDS.get(part, (part,))
    matched = [h for h in hotels if any(k in (h.get("description") or "") + (h.get("tags") or "") for k in keywords)]
    picked = matched + [h for h in hotels if h not in matched]
    return picked[:count]


def fallback_generate(
    city: str,
    days: int,
    persons: int,
    preferences: list[str],
    hotels: list[dict] | None = None,
    hotel_tier: str | None = None,
    attractions: list[dict] | None = None,
    foods: list[dict] | None = None,
    consumption: dict | None = None,
    pace_days: int | None = None,
    budget_limit: float | None = None,
    report_sink: dict | None = None,
) -> tuple[list[dict], dict]:
    """确定性排布（仅供离线消融评测使用，非生产路径）。

    直接用知识库候选按时间槽 + 最近邻顺序拼装行程。生产链路遵循 LLM-only
    原则不调用本函数——它存在只是为了 eval_baselines 的"无 LLM 基线"消融。
    """
    attractions = attractions if attractions is not None else tools.search_attractions(city, preferences)
    foods = foods if foods is not None else tools.search_foods(city)
    consumption = consumption if consumption is not None else tools.get_consumption(city)

    # 预算倾向：人均每天预算高时优先高价（高档）酒店与名店餐饮，紧张时优先低价，
    # 保证确定性兜底路线同样能体现预算差异。
    per_day = (float(budget_limit) / max(days, 1)) if budget_limit and float(budget_limit) > 0 else None
    if per_day is not None and hotels:
        reverse = per_day >= 600
        hotels = sorted(hotels, key=lambda h: float(h.get("ticket_price") or 0), reverse=reverse)
        if per_day < 250:
            hotels = sorted(hotels, key=lambda h: float(h.get("ticket_price") or 0))
    if per_day is not None and foods:
        reverse = per_day >= 600
        foods = sorted(foods, key=lambda f: float(f.get("ticket_price") or 0), reverse=reverse)
        if per_day < 250:
            foods = sorted(foods, key=lambda f: float(f.get("ticket_price") or 0))

    daily_plans: list[dict] = []
    cursor = 0
    ordered = nearest_neighbor_order(attractions)
    if not ordered:
        ordered = attractions
    for day_no in range(1, days + 1):
        items: list[dict] = []
        for slot_index in range(_daily_attraction_target(pace_days or days)):
            poi = ordered[cursor % len(ordered)] if ordered else None
            cursor += 1
            if poi is None:
                continue
            slots = THREE_ATTRACTION_SLOTS if _daily_attraction_target(pace_days or days) >= 3 else TIME_SLOTS
            start, end = slots[slot_index]
            items.append(_to_item(poi, start, end))
        if foods:
            food = foods[day_no % len(foods)]
            # 三景点日的 19:00 槽已经是最后一个景点，晚餐若紧接其后会
            # 没有任何换乘余量；放到午间空档，给下午/晚间景点留出路线缓冲。
            meal_start, meal_end = ("11:30", "12:30") if len(items) >= 3 else ("18:00", "19:00")
            items.append(_to_item(food, meal_start, meal_end))
        tier_hotels = pick_hotels(hotels, hotel_tier, 2)
        items.append(_hotel_item(city, consumption, tier_hotels or hotels, day_no))
        daily_plans.append({"day_no": day_no, "note": f"{city}第{day_no}天行程", "items": items})

    # 先去重
    daily_plans = dedupe_daily_plans(daily_plans, attractions, foods)
    budget = _estimate_budget(attractions, foods, consumption, days, persons)
    return daily_plans, budget


def dedupe_daily_plans(
    plans: list[dict], candidates: list[dict] | None = None, foods: list[dict] | None = None
) -> list[dict]:
    """去除整个行程中跨天重复的景点/餐饮。

    重复项优先用候选池里尚未使用的同名类型 POI 替换，保持每日密度；候选用尽时直接丢弃。
    酒店不被去重，避免破坏住宿安排。
    """
    if not plans:
        return plans
    used: set[str] = set()
    attr_pool = [c for c in (candidates or []) if c.get("name")]
    food_pool = [f for f in (foods or []) if f.get("name")]

    def _take(pool: list[dict], idx: int):
        while idx < len(pool):
            cand = pool[idx]
            idx += 1
            if cand.get("name") not in used:
                return cand, idx
        return None, idx

    attr_idx = food_idx = 0
    for plan in plans:
        kept: list[dict] = []
        for item in plan.get("items") or []:
            name = item.get("poi_name")
            itype = item.get("item_type")
            if itype in ("attraction", "food") and name:
                if name in used:
                    if itype == "attraction":
                        repl, attr_idx = _take(attr_pool, attr_idx)
                    else:
                        repl, food_idx = _take(food_pool, food_idx)
                    if repl:
                        repl_name = repl.get("name")
                        if repl_name:
                            used.add(repl_name)
                        kept.append(_to_item(repl, item.get("start_time"), item.get("end_time")))
                    continue
                used.add(name)
            kept.append(item)
        plan["items"] = kept
    return plans


def _to_item(poi: dict, start: str, end: str) -> dict:
    return {
        "item_type": poi.get("category", "attraction"),
        "poi_name": poi.get("name", ""),
        "poi_id": str(poi.get("id") or ""),
        "address": poi.get("address"),
        "latitude": poi.get("latitude"),
        "longitude": poi.get("longitude"),
        "start_time": start,
        "end_time": end,
        "duration_min": poi.get("duration_min"),
        "open_time": poi.get("open_time"),
        "cost": poi.get("ticket_price"),
        "tag": poi.get("tags"),
        "remark": None,
        "image": poi.get("image"),
    }


def _hotel_item(city: str, consumption: dict | None, hotels: list[dict] | None = None, day_no: int = 1) -> dict:
    if hotels:
        poi = hotels[(day_no - 1) % len(hotels)]
        return {
            "item_type": "hotel",
            "poi_name": poi.get("name", ""),
            "poi_id": str(poi.get("id") or ""),
            "address": poi.get("address"),
            "latitude": None,
            "longitude": None,
            "start_time": "21:00",
            "end_time": "08:00",
            "duration_min": 660,
            "cost": float(poi.get("ticket_price") or 0),
            "tag": "住宿",
            "remark": poi.get("description") or "知识库酒店基准价",
        }
    price = consumption.get("hotel_price", 300.0) if consumption else 300.0
    return {
        "item_type": "hotel",
        "poi_name": f"{city}市区舒适酒店",
        "poi_id": None,
        "address": f"{city}市中心商圈",
        "latitude": None,
        "longitude": None,
        "start_time": "21:00",
        "end_time": "08:00",
        "duration_min": 660,
        "cost": round(float(price), 2),
        "tag": "酒店",
        "remark": "按单间计费",
    }


def _estimate_budget(
    attractions: list[dict], foods: list[dict], consumption: dict | None, days: int, persons: int
) -> dict:
    ticket = sum(float(a.get("ticket_price") or 0) for a in attractions) / max(len(attractions), 1) * 3
    meal = float(consumption.get("meal_price", 60.0) if consumption else 60.0) * 2
    transport = float(consumption.get("transport_price", 35.0) if consumption else 35.0)
    hotel = float(consumption.get("hotel_price", 300.0) if consumption else 300.0)
    rooms = ceil(persons / 2)
    return {
        "门票": round(ticket * persons, 2),
        "餐饮": round(meal * days * persons, 2),
        "交通": round(transport * days * persons, 2),
        "酒店": round(hotel * rooms * days, 2),
    }
