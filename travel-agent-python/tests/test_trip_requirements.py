"""M1a 需求单一真源单测：TripRequirements/IntakeState/patch 应用/规范化序列化/生成指纹。

纯函数单测（无 DB、无 LLM），风格对齐 tests/test_generation_core.py。
已知输入的手工 v1 哈希对照钉住 legacy 指纹口径；canonical/patch 断言逐字钉产品行为。
"""

import hashlib
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.requirement_patches import RequirementPatch, apply_intake_patches
from app.schemas.trip import GenerateRequest
from app.schemas.trip_requirements import (
    MAX_TRIP_DAYS,
    SUPPORTED_EXCLUDED_CATEGORIES,
    BudgetPolicy,
    DayWindow,
    IntakeState,
    RequiredPlace,
    TripRequirements,
    canonical_requirements_payload,
    intake_state_issues,
    requirements_issues,
)
from app.services.generation_gate import request_fingerprint
from app.services.itinerary_generation import GenerateCommand

# ---------- 词表与常量 ----------


def test_supported_vocabulary_and_max_days():
    """词表与上限常量的当前口径：首期排除类别只支持 museum，单次行程上限 7 天。"""
    assert SUPPORTED_EXCLUDED_CATEGORIES == ("museum",)
    assert MAX_TRIP_DAYS == 7


# ---------- requirements_issues：确定性校验 ----------


def test_requirements_issues_duplicate_window_same_day():
    """同一天出现两个时间窗口 = 冲突：逐字报告哪一天重复。"""
    req = TripRequirements(day_windows=[DayWindow(day_no=1), DayWindow(day_no=1, kind="arrival")])
    assert requirements_issues(req) == ["第 1 天存在重复的时间窗口"]


def test_requirements_issues_inverted_window():
    """窗口倒置（not_before >= finish_by）必须报出；起止相等也算倒置。"""
    req = TripRequirements(day_windows=[DayWindow(day_no=2, not_before="18:00", finish_by="09:00")])
    assert requirements_issues(req) == ["第 2 天时间窗口倒置（不早于 18:00 但须在 09:00 前结束）"]
    equal = TripRequirements(day_windows=[DayWindow(day_no=1, not_before="09:00", finish_by="09:00")])
    assert requirements_issues(equal) == ["第 1 天时间窗口倒置（不早于 09:00 但须在 09:00 前结束）"]


def test_requirements_issues_day_no_beyond_days():
    """带 days 参数才查越界：窗口与必去指向不存在的天逐条报（顺序 = 窗口先、必去后）。"""
    req = TripRequirements(
        day_windows=[DayWindow(day_no=5)],
        required_places=[RequiredPlace(constraint_id="place-1", name="灵隐寺", day_no=6)],
    )
    assert requirements_issues(req, days=3) == [
        "时间窗口指向第 5 天，超出行程天数 3",
        "必去地点「灵隐寺」指定第 6 天，超出行程天数 3",
    ]


def test_requirements_issues_duplicate_constraint_id():
    """constraint_id 是必去约束的身份：重复即冲突，报出重复的 id 本身。"""
    req = TripRequirements(
        required_places=[
            RequiredPlace(constraint_id="place-1", name="西湖"),
            RequiredPlace(constraint_id="place-1", name="灵隐寺"),
        ]
    )
    assert requirements_issues(req) == ["必去地点约束重复：place-1"]


def test_requirements_issues_unsupported_category_rejected_museum_ok():
    """排除类别词表：bar 词表外报问题；museum 词表内合法。"""
    req = TripRequirements(excluded_categories=["bar"])
    assert requirements_issues(req) == ["暂不支持排除类别「bar」，当前支持：museum"]
    assert requirements_issues(TripRequirements(excluded_categories=["museum"])) == []


def test_requirements_issues_all_default_is_clean():
    """全默认结构 = 无额外约束：问题列表为空（等价于旧行为）。"""
    assert requirements_issues(TripRequirements()) == []


# ---------- apply_intake_patches：patch 的确定性应用 ----------


def test_set_days_eight_kept_not_clamped():
    """收集阶段不做上限截断：「想玩 8 天」的原值保留给出口协商，不截成 7。"""
    state = IntakeState(days=3)
    rejected = apply_intake_patches(state, [RequirementPatch(op="set", target="days", value=8)])
    assert rejected == []
    assert state.days == 8
    assert state.requirements.unresolved_requests == []


def test_intake_state_days_eight_valid_and_consistent():
    """IntakeState.days 刻意无上限：8 天的收集状态可构造且整体一致性校验为空。"""
    state = IntakeState(days=8)
    assert state.days == 8
    assert intake_state_issues(state) == []


def test_generate_request_days_eight_rejected_by_contract():
    """生成入口的 days le=7 在 pydantic 契约层拒绝（与收集阶段无上限形成两端口径）。"""
    with pytest.raises(ValidationError) as exc_info:
        GenerateRequest(city="杭州", days=8)
    error = exc_info.value.errors()[0]
    assert error["loc"] == ("days",)
    assert error["type"] == "less_than_equal"


def test_set_required_place_replaces_day_constraint_no_conflict_copy():
    """set 是替换语义：同名必去「改成第二天」只改日约束，不留下 day_no=1 的冲突副本。"""
    state = IntakeState()
    apply_intake_patches(state, [RequirementPatch(op="add", target="required_place", name="灵隐寺", day_no=1)])
    apply_intake_patches(state, [RequirementPatch(op="set", target="required_place", name="灵隐寺", day_no=2)])
    places = state.requirements.required_places
    assert len(places) == 1
    assert places[0].name == "灵隐寺"
    assert places[0].day_no == 2
    assert places[0].constraint_id == "place-1"


def test_unset_budget_policy_mode_clears_whole_policy():
    """「预算不限制了」：unset budget_policy_mode 把 budget_policy 整体置 None，不残留半截口径。"""
    state = IntakeState()
    state.requirements.budget_policy = BudgetPolicy(mode="hard_cap", include_intercity_transport=False)
    rejected = apply_intake_patches(state, [RequirementPatch(op="unset", target="budget_policy_mode")])
    assert rejected == []
    assert state.requirements.budget_policy is None


def test_scalar_set_on_requirements_field_pace():
    """标量 set 走 TripRequirements 自有字段（pace）：值落地、零拒绝。"""
    state = IntakeState()
    rejected = apply_intake_patches(state, [RequirementPatch(op="set", target="pace", value="relaxed")])
    assert rejected == []
    assert state.requirements.pace == "relaxed"


def test_add_then_remove_collection_targets_roundtrip():
    """add/remove 覆盖四个集合目标：preferences 落 state，其余三样落 requirements，remove 按名清掉。"""
    state = IntakeState()
    rejected = apply_intake_patches(
        state,
        [
            RequirementPatch(op="add", target="preferences", name="亲子"),
            RequirementPatch(op="add", target="excluded_place", name="宋城"),
            RequirementPatch(op="add", target="excluded_category", name="museum"),
            RequirementPatch(op="add", target="required_place", name="西湖", day_no=1),
        ],
    )
    assert rejected == []
    assert state.preferences == ["亲子"]
    assert state.requirements.excluded_places == ["宋城"]
    assert state.requirements.excluded_categories == ["museum"]
    assert [(p.name, p.day_no) for p in state.requirements.required_places] == [("西湖", 1)]

    rejected = apply_intake_patches(
        state,
        [
            RequirementPatch(op="remove", target="preferences", name="亲子"),
            RequirementPatch(op="remove", target="excluded_place", name="宋城"),
            RequirementPatch(op="remove", target="excluded_category", name="museum"),
            RequirementPatch(op="remove", target="required_place", name="西湖"),
        ],
    )
    assert rejected == []
    assert state.preferences == []
    assert state.requirements.excluded_places == []
    assert state.requirements.excluded_categories == []
    assert state.requirements.required_places == []


def test_unsupported_category_add_rejected_into_unresolved():
    """词表外类别不静默丢弃也不落地：拒绝原话进 unresolved_requests 留协商依据。"""
    state = IntakeState()
    rejected = apply_intake_patches(state, [RequirementPatch(op="add", target="excluded_category", name="bar")])
    assert rejected == ["add excluded_category bar：暂不支持排除类别，当前支持：museum"]
    assert state.requirements.excluded_categories == []
    assert state.requirements.unresolved_requests == rejected


def test_wrong_value_type_rejected_keeps_old_value():
    """值类型守卫不猜测转换：days="八" 被拒、原值保持、原话进 unresolved。"""
    state = IntakeState(days=3)
    rejected = apply_intake_patches(state, [RequirementPatch(op="set", target="days", value="八")])
    assert rejected == ["set days：值类型不符合该字段要求"]
    assert state.days == 3
    assert state.requirements.unresolved_requests == rejected


def test_state_base_scalar_set_and_unset():
    """基础槽位 patch 写的是 IntakeState 自身字段（不是 TripRequirements）：
    set budget/city 落 state；unset budget 清空（「预算不限制了」）。"""
    state = IntakeState()
    rejected = apply_intake_patches(
        state,
        [
            RequirementPatch(op="set", target="budget", value=3000),
            RequirementPatch(op="set", target="city", value="  杭州 "),
        ],
    )
    assert rejected == []
    assert state.budget == 3000
    assert state.city == "杭州"
    apply_intake_patches(state, [RequirementPatch(op="unset", target="budget")])
    assert state.budget is None
    assert state.city == "杭州", "unset 只清目标字段"


def test_lodging_scalar_targets_write_nested_struct():
    """住宿目标写进 TripRequirements.lodging 嵌套结构；unset 整体置 None 不残留。"""
    state = IntakeState()
    apply_intake_patches(
        state,
        [
            RequirementPatch(op="set", target="lodging_rooms", value=2),
            RequirementPatch(op="set", target="lodging_locked_hotel", value="西湖国宾馆"),
        ],
    )
    assert state.requirements.lodging is not None
    assert state.requirements.lodging.rooms == 2
    assert state.requirements.lodging.locked_hotel_identity == "西湖国宾馆"
    apply_intake_patches(state, [RequirementPatch(op="unset", target="lodging_rooms")])
    assert state.requirements.lodging is None, "unset 后嵌套结构整体清空"


def test_scalar_bounds_guarded():
    """界限守卫：persons=99 出界被拒原值保持；lodging_rooms=0 出界被拒。"""
    state = IntakeState(persons=2)
    rejected = apply_intake_patches(
        state,
        [
            RequirementPatch(op="set", target="persons", value=99),
            RequirementPatch(op="set", target="lodging_rooms", value=0),
        ],
    )
    assert len(rejected) == 2
    assert state.persons == 2
    assert state.requirements.lodging is None


def test_unresolved_requests_capped_at_twenty_keeping_newest():
    """unresolved 有界：超 20 条截最旧、保留最新协商依据。"""
    state = IntakeState()
    patches = [RequirementPatch(op="add", target="excluded_category", name=f"bar{n}") for n in range(25)]
    apply_intake_patches(state, patches)
    unresolved = state.requirements.unresolved_requests
    assert len(unresolved) == 20
    assert len(set(unresolved)) == 20, "截断保序去重不发生：25 条各不相同，留下的 20 条也应各不相同"
    assert unresolved[0].startswith("add excluded_category bar5"), "最旧的 bar0-bar4 被截掉"
    assert unresolved[-1].startswith("add excluded_category bar24"), "最新的 bar24 保留"


# ---------- canonical_requirements_payload：指纹用的确定性序列化 ----------


def test_canonical_none_and_all_default_both_empty():
    """指纹口径：None（未携带）与全默认结构都规范化为空对象——两者等价。"""
    assert canonical_requirements_payload(None) == {}
    assert canonical_requirements_payload(TripRequirements()) == {}


def test_canonical_ignores_element_order_within_sets():
    """LLM 输出顺序抖动不改变规范化结果：day_windows 按天、必去按 constraintId、排除集合字典序。"""
    day_windows = [DayWindow(day_no=2, kind="departure", finish_by="18:00"), DayWindow(day_no=1)]
    req_a = TripRequirements(
        day_windows=list(day_windows),
        required_places=[
            RequiredPlace(constraint_id="place-1", name="西湖", day_no=1),
            RequiredPlace(constraint_id="place-2", name="灵隐寺"),
        ],
        excluded_places=["宋城", "雷峰塔"],
    )
    req_b = TripRequirements(
        day_windows=[DayWindow(day_no=1), DayWindow(day_no=2, kind="departure", finish_by="18:00")],
        required_places=[
            RequiredPlace(constraint_id="place-2", name="灵隐寺"),
            RequiredPlace(constraint_id="place-1", name="西湖", day_no=1),
        ],
        excluded_places=["雷峰塔", "宋城"],
    )
    payload_a = canonical_requirements_payload(req_a)
    payload_b = canonical_requirements_payload(req_b)
    assert payload_a == payload_b
    assert payload_a != {}
    assert [w["dayNo"] for w in payload_a["dayWindows"]] == [1, 2], "day_windows 排序后按天保序"
    assert [p["constraintId"] for p in payload_a["requiredPlaces"]] == ["place-1", "place-2"]
    assert payload_a["excludedPlaces"] == sorted(["宋城", "雷峰塔"]), "排除地点按字典序归一"


def test_canonical_distinguishes_semantic_change():
    """语义变化必须改变规范化结果：同一天的 finish_by 不同即不同 dict。"""
    req_a = TripRequirements(day_windows=[DayWindow(day_no=1, finish_by="18:00")])
    req_b = TripRequirements(day_windows=[DayWindow(day_no=1, finish_by="20:00")])
    assert canonical_requirements_payload(req_a) != canonical_requirements_payload(req_b)


# ---------- request_fingerprint：双口径生成参数指纹 ----------


def _command(**overrides: Any) -> GenerateCommand:
    """指纹基准命令：字段覆盖用 overrides，其余取已知定值。"""
    kwargs: dict[str, Any] = dict(
        city="杭州",
        days=3,
        persons=2,
        stay_nights=2,
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 3),
        budget=Decimal("2000"),
        preferences=["亲子", "美食"],
        hotel_tier="舒适型",
        intent="带娃慢游西湖",
        requirements="不赶路，午休",
        origin_city="上海",
    )
    kwargs.update(overrides)
    return GenerateCommand(**kwargs)


def test_legacy_fingerprint_matches_manual_v1_nine_segment_hash():
    """① legacy 口径 = v1 九段旧哈希：与手工拼段的 sha256 逐字节一致（存量恢复不 409 的根基）。"""
    command = _command(legacy_fingerprint=True)
    # 九段：city|days|persons|stay_nights|budget|start|end|hotel_tier|preferences；
    # budget 经 0.01 标度归一后 str(Decimal("2000.00"))="2000.00"，日期是 ISO 字符串。
    v1_plain = "杭州|3|2|2|2000.00|2026-10-01|2026-10-03|舒适型|亲子,美食"
    expected = hashlib.sha256(v1_plain.encode("utf-8")).hexdigest()
    assert request_fingerprint(command) == expected
    # v1 九段之外的字段不参与：改 intent、带结构化需求都不动 legacy 指纹
    assert request_fingerprint(_command(legacy_fingerprint=True, intent="换成纯逛街")) == expected
    struct = TripRequirements(required_places=[RequiredPlace(constraint_id="place-1", name="灵隐寺", day_no=2)])
    assert request_fingerprint(_command(legacy_fingerprint=True, requirements_struct=struct)) == expected


def test_budget_decimal_scale_normalized():
    """② Decimal("2000") 与 Decimal("2000.00") 同指纹：请求侧与 DB 回读侧的标度差不再打成 409。"""
    assert request_fingerprint(_command(budget=Decimal("2000"))) == request_fingerprint(
        _command(budget=Decimal("2000.00"))
    )
    # legacy 口径同样归一：v1 的 budget 段也是 quantize 后的形状
    assert request_fingerprint(_command(budget=Decimal("2000"), legacy_fingerprint=True)) == request_fingerprint(
        _command(budget=Decimal("2000.00"), legacy_fingerprint=True)
    )


def test_same_struct_different_budget_scale_same_fingerprint():
    """③ 结构化需求相同、预算标度不同的两次构建指纹一致；且结构真实参与指纹（带上即变）。"""
    struct = TripRequirements(
        day_windows=[DayWindow(day_no=1, kind="arrival", not_before="14:00")],
        required_places=[RequiredPlace(constraint_id="place-1", name="灵隐寺", day_no=2)],
    )
    first = request_fingerprint(_command(budget=Decimal("2000"), requirements_struct=struct))
    second = request_fingerprint(_command(budget=Decimal("2000.00"), requirements_struct=struct))
    assert first == second
    assert first != request_fingerprint(_command(budget=Decimal("2000"))), "带非默认结构与不带必不等"


def test_semantic_requirement_changes_alter_fingerprint():
    """④ 到达窗口 / 必去指定日 / 出发地 / 意图任一变化都改变 v2 指纹（完整输入身份）。"""
    base_struct = TripRequirements(
        day_windows=[DayWindow(day_no=1, kind="arrival", finish_by="23:00")],
        required_places=[RequiredPlace(constraint_id="place-1", name="灵隐寺", day_no=2)],
    )
    base = request_fingerprint(_command(requirements_struct=base_struct))

    window_changed = base_struct.model_copy(deep=True)
    window_changed.day_windows[0].finish_by = "22:00"
    assert request_fingerprint(_command(requirements_struct=window_changed)) != base, "改当天窗口改变指纹"

    place_changed = base_struct.model_copy(deep=True)
    place_changed.required_places[0].day_no = 3
    assert request_fingerprint(_command(requirements_struct=place_changed)) != base, "改必去指定日改变指纹"

    origin_changed = _command(requirements_struct=base_struct, origin_city="南京")
    assert request_fingerprint(origin_changed) != base, "改出发地改变指纹"
    intent_changed = _command(requirements_struct=base_struct, intent="纯逛街不进景点")
    assert request_fingerprint(intent_changed) != base, "改意图改变指纹"


def test_all_default_struct_equals_none_fingerprint():
    """⑤ 全默认 struct 与 struct=None 指纹相同：canonical(exclude_defaults) 下两者都是空对象。"""
    assert request_fingerprint(_command(requirements_struct=TripRequirements())) == request_fingerprint(
        _command(requirements_struct=None)
    )


def test_legacy_and_v2_fingerprints_differ_for_same_input():
    """⑥ 双口径值域天然不相交：同输入 legacy 与 v2 指纹不同（"v2|" 前缀 + 附加段）。"""
    assert request_fingerprint(_command(legacy_fingerprint=True)) != request_fingerprint(_command())


def test_dict_form_with_camel_struct_matches_model_fingerprint():
    """⑦ dict（含 camelCase requirementsStruct）与等值模型指纹相同：dict 重建路径与模型同口径。"""
    struct = TripRequirements(
        day_windows=[DayWindow(day_no=1, kind="arrival", not_before="14:00", finish_by="23:00")],
        required_places=[RequiredPlace(constraint_id="place-1", name="灵隐寺", day_no=2)],
        excluded_places=["宋城"],
        pace="relaxed",
    )
    command = _command(requirements_struct=struct)
    as_dict = {
        "city": "杭州",
        "days": 3,
        "persons": 2,
        "stayNights": 2,
        "budget": Decimal("2000"),
        "startDate": "2026-10-01",
        "endDate": "2026-10-03",
        "hotelTier": "舒适型",
        "preferences": ["亲子", "美食"],
        "intent": "带娃慢游西湖",
        "requirements": "不赶路，午休",
        "origin_city": "上海",
        "requirementsStruct": {
            "dayWindows": [{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "23:00"}],
            "requiredPlaces": [{"constraintId": "place-1", "name": "灵隐寺", "dayNo": 2}],
            "excludedPlaces": ["宋城"],
            "pace": "relaxed",
        },
    }
    assert request_fingerprint(as_dict) == request_fingerprint(command)
