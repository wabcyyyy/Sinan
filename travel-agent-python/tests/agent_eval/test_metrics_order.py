"""trace 序列规范化单测（PR-0）：泳道稳定排序 = 确定性 + 保序，两者都要。

背景（实测）：研究三域并行执行，工具事件的**到达顺序**由线程调度决定——同一
fixture 三次运行拿到三种排列。旧口径 `sorted(名字)` 稳定了字节却把调用顺序整个
丢掉（丢序），图路径乱序也测不出来。这里钉住新口径的三条语义：
1. 跨泳道乱序的到达排列 → 规范化后字节一致（确定性）；
2. 泳道内到达序保留（顺序信息不丢）；
3. 主泳道（节点/主线程工具）到达序原样保留——顺序反了要能测出来。
"""

from __future__ import annotations

from app.agent.research.evidence import RESEARCH_DOMAINS
from tests.agent_eval.metrics import ordered_event_names


def _event(kind: str, name: str, span_id: str, parent_span_id: str | None = None) -> dict:
    event: dict = {"kind": kind, "name": name, "span_id": span_id}
    if parent_span_id:
        event["parent_span_id"] = parent_span_id
    return event


def _lane_events(domain: str, tool_names: list[str]) -> list[dict]:
    """一个研究泳道的事件序列：agent span + 其下按序到达的工具事件。"""
    lane_span = f"span-{domain}"
    events = [_event("tool", name, f"span-{name}", parent_span_id=lane_span) for name in tool_names]
    events.append(_event("agent", f"research.{domain}", lane_span, parent_span_id="node-research"))
    return events


def _all_lanes() -> list[dict]:
    events: list[dict] = []
    for domain in RESEARCH_DOMAINS:
        events += _lane_events(domain, [f"search_{domain}", f"refine_{domain}"])
    return events


def test_cross_lane_interleavings_normalize_to_the_same_sequence():
    """三泳道三种到达排列（实测的那三种）→ 规范化后完全一致。"""
    base = _all_lanes()
    hotel_first = (
        _lane_events("hotel", ["search_hotel", "refine_hotel"])
        + _lane_events("attraction", ["search_attraction", "refine_attraction"])
        + _lane_events("food", ["search_food", "refine_food"])
    )
    attraction_first = (
        _lane_events("attraction", ["search_attraction", "refine_attraction"])
        + _lane_events("food", ["search_food", "refine_food"])
        + _lane_events("hotel", ["search_hotel", "refine_hotel"])
    )
    interleaved = (
        _lane_events("food", ["search_food"])
        + _lane_events("hotel", ["search_hotel"])
        + _lane_events("attraction", ["search_attraction"])
    )
    # 前两组是同一批泳道事件的整段换序；第三组是逐泳道交错（只含首轮工具）
    assert ordered_event_names(base) == ordered_event_names(hotel_first)
    assert ordered_event_names(base) == ordered_event_names(attraction_first)
    # 泳道内保到达序：agent 收尾事件跟在本泳道工具后，不与他泳道交织
    assert ordered_event_names(interleaved) == [
        "search_hotel",
        "research.hotel",
        "search_attraction",
        "research.attraction",
        "search_food",
        "research.food",
    ]


def test_within_lane_order_is_preserved():
    """泳道内到达序 = 调用序：补查发生在主检索之后，顺序反了要能测出来。"""
    lane = _lane_events("attraction", ["search_attraction", "refine_attraction", "refine_again"])
    assert ordered_event_names(lane) == [
        "search_attraction",
        "refine_attraction",
        "refine_again",
        "research.attraction",
    ]


def test_main_lane_events_keep_arrival_order():
    """节点/主线程工具归主泳道、排在研究泳道后，到达序原样保留。

    这条就是「丢序」修复的证明：旧口径 sorted(名字) 会把 parse→research→generate
    排成字母序，图路径乱序（如 generate 排到 format 前）根本测不出来。
    """
    events = [
        *_all_lanes(),
        _event("tool", "fixture.get_consumption", "span-cons", parent_span_id="node-research"),
        _event("tool", "fixture.resolve_poi", "span-poi1", parent_span_id="node-generate"),
        _event("tool", "fixture.resolve_poi", "span-poi2", parent_span_id="node-generate"),
    ]
    nodes = [
        _event("node", "parse", "n1"),
        _event("node", "research", "n2"),
        _event("node", "generate", "n3"),
        _event("node", "reflect", "n4"),
        _event("node", "format", "n5"),
    ]
    assert ordered_event_names(events)[-3:] == ["fixture.get_consumption", "fixture.resolve_poi", "fixture.resolve_poi"]
    # 节点序列保到达序：换序输入 → 换序输出（sorted(名字) 在这里测不出回归）
    assert ordered_event_names(nodes) == ["parse", "research", "generate", "reflect", "format"]
    assert ordered_event_names(list(reversed(nodes))) == ["format", "reflect", "generate", "research", "parse"]


def test_events_without_lane_ancestor_stay_in_main_lane():
    """上溯不到 research.<domain> 的事件（含无 parent 的裸事件）都归主泳道。"""
    events = [
        _event("tool", "zzz_main", "s1", parent_span_id="unknown-span"),
        _event("tool", "aaa_main", "s2"),
        _event("tool", "search_hotel", "s3", parent_span_id="lane-hotel"),
        _event("agent", "research.hotel", "lane-hotel", parent_span_id="node-research"),
    ]
    assert ordered_event_names(events) == ["search_hotel", "research.hotel", "zzz_main", "aaa_main"]
