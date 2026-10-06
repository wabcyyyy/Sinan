"""generation_core 单测：产品口径唯一真相源。"""

from app.agent.generation.rules.generation_core import (
    MAX_DAY_ATTEMPTS,
    MAX_FIX_ATTEMPTS,
    MAX_GENERATION_ATTEMPTS,
    count_hotel_nights_in_budget,
    day_hotel_clause,
    day_needs_hotel,
    draft_day_plans,
    filter_dirty_items,
    hotel_prompt_clause,
    spread_hotels,
    stay_nights,
)


def test_stay_nights_is_n_minus_1():
    assert stay_nights(3) == 2
    assert stay_nights(1) == 0
    assert stay_nights(None) == 0


def test_day_needs_hotel_last_day_false():
    assert day_needs_hotel(1, 3) is True
    assert day_needs_hotel(2, 3) is True
    assert day_needs_hotel(3, 3) is False
    assert day_needs_hotel(1, 3, override=False) is False


def test_hotel_prompt_clause_multi_day():
    text = hotel_prompt_clause(True, 3)
    assert "第 1 至第 2 天" in text
    assert "全程沿用同一家" in text
    assert "最后一天不安排入住" in text


def test_budget_skips_last_day_hotel():
    assert count_hotel_nights_in_budget(3, 3, "hotel") is False
    assert count_hotel_nights_in_budget(2, 3, "hotel") is True
    assert count_hotel_nights_in_budget(1, 1, "hotel") is True


def test_spread_hotels_fills_missing_nights():
    plans = [
        {"day_no": 1, "items": [{"item_type": "hotel", "poi_name": "A"}]},
        {"day_no": 2, "items": []},
        {"day_no": 3, "items": []},
    ]
    added = spread_hotels(plans, stay_nights(3))
    assert added == 1
    assert any(i.get("poi_name") == "A" for i in plans[1]["items"])
    # 最末日不摊铺
    assert plans[2]["items"] == []


def test_draft_plans_empty_and_labeled():
    plans = draft_day_plans("杭州", 2, "llm down")
    assert len(plans) == 2
    assert all(p["items"] == [] and "待研究" in p["note"] for p in plans)


def test_filter_dirty_items():
    out = filter_dirty_items(
        [
            {"poi_name": "西湖"},
            {"poi_name": ""},
            "nope",
            {"name": "x"},
        ]
    )
    assert out == [{"poi_name": "西湖"}]


def test_attempt_constants_aligned():
    assert MAX_DAY_ATTEMPTS == MAX_FIX_ATTEMPTS
    assert MAX_GENERATION_ATTEMPTS == 2


def test_normalize_item_type_and_sanitize():
    from app.agent.generation.rules.generation_core import normalize_item_type, sanitize_itinerary_items

    assert normalize_item_type("souvenir") == "attraction"
    assert normalize_item_type("FOOD") == "food"
    assert normalize_item_type("nonsense") == "attraction"
    rows = sanitize_itinerary_items(
        [
            {"poi_name": "浅草寺", "item_type": "souvenir"},
            {"poi_name": "拉面", "item_type": "restaurant"},
            {"item_type": "attraction"},
        ]
    )
    assert [r.get("item_type") for r in rows] == ["attraction", "food"]
    assert rows[0].get("poi_name") == "浅草寺"


def test_day_hotel_clause():
    assert day_hotel_clause(True) == "安排 1 家酒店；"
    assert day_hotel_clause(False) == "今日无需安排酒店；"
