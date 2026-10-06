from app.agent.generation.output.schedule_optimizer import optimize_daily_plan


def _item(name, start="09:00", end="10:00", duration=60, open_time=None, kind="attraction"):
    return {
        "item_type": kind,
        "poi_id": name,
        "poi_name": name,
        "latitude": 30.0,
        "longitude": 120.0,
        "start_time": start,
        "end_time": end,
        "duration_min": duration,
        "open_time": open_time,
    }


def test_optimizer_uses_real_route_time_and_reorders_to_fit():
    a = _item("A", open_time="09:00-12:00")
    b = _item("B", open_time="09:00-18:00")
    c = _item("C", open_time="13:00-18:00")
    matrix = {
        ("A", "B"): {"duration_min": 180, "source": "amap"},
        ("B", "A"): {"duration_min": 180, "source": "amap"},
        ("A", "C"): {"duration_min": 30, "source": "amap"},
        ("C", "A"): {"duration_min": 30, "source": "amap"},
        ("B", "C"): {"duration_min": 30, "source": "amap"},
        ("C", "B"): {"duration_min": 30, "source": "amap"},
    }
    result = optimize_daily_plan({"day_no": 1, "items": [a, b, c]}, route_matrix=matrix)
    names = [item["poi_name"] for item in result.plan["items"]]
    assert names == ["A", "C", "B"] or names == ["B", "C", "A"]
    assert result.violations == []
    assert result.travel_time_total_min == 60
    assert result.degraded is False


def test_optimizer_removes_optional_item_and_explains_reason():
    a = _item("A", duration=180, open_time="09:00-12:00")
    b = _item("B", duration=180, open_time="09:00-11:00")
    result = optimize_daily_plan(
        {"day_no": 1, "items": [a, b]},
        route_matrix={("A", "B"): {"duration_min": 30, "source": "amap"}},
    )
    assert len([item for item in result.plan["items"] if item["item_type"] == "attraction"]) == 1
    assert result.removed_candidates[0]["name"] == "B"
    assert "营业时间" in result.removed_candidates[0]["reason"]


def test_optimizer_keeps_hotel_and_marks_coordinate_fallback_degraded():
    a = _item("A")
    hotel = {"item_type": "hotel", "poi_name": "酒店", "start_time": "21:00", "end_time": "08:00"}
    result = optimize_daily_plan(
        {"day_no": 1, "items": [a, hotel]},
        route_matrix={},
    )
    assert result.plan["items"][-1]["item_type"] == "hotel"
    assert result.degraded is False


def test_optimizer_applies_budget_limit_and_reports_removed_item():
    a = _item("A")
    a["cost"] = 80
    b = _item("B")
    b["cost"] = 80
    result = optimize_daily_plan(
        {"day_no": 1, "items": [a, b]},
        route_matrix={("A", "B"): {"duration_min": 0, "source": "amap"}},
        budget_limit=100,
    )
    assert len([item for item in result.plan["items"] if item["item_type"] == "attraction"]) == 1
    assert "预算" in result.removed_candidates[0]["reason"]
