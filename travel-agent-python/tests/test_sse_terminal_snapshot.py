"""SSE 订阅发生在终态之后时必须自己收尾（R2-2）。

进程内总线只广播、不留历史缓冲：断线重连或"生成完了才打开页面"的客户端
如果只等广播，就永远只能收到心跳帧——UI 上表现为无限转圈。

异步用例走 `anyio.run`（本仓无 pytest-asyncio 配置，既有异步测试同此口径）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anyio
import pytest

from app.common import event_hub
from app.services import itinerary_events


def _main(gen_state: str | None, days: int = 3) -> SimpleNamespace:
    return SimpleNamespace(gen_state=gen_state, days=days, trip_theme=None)


def _collect_frames(itinerary_id: int, subscription, snapshot: str | None) -> list[str]:
    out: list[str] = []

    async def _run() -> None:
        async for frame in itinerary_events.event_frames(itinerary_id, subscription, snapshot):
            out.append(frame)

    anyio.run(_run)
    return out


def test_completed_itinerary_yields_a_done_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(itinerary_events.day_persistence, "unfinished_day_nos", lambda _id: [3])
    envelope = json.loads(str(itinerary_events.terminal_snapshot(42, _main("COMPLETED"))))
    assert envelope["type"] == "done"
    assert envelope["itineraryId"] == 42
    assert envelope["data"]["daysExpected"] == 3
    assert envelope["data"]["daysEmitted"] == [1, 2]
    assert envelope["data"]["complete"] is True


def test_partial_itinerary_reports_incomplete_done(monkeypatch) -> None:
    monkeypatch.setattr(itinerary_events.day_persistence, "unfinished_day_nos", lambda _id: [2, 3])
    envelope = json.loads(str(itinerary_events.terminal_snapshot(7, _main("PARTIAL"))))
    assert envelope["data"]["complete"] is False


def test_failed_itinerary_yields_an_error_snapshot() -> None:
    envelope = json.loads(str(itinerary_events.terminal_snapshot(7, _main("FAILED"))))
    assert envelope["type"] == "error"
    assert envelope["data"]["code"] == "GENERATION_FAILED"


@pytest.mark.parametrize("gen_state", ["GENERATING", None])
def test_still_running_has_no_snapshot(gen_state: str | None) -> None:
    assert itinerary_events.terminal_snapshot(7, _main(gen_state)) is None


def test_frames_close_after_the_snapshot(monkeypatch) -> None:
    """拿到终态帧后连接必须结束：不退回心跳循环，也不再去等广播。"""
    monkeypatch.setattr(event_hub, "unsubscribe", lambda _sub: None)
    taken: list[str] = []

    class _Sub:
        async def take(self):
            taken.append("take")
            return None  # 空闲：没有快照时这里会变成心跳帧

    frames = _collect_frames(1, _Sub(), "SNAPSHOT")
    assert frames == ["data:SNAPSHOT\n\n"]
    assert taken == []


def test_snapshot_path_still_unsubscribes(monkeypatch) -> None:
    calls: list[object] = []
    monkeypatch.setattr(event_hub, "unsubscribe", lambda sub: calls.append(sub))
    frames = _collect_frames(1, "SUB", "S")
    assert frames == ["data:S\n\n"]
    assert calls == ["SUB"]
