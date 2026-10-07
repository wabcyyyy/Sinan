"""转场时间判据与生成后微调规则（rules/transfer_time）的单测。

两面都要钉住：
- 应触发：相邻点位留白低于判据（留白 + 容差 < 估算换乘）时，微调后必须达到
  validate_plans 的同一判官口径，且不产生新的重叠/越界；
- 不应触发：判官放行的对（容差内）、缺坐标 / 0-0 哨兵、重叠对、非
  attraction/food 条目、时刻不可解析的条目，一律原样不动。
"""

import copy

from app.agent.generation.content.reflect import validate_plans
from app.agent.generation.rules.transfer_time import (
    MIN_VISIT_MINUTES,
    estimate_transfer_minutes,
    fix_transfer_gaps,
    normalize_item_clocks,
)

SAME_COORD = (30.25, 120.15)


def _item(name, start, end, *, coord=SAME_COORD, **extra):
    row = {
        "item_type": "attraction",
        "poi_name": name,
        "latitude": coord[0],
        "longitude": coord[1],
        "start_time": start,
        "end_time": end,
    }
    row.update(extra)
    return row


def _by_name(items, name):
    return next(item for item in items if item["poi_name"] == name)


def test_same_coord_transfer_is_the_shared_judge_floor():
    """同坐标两点的要求换乘 = ceil(5×1.25 + 10) = 17 分钟：判据下沉后的钉子。"""
    assert estimate_transfer_minutes(_item("A", "09:00", "10:00"), _item("B", "10:00", "11:00")) == 17


def test_fix_trims_previous_visit_and_syncs_duration():
    """留白 5 分钟、需 17 分钟：先收缩前一条目 7 分钟（保底 30 之上），后条不动。"""
    items = [_item("A", "09:00", "10:00"), _item("B", "10:05", "12:00")]
    assert fix_transfer_gaps(items) == 1
    fixed_a = _by_name(items, "A")
    assert fixed_a["end_time"] == "09:53", "收缩 7 分钟后留白 = 17 - 5(容差) 达到判官口径"
    assert fixed_a["duration_min"] == 53, "duration 跟随时间窗（day_stream 同一口径）"
    assert _by_name(items, "B")["start_time"] == "10:05", "够收缩时不惊动后续条目"
    issues, _log = validate_plans([{"day_no": 1, "items": items}])
    assert not any("路线时间不足" in issue for issue in issues), f"修复后判官必须放行：{issues}"


def test_fix_shifts_suffix_when_trim_is_capped():
    """前条只剩 5 分钟可收：余量整段顺延后续条目，段内间隔不变、无新重叠。

    A→B 缺口 12（可收 5 + 顺延 7）；顺延后 B→C 贴成 0 间隔再触发一轮，
    B 自己收缩补足——两对各自达标，互不回退。
    """
    items = [
        _item("A", "09:00", "09:35"),
        _item("B", "09:35", "11:00"),
        _item("C", "11:00", "12:00"),
    ]
    assert fix_transfer_gaps(items) == 2
    a, b, c = (_by_name(items, name) for name in "ABC")
    assert a["end_time"] == "09:30", "先收缩到保底 30 分钟"
    assert (b["start_time"], b["end_time"]) == ("09:42", "10:55"), "顺延 7 分钟后再为 B→C 自收缩 12 分钟"
    assert (c["start_time"], c["end_time"]) == ("11:07", "12:07"), "C 随整段顺延，时长不变"
    assert a["duration_min"] == 30 and b["duration_min"] == 73
    assert c.get("duration_min") is None, "平移不改时长，也不新造 duration_min 字段"
    issues, _log = validate_plans([{"day_no": 1, "items": items}])
    assert not any("路线时间不足" in issue for issue in issues), f"修复后判官必须放行：{issues}"


def test_fix_respects_open_window_when_shifting():
    """顺延不得把条目推出 open_time 关窗：窗口紧时只顺延到关窗为止。"""
    items = [
        _item("A", "09:00", "09:35"),
        _item("B", "09:40", "09:58", open_time="09:00-10:00"),
    ]
    assert fix_transfer_gaps(items) == 1
    b = _by_name(items, "B")
    assert (b["start_time"], b["end_time"]) == ("09:42", "10:00"), "顺延 2 分钟后正好压线关窗，不越界"
    issues, _log = validate_plans([{"day_no": 1, "items": items}])
    assert not any("路线时间不足" in issue or "开放时间不符" in issue for issue in issues), f"{issues}"


def test_fix_counts_zero_when_bounds_make_it_unfixable():
    """关窗卡死顺延、前条已在保底：修不动就如实返回 0，只做不越界的部分顺延。"""
    items = [
        _item("A", "09:00", "09:35"),
        _item("B", "09:40", "09:58", open_time="09:00-09:59"),
    ]
    assert fix_transfer_gaps(items) == 0
    b = _by_name(items, "B")
    assert b["start_time"] == "09:41", "窗口只剩 1 分钟就只顺延 1 分钟，宁缺毋越"
    issues, _log = validate_plans([{"day_no": 1, "items": items}])
    assert any("路线时间不足" in issue for issue in issues), "修不掉的原样交给判官，进校验反馈循环"


def test_fix_respects_day_end_bound():
    """顺延不得越过当天 23:59：写到 24:00 会被 parse_time 拒收、判官退回推算口径。"""
    items = [
        _item("A", "22:00", "22:30"),
        _item("B", "22:30", "23:50"),
    ]
    assert fix_transfer_gaps(items) == 0, "只剩 9 分钟顺延空间，不足以达标"
    b = _by_name(items, "B")
    assert b["end_time"] == "23:59", "顺延顶到 23:59 为止，绝不写出 24:00"


def test_no_fix_when_gap_already_sufficient():
    """判官放行的对（含容差内裕量差）一律不动：避免无谓改动产物。"""
    sufficient = [_item("A", "09:00", "10:00"), _item("B", "10:30", "12:00")]
    within_tolerance = [_item("A", "09:00", "10:00"), _item("B", "10:27", "12:00")]
    for items in (sufficient, within_tolerance):
        before = copy.deepcopy(items)
        assert fix_transfer_gaps(items) == 0
        assert items == before, "留白达标 / 容差内的计划不该被碰"
    issues, _log = validate_plans([{"day_no": 1, "items": sufficient}])
    assert not any("路线时间不足" in issue for issue in issues)


def test_no_fix_without_coords_or_with_zero_sentinel():
    """缺坐标与 0/0 缺失哨兵都不猜路线：判官跳过的对微调规则同样跳过。"""
    missing = [_item("A", "09:00", "10:00", coord=(None, None)), _item("B", "10:00", "12:00")]
    sentinel = [_item("A", "09:00", "10:00", coord=(0.0, 0.0)), _item("B", "10:00", "12:00")]
    for items in (missing, sentinel):
        before = copy.deepcopy(items)
        assert fix_transfer_gaps(items) == 0
        assert items == before


def test_no_fix_on_overlapping_pair():
    """重叠是时间冲突规则的辖区：本规则不越权去拉扯重叠对。"""
    items = [_item("A", "09:00", "10:00"), _item("B", "09:30", "12:00")]
    before = copy.deepcopy(items)
    assert fix_transfer_gaps(items) == 0
    assert items == before


def test_non_timed_items_are_ignored_but_pair_through():
    """酒店/交通不参与配对也不被改动；夹在中间时相邻 attraction/food 直接配对。"""
    hotel = {
        "item_type": "hotel",
        "poi_name": "酒店",
        "start_time": "21:00",
        "end_time": "23:00",
        "latitude": SAME_COORD[0],
        "longitude": SAME_COORD[1],
    }
    transport = {
        "item_type": "transport",
        "poi_name": "地铁",
        "start_time": "10:00",
        "end_time": "10:10",
        "latitude": SAME_COORD[0],
        "longitude": SAME_COORD[1],
    }
    items = [_item("A", "09:00", "10:00"), transport, _item("B", "10:05", "12:00"), hotel]
    before_transport = copy.deepcopy(transport)
    before_hotel = copy.deepcopy(hotel)
    assert fix_transfer_gaps(items) == 1, "A→B 中间的 transport 不打断配对（判官同口径）"
    assert transport == before_transport and hotel == before_hotel


def test_items_without_parseable_times_are_untouched():
    """时刻不可解析（缺 end_time）的条目判官按 start+duration 推算，本规则不动它。"""
    no_end = {
        "item_type": "attraction",
        "poi_name": "A",
        "latitude": 30.0,
        "longitude": 120.0,
        "start_time": "09:00",
        "duration_min": 60,
    }
    items = [no_end, _item("B", "09:30", "12:00")]
    before = copy.deepcopy(items)
    assert fix_transfer_gaps(items) == 0
    assert items == before


def test_unsorted_input_and_multiple_days_are_handled_per_plan():
    """输入按条目自身 start 排序后配对；函数只吃单日 items 列表，多天逐日调用。"""
    day1 = [_item("B", "10:05", "12:00"), _item("A", "09:00", "10:00")]
    day2 = [_item("C", "09:00", "10:00"), _item("D", "11:00", "12:00")]
    assert fix_transfer_gaps(day1) == 1
    assert fix_transfer_gaps(day2) == 0, "留白 60 分钟的第二天不触发"
    assert _by_name(day1, "A")["end_time"] == "09:53"


def test_min_visit_floor_is_respected():
    """前条时长恰为保底（30 分钟）时没有可收缩空间：只能靠顺延。"""
    items = [_item("A", "09:00", "09:30"), _item("B", "09:30", "12:00")]
    assert fix_transfer_gaps(items) == 1
    a = _by_name(items, "A")
    assert a["end_time"] == "09:30", f"保底 {MIN_VISIT_MINUTES} 分钟不可再收"
    assert _by_name(items, "B")["start_time"] == "09:42", "全部缺口走顺延"


def test_normalize_item_clocks_wraps_24h_and_syncs_duration():
    """24:00 回绕 00:00、duration 跟随时间窗；end<=start 的脏窗不写 duration。"""
    item = {"start_time": "23:00", "end_time": "24:00", "duration_min": 999}
    normalize_item_clocks(item)
    assert item["start_time"] == "23:00" and item["end_time"] == "00:00"
    assert item["duration_min"] == 999, "回绕后 en<=st（原口径 en > st 才写），duration 原样保留"

    normal = {"start_time": "09:00", "end_time": "11:30", "duration_min": 999}
    normalize_item_clocks(normal)
    assert normal["duration_min"] == 150, "常规窗以时间窗为准"

    crossed = {"start_time": "23:30", "end_time": "01:00", "duration_min": 120}
    normalize_item_clocks(crossed)
    assert crossed == {"start_time": "23:30", "end_time": "01:00", "duration_min": 120}, "跨午夜脏窗原样保留"
