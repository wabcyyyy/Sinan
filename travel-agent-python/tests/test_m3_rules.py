"""M3（spec §8.3）共用规则、硬约束与局部修复的回归钉。

覆盖规格 §8.3 点名的 8 条行为 + 2 条 ConstraintReport 单元语义。被钉的常量与
判据全部来自产品代码单一真源（route_service / transfer_time / schedule_optimizer /
day_policy / constraint_checks / reflect / day_delivery_gate），本文件只读不改——
常量与断言按公式实算核对（数值漂移即红，属门禁只升不降）。
"""

from app.agent.data.route_service import MODE_SPEED_KMH, estimate_duration_minutes
from app.agent.generation.content.reflect import BUDGET_OVERAGE_MARK, validate_plans
from app.agent.generation.output.schedule_optimizer import (
    UNKNOWN_ROUTE_PENALTY_MIN,
    UNKNOWN_ROUTE_SOURCE,
    optimize_daily_plan,
)
from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import day_policy_for
from app.agent.generation.rules.generation_core import estimate_plans_total, has_double_lunch, meal_slot_of
from app.agent.generation.rules.transfer_time import estimate_transfer_minutes
from app.schemas.trip import DailyPlan, TripItem
from app.schemas.trip_requirements import (
    BudgetPolicy,
    DayWindow,
    LodgingRequirements,
    RequiredPlace,
    TripRequirements,
)
from app.services.day_delivery_gate import hard_delivery_blockers

# ---- §8.3 ①：mode-aware 速度分档（5km 步行估算明显长于驾驶）----


def test_walking_estimate_is_clearly_longer_than_driving_for_same_5km():
    """同一直线距离按交通档位分档估算：步行 4.5km/h 不得再误用车速。

    按常量实算（直线 5000m、绕行 1.35、缓冲 25%+10min）：
    - walking：5000×1.35/(4.5×1000/60)=90min → ceil(90×1.25+10)=123；
    - driving：5000×1.35/(32×1000/60)≈12.66min → ceil(12.66×1.25+10)=26。
    钉两件事：步行估算必须明显长于驾驶（>2 倍，钉住"步行时长被车速低估"
    的历史 bug 不复发）；精确值钉住常量本身（速度/缓冲任一漂移即红）。
    """
    assert MODE_SPEED_KMH["walking"] == 4.5
    assert MODE_SPEED_KMH["driving"] == 32.0
    walking = estimate_duration_minutes(5000, "walking")
    driving = estimate_duration_minutes(5000, "driving")
    assert walking > driving * 2
    assert walking == 123
    assert driving == 26


# ---- §8.3 ②：缺坐标是 unknown 不是 0 ----


def test_missing_coordinates_are_unknown_not_zero():
    """缺坐标/0-0 哨兵的路段按保守罚分参与排序，绝不当 0 分钟最优解。

    三层口径：
    - estimate_transfer_minutes：任一端缺坐标或 0/0 哨兵 → None（不猜路线）；
    - 全空矩阵 + 有坐标：走坐标估算（source=coordinate-estimate，degraded）；
    - 全空矩阵 + 无坐标：travel_total ≥ UNKNOWN_ROUTE_PENALTY_MIN 且
      route_sources 如实含 unknown-route；
    - 混合矩阵（A-B 真实 10min、B-C 无坐标）：含 B-C 边的排列被罚分，
      travel_total == 10+45 而非 10——unknown 边不免费。
    """
    missing = {"poi_name": "A", "latitude": None, "longitude": None}
    real = {"poi_name": "B", "latitude": 30.0, "longitude": 120.0}
    assert estimate_transfer_minutes(missing, real) is None
    assert estimate_transfer_minutes(real, missing) is None
    sentinel = {"poi_name": "C", "latitude": 0, "longitude": 0}
    assert estimate_transfer_minutes(sentinel, real) is None

    # 全空矩阵 + 有坐标：坐标估算源
    located_a = {"item_type": "attraction", "poi_id": "A", "poi_name": "A", "latitude": 30.0, "longitude": 120.0}
    located_b = {"item_type": "attraction", "poi_id": "B", "poi_name": "B", "latitude": 30.05, "longitude": 120.05}
    result = optimize_daily_plan({"day_no": 1, "items": [located_a, located_b]}, route_matrix={})
    assert result.route_sources == ["coordinate-estimate"]
    assert result.travel_time_total_min > 0
    assert result.degraded is True

    # 全空矩阵 + 无坐标：unknown 罚分，不是 0
    bare_a = {"item_type": "attraction", "poi_id": "A", "poi_name": "A", "duration_min": 60}
    bare_b = {"item_type": "attraction", "poi_id": "B", "poi_name": "B", "duration_min": 60}
    result = optimize_daily_plan({"day_no": 1, "items": [bare_a, bare_b]}, route_matrix={})
    assert UNKNOWN_ROUTE_SOURCE in result.route_sources
    assert result.travel_time_total_min >= UNKNOWN_ROUTE_PENALTY_MIN
    assert result.degraded is True

    # 混合：A-B 真实 10min、B-C 无坐标 → 含 B-C 边的排列 travel 被罚分
    with_coords_a = dict(bare_a, latitude=30.0, longitude=120.0)
    with_coords_b = dict(bare_b, latitude=30.01, longitude=120.0)
    no_coords_c = {"item_type": "attraction", "poi_id": "C", "poi_name": "C", "latitude": None, "longitude": None}
    matrix = {
        ("A", "B"): {"duration_min": 10, "source": "amap"},
        ("B", "A"): {"duration_min": 10, "source": "amap"},
    }
    mixed_plan = {"day_no": 1, "items": [with_coords_a, with_coords_b, no_coords_c]}
    result = optimize_daily_plan(mixed_plan, route_matrix=matrix)
    assert result.travel_time_total_min == 10 + UNKNOWN_ROUTE_PENALTY_MIN
    assert result.route_sources == ["amap", UNKNOWN_ROUTE_SOURCE]


# ---- §8.3 ③：午餐窗口与双午餐判据 ----


def test_meal_slots_lunch_window_and_double_lunch():
    """餐次按开始时间粗分：09:00 是早餐不是午餐；双午餐只看午窗（10:30-14:30）。

    09:00 一条 + 12:00 一条不算双午餐（一早一午）；两条都落在午窗才算。
    """
    assert meal_slot_of("09:00") != "lunch"
    assert meal_slot_of("09:00") == "breakfast"
    assert meal_slot_of("10:30") == "lunch"
    assert meal_slot_of("12:00") == "lunch"

    double = [
        {"item_type": "food", "poi_name": "午A", "start_time": "10:30"},
        {"item_type": "food", "poi_name": "午B", "start_time": "12:00"},
    ]
    assert has_double_lunch(double) is True
    mixed = [
        {"item_type": "food", "poi_name": "早", "start_time": "09:00"},
        {"item_type": "food", "poi_name": "午", "start_time": "12:00"},
    ]
    assert has_double_lunch(mixed) is False


# ---- §8.3 ④：两小时只容纳必去点时不能用两可选点替代 ----


def test_required_place_cannot_be_replaced_by_two_optional_items():
    """120 分钟日窗只装得下必去点：目标序第一键（硬违例优先）压过可选数量。

    构造：灵隐寺 120min + 可选 B/C 各 60min，day 10:00-12:00。若为凑数量选
    B+C，灵隐寺即成"必去点无法安排"违例；优化器必须选含灵隐寺的排列
    （violations 空且灵隐寺在 items），B/C 进 removed_candidates 说明原因。
    """
    lingyin = {
        "item_type": "attraction",
        "poi_id": "LY",
        "poi_name": "灵隐寺",
        "latitude": 30.0,
        "longitude": 120.0,
        "duration_min": 120,
    }
    optional_b = {
        "item_type": "attraction",
        "poi_id": "B",
        "poi_name": "B",
        "latitude": 30.0,
        "longitude": 120.0,
        "duration_min": 60,
    }
    optional_c = dict(optional_b, poi_id="C", poi_name="C")
    result = optimize_daily_plan(
        {"day_no": 1, "items": [optional_b, optional_c, lingyin]},
        route_matrix={},
        day_start="10:00",
        day_end="12:00",
        required_names={"灵隐寺"},
    )
    names = [item["poi_name"] for item in result.plan["items"]]
    assert result.violations == []
    assert "灵隐寺" in names
    assert "B" not in names and "C" not in names
    removed_names = {entry["name"] for entry in result.removed_candidates}
    assert {"B", "C"} <= removed_names


# ---- §8.3 ⑤：150 分钟到达日不被全天下限误杀 ----


def test_arrival_day_with_explicit_150min_window_is_not_flagged_thin():
    """到达日 16:00-18:30（150 分钟）按明确窗口验收，不套 240 分钟全天下限。

    - DayPolicy：kind=arrival、min_active_minutes=0、playable=True（150≥120）、
      max_daily_minutes 钳到 150、has_known_window=True；
    - validate_plans：一景点一餐共 120 分钟（低于全天下限 240）不产生
      "安排过稀"/"未安排任何景点"；
    - 同方案走 check_constraints：条目都在 16:00-18:30 窗口内 → 窗口 pass。
    """
    requirements = TripRequirements(
        day_windows=[DayWindow(day_no=1, kind="arrival", not_before="16:00", finish_by="18:30")]
    )
    policy = day_policy_for(requirements, 1)
    assert policy.kind == "arrival"
    assert policy.has_known_window is True
    assert policy.min_active_minutes == 0
    assert policy.max_daily_minutes == 150
    assert policy.playable is True

    plan = {
        "day_no": 1,
        "items": [
            {"item_type": "attraction", "poi_name": "湖滨步行街", "start_time": "16:00", "end_time": "17:00"},
            {"item_type": "food", "poi_name": "晚餐", "start_time": "17:30", "end_time": "18:30"},
        ],
    }
    issues, _log = validate_plans([plan], requirements=requirements)
    assert not any("安排过稀" in issue for issue in issues)
    assert not any("未安排任何景点" in issue for issue in issues)

    report = check_constraints(plan, requirements, 1, policy)
    window = next(check for check in report.checks if check.kind == "day_window")
    assert window.status == "pass"
    assert window.blocking is True and report.has_blocking is False


# ---- §8.3 ⑥：普通全天下限不变 ----


def test_normal_full_day_thresholds_unchanged_without_requirements():
    """无需求 = 普通全天，既有 240 分钟/6 景点基线原样保留（现状不回退）。

    - 2 景点共 120 分钟（< 240）→ 仍报"安排过稀"；
    - 8 个景点（> 6）→ 仍报"景点过多（8 个，上限 6）"。
    """
    thin = {
        "day_no": 1,
        "items": [
            {"item_type": "attraction", "poi_name": "A", "start_time": "09:00", "end_time": "10:00"},
            {"item_type": "attraction", "poi_name": "B", "start_time": "10:00", "end_time": "11:00"},
        ],
    }
    issues, _log = validate_plans([thin])
    assert any("安排过稀" in issue for issue in issues)

    crowded = {
        "day_no": 1,
        "items": [
            {"item_type": "attraction", "poi_name": f"P{index}", "start_time": "09:00", "duration_min": 10}
            for index in range(8)
        ],
    }
    issues, _log = validate_plans([crowded])
    assert any("景点过多（8 个，上限 6）" in issue for issue in issues)


# ---- §8.3 ⑦：修复不动已通过天（day_delivery_gate 轻量验证）----


def test_delivery_gate_passes_compliant_day_and_blocks_required_violation():
    """硬伤门禁双轨：合规天返回空（修复循环不空转）；必去违例天必须阻断。

    - 合规天（3 景点 1 餐、360 分钟、无冲突、无需求）→ blockers == []；
    - 需求指定第 2 天必去灵隐寺而 plan 无 → blockers 含"需求违例"
      （required_place），进入 PENDING 而非 SUCCEEDED。
    """
    compliant = DailyPlan(
        day_no=1,
        items=[
            TripItem(item_type="attraction", poi_name="花港观鱼", start_time="09:00", end_time="10:30"),
            TripItem(item_type="food", poi_name="楼外楼", start_time="10:30", end_time="12:00"),
            TripItem(item_type="attraction", poi_name="苏堤春晓", start_time="13:00", end_time="14:30"),
            TripItem(item_type="attraction", poi_name="曲院风荷", start_time="15:00", end_time="16:30"),
        ],
    )
    assert hard_delivery_blockers(1, compliant) == []

    requirements = TripRequirements(
        required_places=[RequiredPlace(constraint_id="req-lingyin", name="灵隐寺", day_no=2)]
    )
    violating = DailyPlan(
        day_no=2,
        items=[
            TripItem(item_type="attraction", poi_name="断桥残雪", start_time="09:00", end_time="11:00"),
            TripItem(item_type="food", poi_name="知味观", start_time="11:00", end_time="13:00"),
            TripItem(item_type="attraction", poi_name="雷峰塔", start_time="14:00", end_time="17:00"),
        ],
    )
    blockers = hard_delivery_blockers(2, violating, requirements=requirements)
    assert any("需求违例" in blocker and "灵隐寺" in blocker for blocker in blockers)


# ---- §8.3 ⑧：strict/target 预算区分与房间口径 ----


def test_budget_hard_cap_has_no_tolerance_while_target_keeps_8_percent():
    """hard_cap 是严格上限（ratio=0，超 1 元即报）；target 保留 8% 容差。

    构造估算恰好 105（门票 30 + 餐饮 40 + 交通 35，1 人 1 天）、budget=100：
    - hard_cap：over=5 > 100×0 → 有 BUDGET_OVERAGE_MARK issue；
    - target：over=5 ≤ 100×8% → 无预算 issue。
    """
    plans = [
        {
            "day_no": 1,
            "items": [
                {"item_type": "attraction", "poi_name": "A", "cost": 30, "duration_min": 60},
                {"item_type": "food", "poi_name": "F", "cost": 40, "start_time": "12:00", "duration_min": 60},
            ],
        }
    ]
    hard_cap = TripRequirements(budget_policy=BudgetPolicy(mode="hard_cap"))
    issues, _log = validate_plans(plans, budget=100, requirements=hard_cap)
    assert any(BUDGET_OVERAGE_MARK in issue for issue in issues)

    target = TripRequirements(budget_policy=BudgetPolicy(mode="target"))
    issues, _log = validate_plans(plans, budget=100, requirements=target)
    assert not any(BUDGET_OVERAGE_MARK in issue for issue in issues)


def test_rooms_requirement_replaces_persons_half_guess_for_hotel_scope():
    """用户明确给出 lodging.rooms 时酒店按该房数计，替代 ceil(persons/2) 猜测。

    构造 2 天各一家 100 元酒店（N-1 口径只计第 1 晚）、4 人：
    - rooms=None：room_n=ceil(4/2)=2 → 酒店=100×2=200；
    - rooms=1：room_n=1 → 酒店=100。
    """
    hotels = [
        {"day_no": 1, "items": [{"item_type": "hotel", "poi_name": "H", "cost": 100}]},
        {"day_no": 2, "items": [{"item_type": "hotel", "poi_name": "H", "cost": 100}]},
    ]
    guessed = estimate_plans_total(hotels, persons=4, days=2)
    explicit = estimate_plans_total(hotels, persons=4, days=2, rooms=1)
    assert guessed["酒店"] == 200.0
    assert explicit["酒店"] == 100.0
    assert explicit["合计"] < guessed["合计"]


# ---- ConstraintReport 单元：unknown 语义与 time_conflict ----


def test_constraint_report_unknown_semantics_never_blocks():
    """不可判的约束必须显式 unknown，不算 pass 也不阻断交付。

    - 词表外排除类别（"aquarium" 不在 museum 词表）→ status=unknown、
      blocking=False、进 unknown_checks；
    - 到达日未给明确窗口（只有 kind，用 14:00 预留估算标 estimated）→
      窗口检查 status=unknown、evidence=estimated-city-boundary、不阻断。
    """
    requirements = TripRequirements(
        excluded_categories=["aquarium"],
        day_windows=[DayWindow(day_no=1, kind="arrival")],
    )
    policy = day_policy_for(requirements, 1)
    assert policy.has_known_window is False
    report = check_constraints({"day_no": 1, "items": []}, requirements, 1, policy)

    category = next(check for check in report.checks if check.kind == "excluded_category")
    assert category.status == "unknown"
    assert category.blocking is False
    window = next(check for check in report.checks if check.kind == "day_window")
    assert window.status == "unknown"
    assert window.blocking is False
    assert "estimated-city-boundary" in window.evidence_refs
    assert {check.constraint_id for check in report.unknown_checks} >= {
        category.constraint_id,
        window.constraint_id,
    }
    assert report.blocking_checks == []
    assert report.has_blocking is False


def test_constraint_report_time_conflict_is_blocking_violation():
    """时间冲突是明确硬违例：status=violation、blocking=True、has_blocking 命中。

    注意与 transfer_time.fix_transfer_gaps 的分工：重叠对不由微调规则修，
    归本检查器拦截（blocking 进交付门）。
    """
    plan = {
        "day_no": 1,
        "items": [
            {"item_type": "attraction", "poi_name": "A", "start_time": "09:00", "end_time": "11:00"},
            {"item_type": "attraction", "poi_name": "B", "start_time": "10:00", "end_time": "12:00"},
        ],
    }
    report = check_constraints(plan, None, 1, day_policy_for(None, 1))
    conflict = next(check for check in report.checks if check.kind == "time_conflict")
    assert conflict.status == "violation"
    assert conflict.blocking is True
    assert conflict.item_refs == ["A→B"]
    assert report.has_blocking is True
    assert conflict in report.blocking_checks


# ---- 补充钉：DayPolicy 派生的其余口径（relaxed 上限 / 未知窗口预留 / 不可玩豁免）----


def test_day_policy_relaxed_cap_and_default_transit_windows():
    """DayPolicy 其余口径钉：慢游只降景点上限；未知到离窗口用预留估算。

    - pace=relaxed：max_attractions 6→4，时长阈值不动（不全局降标）；
    - departure 未给窗口：end 取 18:00 预留（DEPARTURE_DEFAULT_END_MIN）、
      has_known_window=False（硬保证只针对用户明确窗口）；
    - 到达日只给 16:00 起、未给结束：可用窗按全天边界折算 480 分钟，
      playable=True；若窗口可用 < 120 分钟 → playable=False（无景点合法）。
    """
    relaxed = day_policy_for(TripRequirements(pace="relaxed"), 2)
    assert relaxed.max_attractions == 4
    assert relaxed.max_daily_minutes == 480
    assert relaxed.min_active_minutes == 240

    departure_unknown = day_policy_for(TripRequirements(day_windows=[DayWindow(day_no=1, kind="departure")]), 1)
    assert departure_unknown.has_known_window is False
    assert departure_unknown.window_end_min == 18 * 60
    assert departure_unknown.min_active_minutes == 0

    # 单边到达窗（规格意图）：只给到达钟点 → 可玩到当日 24:00（时长上限是分钟数
    # 不是钟点，不能拿 480 当 08:00 减）。16:00 落地 → 可用 480 分钟、playable。
    arrival_open_end = day_policy_for(
        TripRequirements(day_windows=[DayWindow(day_no=1, kind="arrival", not_before="16:00")]), 1
    )
    assert arrival_open_end.has_known_window is True
    assert arrival_open_end.playable is True
    assert arrival_open_end.max_daily_minutes == 480

    cramped = day_policy_for(
        TripRequirements(day_windows=[DayWindow(day_no=1, kind="arrival", not_before="17:00", finish_by="18:00")]), 1
    )
    assert cramped.playable is False


def test_lodging_and_full_trip_required_place_shape_roundtrip():
    """需求结构契约钉：全程必去（day_no=None）与 lodging.rooms 随 schema 保真。

    check_constraints 单天视角对全程必去（day_no=None）判 not_applicable
    （整趟检查负责），不误报当日违例。
    """
    requirements = TripRequirements(
        required_places=[RequiredPlace(constraint_id="rq-whole", name="西湖", day_no=None)],
        lodging=LodgingRequirements(rooms=2),
    )
    assert requirements.lodging is not None and requirements.lodging.rooms == 2
    plan = {
        "day_no": 1,
        "items": [{"item_type": "attraction", "poi_name": "断桥残雪", "start_time": "09:00", "end_time": "11:00"}],
    }
    report = check_constraints(plan, requirements, 1, day_policy_for(requirements, 1))
    required = next(check for check in report.checks if check.kind == "required_place")
    assert required.status == "not_applicable"
    assert required.blocking is True and report.has_blocking is False
