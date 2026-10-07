"""确定性排布与共用小工具的行为钉（app/agent/generation/content/generators.py）。

fallback_generate 是 eval_baselines 的"无 LLM 基线"消融专用路径：它的价值在
**确定性**——同样的候选必须产出同样的行程。这里把排布结构、预算排序倾向、
跨天去重与预算估算的算术逐条钉住，改任何一条都是消融基线的口径变化。
"""

from __future__ import annotations

from app.agent.generation.content import generators
from app.agent.generation.content.generators import (
    dedupe_daily_plans,
    fallback_generate,
    pick_hotels,
)


def _poi(
    name: str, *, lat: float | None = None, lng: float | None = None, price: float = 0.0, category: str = "attraction"
) -> dict:
    return {
        "id": name,
        "name": name,
        "category": category,
        "address": f"{name}地址",
        "latitude": lat,
        "longitude": lng,
        "ticket_price": price,
        "tags": "",
        "description": "",
        "image": None,
        "duration_min": None,
        "open_time": None,
    }


def _hotel(name: str, price: float, *, description: str = "", tags: str = "") -> dict:
    return {"id": name, "name": name, "ticket_price": price, "description": description, "tags": tags}


# ---------- pick_hotels ----------


def test_pick_hotels_empty_input_returns_empty() -> None:
    assert pick_hotels(None, "豪华型", 2) == []
    assert pick_hotels([], "豪华型", 2) == []


def test_pick_hotels_multi_tier_keywords_and_backfill() -> None:
    """多选拼接档位（「经济型、豪华型」）逐段查关键词表；匹配的排前，其余按原序补齐。"""
    budget = _hotel("如家", 100, description="经济连锁")
    five_star = _hotel("半岛", 900, tags="五星 海景")
    plain = _hotel("汉庭", 200, description="连锁商务")

    picked = pick_hotels([plain, budget, five_star], "经济型、豪华型", 3)

    assert [h["name"] for h in picked] == ["如家", "半岛", "汉庭"]


def test_pick_hotels_unknown_tier_uses_raw_keyword_and_count_caps() -> None:
    """表外档位关键词原样参与匹配（TIER_KEYWORDS.get 的兜底分支）；count 截断生效。"""
    a = _hotel("A", 1, description="湖景房")
    b = _hotel("B", 2, description="山景房")
    c = _hotel("C", 3, description="湖景豪宅")

    picked = pick_hotels([a, b, c], "湖景", 1)

    assert [h["name"] for h in picked] == ["A"]


def test_pick_hotels_no_tier_keeps_original_order() -> None:
    hotels = [_hotel("B", 2), _hotel("A", 1)]
    assert [h["name"] for h in pick_hotels(hotels, None, 2)] == ["B", "A"]


# ---------- fallback_generate ----------


def test_fallback_generate_day_structure_and_slots() -> None:
    """每天 = 2 景点（09:00/13:30 槽）+ 晚餐（18:00 槽）+ 酒店（21:00 入住）。"""
    attractions = [
        _poi("灵隐寺", lat=30.24, lng=120.10),
        _poi("西湖", lat=30.25, lng=120.15),
        _poi("宋城", lat=30.20, lng=120.03),
    ]
    foods = [_poi("楼外楼", category="food")]
    hotels = [_hotel("杭州饭店", 300)]
    consumption = {"meal_price": 60, "transport_price": 35, "hotel_price": 300}

    plans, _budget = fallback_generate(
        "杭州",
        2,
        2,
        [],
        hotels=hotels,
        hotel_tier=None,
        attractions=attractions,
        foods=foods,
        consumption=consumption,
    )

    assert [plan["day_no"] for plan in plans] == [1, 2]
    day1 = plans[0]["items"]
    assert [item["item_type"] for item in day1] == ["attraction", "attraction", "food", "hotel"]
    assert [(item["start_time"], item["end_time"]) for item in day1] == [
        ("09:00", "11:30"),
        ("13:30", "16:00"),
        ("18:00", "19:00"),
        ("21:00", "08:00"),
    ]
    assert day1[3]["poi_name"] == "杭州饭店" and day1[3]["tag"] == "住宿"


def test_fallback_generate_nearest_neighbor_order() -> None:
    """带坐标的景点按最近邻重排：从第一个点出发，每次取最近者。"""
    west = _poi("西湖", lat=30.25, lng=120.15)
    far = _poi(" far", lat=30.40, lng=120.40)
    near = _poi("near", lat=30.26, lng=120.16)

    plans, _ = fallback_generate(
        "杭州", 1, 1, [], attractions=[west, far, near], foods=[], consumption={"hotel_price": 300}
    )

    assert [item["poi_name"] for item in plans[0]["items"] if item["item_type"] == "attraction"] == ["西湖", "near"]


def test_fallback_generate_without_coords_keeps_input_order() -> None:
    """候选无坐标时跳过最近邻（geo 对空坐标返回空表），保持输入顺序。"""
    a = _poi("甲")
    b = _poi("乙")

    plans, _ = fallback_generate("杭州", 1, 1, [], attractions=[a, b], foods=[], consumption=None)

    assert [item["poi_name"] for item in plans[0]["items"] if item["item_type"] == "attraction"] == ["甲", "乙"]


def test_fallback_generate_budget_tilt_sorts_hotels_and_foods_by_price(monkeypatch) -> None:
    """预算倾向：人均每天 ≥600 高价优先，<250 低价优先——兜底路线也体现预算差异。

    餐食选取按 `foods[day_no % len(foods)]` 轮转：单天用例取的是排序后**末位**，
    因此第 1 天高价档吃到的恰是便宜的那家——用 cost 与酒店项共同证明排序方向。
    """
    hotels = [_hotel("中档", 300), _hotel("奢华", 900), _hotel("经济", 100)]
    foods = [_poi("小吃", category="food", price=30), _poi("名店", category="food", price=200)]
    consumption = {"meal_price": 60, "transport_price": 35, "hotel_price": 300}
    monkeypatch.setattr(generators.tools, "get_consumption", lambda _city: consumption)

    rich_plans, _ = fallback_generate(
        "杭州",
        1,
        1,
        [],
        hotels=hotels,
        attractions=[_poi("西湖", lat=30.25, lng=120.15)],
        foods=foods,
        consumption=consumption,
        budget_limit=6000,
    )
    tight_plans, _ = fallback_generate(
        "杭州",
        1,
        1,
        [],
        hotels=hotels,
        attractions=[_poi("西湖", lat=30.25, lng=120.15)],
        foods=foods,
        consumption=consumption,
        budget_limit=200,
    )

    rich_day = rich_plans[0]["items"]
    # desc 排序后 [名店200, 小吃30]，day 1 轮转到下标 1 → 小吃（单景点日 items = [景, 餐, 酒店]）
    assert (rich_day[1]["poi_name"], rich_day[1]["cost"]) == ("小吃", 30)
    assert (rich_day[2]["poi_name"], rich_day[2]["cost"]) == ("奢华", 900)
    tight_day = tight_plans[0]["items"]
    # asc 排序后 [小吃30, 名店200]，day 1 轮转到下标 1 → 名店
    assert (tight_day[1]["poi_name"], tight_day[1]["cost"]) == ("名店", 200)
    assert (tight_day[2]["poi_name"], tight_day[2]["cost"]) == ("经济", 100)


def test_fallback_generate_candidate_exhaustion_drops_repeats_but_keeps_hotels() -> None:
    """候选耗尽：跨天重复的景点/餐食被去重丢弃；酒店不参与去重、逐日保留。"""
    attractions = [_poi("灵隐寺", lat=30.24, lng=120.10), _poi("西湖", lat=30.25, lng=120.15)]
    foods = [_poi("楼外楼", category="food")]

    plans, _ = fallback_generate(
        "杭州",
        3,
        1,
        [],
        hotels=[_hotel("杭州饭店", 300)],
        attractions=attractions,
        foods=foods,
        consumption={"hotel_price": 300},
    )

    day1_names = [item["poi_name"] for item in plans[0]["items"]]
    assert day1_names == ["灵隐寺", "西湖", "楼外楼", "杭州饭店"]
    # 第 2 天：两个景点与餐食都已用过、候选池耗尽 → 全部丢弃，只剩酒店
    assert [item["poi_name"] for item in plans[1]["items"]] == ["杭州饭店"]
    assert [item["poi_name"] for item in plans[2]["items"]] == ["杭州饭店"]


def test_fallback_generate_without_attractions_still_lays_food_and_hotel() -> None:
    """空景点池：排布退化为 餐食+酒店，不炸（离线消融环境候选缺失的边界）。"""
    plans, budget = fallback_generate(
        "杭州",
        2,
        1,
        [],
        hotels=[_hotel("杭州饭店", 300)],
        attractions=[],
        foods=[],
        consumption={"hotel_price": 300},
    )
    assert all([item["poi_name"] for item in plan["items"]] == ["杭州饭店"] for plan in plans)
    assert budget["门票"] == 0.0, "无候选时门票按 0 计（均价分母取 1）"

    plans_with_food, _ = fallback_generate(
        "杭州",
        1,
        1,
        [],
        hotels=[_hotel("杭州饭店", 300)],
        attractions=[],
        foods=[_poi("楼外楼", category="food", price=88)],
        consumption={"hotel_price": 300},
    )
    assert [item["poi_name"] for item in plans_with_food[0]["items"]] == ["楼外楼", "杭州饭店"]


def test_fallback_generate_hotel_fallback_uses_consumption_price() -> None:
    """无候选酒店时按消费基准生成占位酒店项（默认 300，单间计费）。"""
    plans, _ = fallback_generate(
        "杭州", 1, 1, [], hotels=[], attractions=[_poi("西湖")], foods=[], consumption={"hotel_price": 288.456}
    )
    hotel = plans[0]["items"][-1]
    assert hotel["poi_name"] == "杭州市区舒适酒店"
    assert hotel["cost"] == 288.46
    assert hotel["tag"] == "酒店" and hotel["remark"] == "按单间计费"


def test_fallback_generate_estimates_budget_with_shared_rooms() -> None:
    """预算估算：门票=均价×3×人数；房量=⌈人数/2⌉；缺票价的点按 0 计。"""
    attractions = [_poi("甲", price=100), _poi("乙", price=200), _poi("丙")]
    foods = [_poi("楼外楼", category="food", price=88)]
    consumption = {"meal_price": 50, "transport_price": 10, "hotel_price": 400}

    _, budget = fallback_generate("杭州", 2, 3, [], attractions=attractions, foods=foods, consumption=consumption)

    assert budget == {
        "门票": round((100 + 200 + 0) / 3 * 3 * 3, 2),  # 均价100 × 3次 × 3人
        "餐饮": 50 * 2 * 2 * 3,  # 每日两餐 × 2天 × 3人
        "交通": 10 * 2 * 3,
        "酒店": 400 * 2 * 2,  # ⌈3/2⌉=2 间 × 2 晚
    }


def test_fallback_generate_none_consumption_falls_back_to_tools(monkeypatch) -> None:
    """consumption=None 时经 tools.get_consumption 查城市消费基准；空 dict 才走内置默认值。"""
    sentinel = {"meal_price": 99, "transport_price": 11, "hotel_price": 222}
    queried: list[str] = []
    monkeypatch.setattr(generators.tools, "get_consumption", lambda city: queried.append(city) or sentinel)

    _, budget = fallback_generate("杭州", 1, 1, [], attractions=[_poi("西湖")], foods=[], consumption=None)
    assert queried == ["杭州"], "缺消费基准必须经工具补齐"
    assert budget == {"门票": 0.0, "餐饮": 198.0, "交通": 11.0, "酒店": 222.0}

    _, defaults = fallback_generate("杭州", 1, 1, [], attractions=[_poi("西湖")], foods=[], consumption={})
    assert queried == ["杭州"], "空 dict 不再触发工具查询"
    assert defaults == {"门票": 0.0, "餐饮": 120.0, "交通": 35.0, "酒店": 300.0}


# ---------- dedupe_daily_plans ----------


def test_dupe_replaced_by_unused_pool_candidate_keeps_slot() -> None:
    """跨天重复优先用候选池里未使用的同名类型 POI 顶替，时段原样保留。"""
    x = _poi("灵隐寺", lat=30.24, lng=120.10)
    y = _poi("飞来峰")
    plans = [
        {"day_no": 1, "items": [_item("attraction", "灵隐寺", "09:00", "11:30")]},
        {"day_no": 2, "items": [_item("attraction", "灵隐寺", "13:30", "16:00")]},
    ]

    deduped = dedupe_daily_plans(plans, candidates=[x, y])

    assert [i["poi_name"] for i in deduped[0]["items"]] == ["灵隐寺"]
    replacement = deduped[1]["items"][0]
    assert replacement["poi_name"] == "飞来峰"
    assert (replacement["start_time"], replacement["end_time"]) == ("13:30", "16:00")


def test_dupe_food_pulls_from_food_pool_not_attraction_pool() -> None:
    """餐食重复从餐池顶替，不串用景点候选。"""
    food_dup = _item("food", "楼外楼", "18:00", "19:00")
    other_food = _poi("知味观", category="food")
    other_attr = _poi("飞来峰")
    plans = [
        {"day_no": 1, "items": [dict(food_dup)]},
        {"day_no": 2, "items": [dict(food_dup)]},
    ]

    deduped = dedupe_daily_plans(plans, candidates=[other_attr], foods=[other_food])

    assert deduped[1]["items"][0]["poi_name"] == "知味观"


def test_dupe_drops_when_pool_exhausted_and_keeps_hotel() -> None:
    """候选耗尽直接丢弃重复项；同名酒店不被去重（破坏住宿安排）。"""
    hotel = _item("hotel", "杭州饭店", "21:00", "08:00")
    dup = _item("attraction", "灵隐寺", "09:00", "11:30")
    plans = [
        {"day_no": 1, "items": [dict(dup), dict(hotel)]},
        {"day_no": 2, "items": [dict(dup), dict(hotel)]},
    ]

    deduped = dedupe_daily_plans(plans, candidates=[_poi("灵隐寺")])

    assert [i["poi_name"] for i in deduped[0]["items"]] == ["灵隐寺", "杭州饭店"]
    assert [i["poi_name"] for i in deduped[1]["items"]] == ["杭州饭店"]


def test_dupe_handles_empty_plans_and_missing_items() -> None:
    """空列表原样返回；items 缺失的天规整为空列表。"""
    assert dedupe_daily_plans([], candidates=[]) == []
    plans = [{"day_no": 1}]
    assert dedupe_daily_plans(plans) == [{"day_no": 1, "items": []}]


def _item(item_type: str, name: str, start: str, end: str) -> dict:
    return {
        "item_type": item_type,
        "poi_name": name,
        "poi_id": "",
        "address": None,
        "latitude": None,
        "longitude": None,
        "start_time": start,
        "end_time": end,
        "duration_min": None,
        "open_time": None,
        "cost": None,
        "tag": None,
        "remark": None,
        "image": None,
    }
