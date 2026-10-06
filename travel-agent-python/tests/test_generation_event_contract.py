"""生成链路事件归一的结构守卫（G-2.5 ②）。

背景：图节点链路（逐日）与整段流式链路（NDJSON）此前各有各的事件出口，
"同一业务两份事件"会让前端看到的进度序列随走哪条链路而变。归一后的约定是：
**两条链路都只经 `app/services/generation_events.py` 发布日程事件**，该模块再
统一委托 `app/common/event_publisher`（信封 5 键 camelCase、seq 单调）。

本测试用两条互补的守卫钉住该约定：
1. 行为面——钉住 day_start / day_done 的 data 键名与取值口径（两条链路的两个
   调用点共用这一份契约，改口径必然同时反映到两侧）；
2. 结构面——业务层不得绕过 generation_events 直接调 publish_event（否则又回到
   两份出口）。

为什么值得单独守：这类"多出口"退化不会让任何单测变红，只会让前端进度序列
在换链路时悄悄变样——正是 P2 要消除的那类漂移。
"""

from __future__ import annotations

import pathlib

from app.services import generation_events

GENERATION_MODULE = pathlib.Path(__file__).resolve().parent.parent / "app" / "services" / "itinerary_generation.py"


def test_day_events_payload_contract(monkeypatch):
    """day_start / day_done 的 data 口径（两条链路的调用点共用）。"""
    captured: list[tuple] = []
    monkeypatch.setattr(
        generation_events,
        "publish_event",
        lambda itinerary_id, event_type, data: captured.append((itinerary_id, event_type, data)),
    )

    generation_events.day_start(7, 2)
    generation_events.day_done(7, 2, "园林慢游", 3, "含晚间灯光秀")

    assert captured == [
        (7, "day_start", {"dayNo": 2}),
        (7, "day_done", {"dayNo": 2, "theme": "园林慢游", "itemCount": 3, "note": "含晚间灯光秀"}),
    ]


def test_generation_layer_publishes_only_through_service():
    """业务层不得裸调 publish_event：日程事件必须经 generation_events 服务。

    逐日链路与整段流式链路都在这一个文件里发事件——出现裸 publish_event
    调用即意味着又有了一条旁路出口。
    """
    source = GENERATION_MODULE.read_text(encoding="utf-8")
    assert "generation_events." in source, "逐日/整段链路的日程事件应经 generation_events 发布"
    assert "publish_event(" not in source.replace("generation_events.publish_event", ""), (
        "itinerary_generation 不得直接调用 publish_event；请经 generation_events 服务（G-2.5 ②）"
    )
