from app.agent.generation.content.reflect import (
    build_feedback,
    estimate_transfer_minutes,
    parse_time,
    validate_plans,
)


def test_parse_time():
    assert parse_time("09:00") == 540
    assert parse_time("23:59") == 1439
    assert parse_time("") is None
    assert parse_time(None) is None
    assert parse_time("abc") is None


def test_validate_ok():
    plans = [
        {
            "day_no": 1,
            "items": [
                {"item_type": "attraction", "poi_name": "A", "start_time": "09:00", "end_time": "11:30"},
                {"item_type": "attraction", "poi_name": "B", "start_time": "13:30", "end_time": "16:00"},
                {"item_type": "food", "poi_name": "C", "start_time": "18:00", "end_time": "19:00"},
                {"item_type": "hotel", "poi_name": "酒店", "start_time": "21:00", "end_time": "08:00"},
            ],
        }
    ]
    issues, log = validate_plans(plans)
    assert issues == []
    assert len(log) == 1


def test_validate_flags_day_without_attractions():
    """只有酒店/餐饮没有景点的天必须进校验问题（丽江占位酒店事故缺口）。"""
    plans = [
        {
            "day_no": 1,
            "items": [
                {"item_type": "food", "poi_name": "餐厅", "start_time": "12:00", "end_time": "13:00"},
                {"item_type": "hotel", "poi_name": "酒店", "start_time": "21:00", "end_time": "08:00"},
            ],
        },
        {"day_no": 2, "items": []},  # 完全空日仍走"无行程项"日志语义，不重复报 issue
    ]
    issues, _log = validate_plans(plans)
    assert issues == ["第 1 天未安排任何景点"]


def test_validate_conflict_and_open_time():
    plans = [
        {
            "day_no": 1,
            "items": [
                {"item_type": "attraction", "poi_name": "A", "start_time": "09:00", "end_time": "12:00"},
                {"item_type": "attraction", "poi_name": "B", "start_time": "11:00", "end_time": "13:00"},
                {
                    "item_type": "attraction",
                    "poi_name": "C",
                    "start_time": "19:00",
                    "end_time": "20:00",
                    "open_time": "08:00-17:00",
                },
            ],
        }
    ]
    issues, _ = validate_plans(plans)
    assert any("时间冲突" in i for i in issues)
    assert any("开放时间不符" in i for i in issues)


def test_validate_requires_full_opening_window():
    plans = [
        {
            "day_no": 1,
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "闭馆前进不去",
                    "start_time": "16:00",
                    "end_time": "18:00",
                    "open_time": "09:00-17:00",
                }
            ],
        }
    ]
    issues, _ = validate_plans(plans)
    assert any("开放时间不符" in i for i in issues)


def test_route_constraint_uses_coordinates_and_reports_tolerant_gap():
    far = [
        {
            "day_no": 1,
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "远点A",
                    "start_time": "09:00",
                    "end_time": "10:00",
                    "latitude": 30.0,
                    "longitude": 120.0,
                },
                {
                    "item_type": "attraction",
                    "poi_name": "远点B",
                    "start_time": "10:30",
                    "end_time": "12:00",
                    "latitude": 30.2,
                    "longitude": 120.0,
                },
            ],
        }
    ]
    issues, _ = validate_plans(far)
    assert any("路线时间不足" in i for i in issues)
    assert estimate_transfer_minutes(far[0]["items"][0], far[0]["items"][1]) > 30

    close = [
        {
            "day_no": 1,
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "近点A",
                    "start_time": "09:00",
                    "end_time": "10:00",
                    "latitude": 30.0,
                    "longitude": 120.0,
                },
                {
                    "item_type": "attraction",
                    "poi_name": "近点B",
                    "start_time": "10:20",
                    "end_time": "12:00",
                    "latitude": 30.005,
                    "longitude": 120.005,
                },
            ],
        }
    ]
    close_issues, _ = validate_plans(close)
    assert not any("路线时间不足" in i for i in close_issues)


def test_validate_saturation_hotel_excluded():
    plans = [
        {
            "day_no": 1,
            "items": [
                {"item_type": "attraction", "poi_name": "A", "start_time": "09:00", "end_time": "11:30"},
                {"item_type": "attraction", "poi_name": "B", "start_time": "13:30", "end_time": "16:00"},
                {"item_type": "attraction", "poi_name": "C", "start_time": "19:00", "end_time": "20:30"},
                {"item_type": "food", "poi_name": "D", "start_time": "18:00", "end_time": "19:00"},
                {
                    "item_type": "hotel",
                    "poi_name": "酒店",
                    "start_time": "21:00",
                    "end_time": "08:00",
                    "duration_min": 660,
                },
            ],
        }
    ]
    issues, _ = validate_plans(plans)
    assert issues == []


def test_build_feedback():
    issues = ["第 1 天时间冲突：A 与 B 重叠", "第 1 天行程过满"]
    feedback = build_feedback(issues)
    assert "A 与 B" in feedback and "行程过满" in feedback


# ---------- P2 逐日兜底空天：坐标估算源判定容差（2026-10-05） ----------


def _clock(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _route_pair(gap_min: int) -> list[dict]:
    """相邻两景点坐标相同：坐标估算需求恒为下限 17 分钟（ceil(5×1.25+10)）。"""
    end = 11 * 60
    return [
        {
            "day_no": 1,
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "A",
                    "start_time": "09:00",
                    "end_time": _clock(end),
                    "latitude": 30.0,
                    "longitude": 120.0,
                },
                {
                    "item_type": "attraction",
                    "poi_name": "B",
                    "start_time": _clock(end + gap_min),
                    "end_time": _clock(end + gap_min + 60),
                    "latitude": 30.0,
                    "longitude": 120.0,
                },
            ],
        }
    ]


def test_coordinate_estimate_small_deficit_no_longer_flags():
    """坐标估算含保守缓冲（下限 17 分钟），1-2 分钟级裕量差不再判不足。

    旧预期为什么是错的：夜审 P2 实录「留 15/20 分钟、按坐标估需 17/21 分钟」
    即整日退回 PENDING，逐日兜底在 json_object 降级档下间歇产出空天——用保守
    估算的 1-2 分钟超额退回整日，代价收益不成比。估算源给容差后此类路段放行。
    """
    issues, _log = validate_plans(_route_pair(15))
    assert not any("路线时间不足" in issue for issue in issues)


def test_coordinate_estimate_large_deficit_still_flags():
    issues, _log = validate_plans(_route_pair(10))
    assert any("路线时间不足" in issue for issue in issues)
    assert any("坐标估算" in issue for issue in issues)


def test_real_route_source_stays_strict():
    """真实路线源不走容差：测量口径差 1 分钟也是真不足，判定维持原样。"""
    matrix = {("A", "B"): {"duration_min": 17, "source": "route-service"}}
    issues, _log = validate_plans(_route_pair(15), route_matrix=matrix)
    assert any("路线时间不足" in issue for issue in issues)
    assert any("真实路线" in issue for issue in issues)
