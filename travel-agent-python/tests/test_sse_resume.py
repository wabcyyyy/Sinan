"""业务进度 SSE 断线续传（2026-10-12）：回放日志 + Last-Event-ID 补发。

钉住四件事：
1. 只有业务进度帧进回放日志（心跳、会话私有的对话帧不进）；
2. 重连带编号时补发其后的帧，订阅与回放之间到达的帧不重复、不遗漏；
3. 补不全（被挤出 / 进程重启 / 编号来自别处）时先发 resync 帧，不假装续上；
4. 补发里已有真实终态帧时，不再叠一帧终态快照。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.common import event_hub, event_publisher
from app.services import itinerary_events

TRIP = 77


@pytest.fixture(autouse=True)
def _isolated_hub(monkeypatch):
    from app.common import generation_event_log

    monkeypatch.setattr(generation_event_log, "append", lambda *_: None)
    monkeypatch.setattr(generation_event_log, "next_seq", lambda *_: None)
    monkeypatch.setattr(generation_event_log, "read_after", lambda *_: None)
    # 终态后连接要空转一个心跳周期才收尾；缩短周期让用例不必真等 15 秒
    monkeypatch.setattr(event_hub, "HEARTBEAT_SECONDS", 0.05)
    event_publisher.reset_event_publisher()
    yield
    event_publisher.reset_event_publisher()


def _parse(frame: str) -> tuple[int | None, dict]:
    event_id = None
    data = ""
    for line in frame.strip().splitlines():
        if line.startswith("id:"):
            event_id = int(line[3:])
        elif line.startswith("data:"):
            data = line[5:]
    return event_id, json.loads(data)


def test_only_business_events_enter_replay_log() -> None:
    event_publisher.publish_event(TRIP, "day_start", {"dayNo": 1})
    event_publisher.publish_local(TRIP, "chat_token", {"text": "私有"})
    event_hub.broadcast(TRIP, event_publisher.heartbeat_envelope(TRIP))
    event_publisher.publish_event(TRIP, "day_done", {"dayNo": 1})

    frames, complete, upto = event_hub.replay_after(TRIP, 0)
    assert complete and upto == 2
    assert [json.loads(frame)["type"] for frame in frames] == ["day_start", "day_done"]
    assert [frame.event_id for frame in frames] == [1, 2]


def test_replay_is_incomplete_when_log_cannot_cover_the_gap(monkeypatch) -> None:
    assert event_hub.replay_after(TRIP, 3) == ([], False, 0), "日志里没有这个行程（如进程重启）"
    monkeypatch.setattr(event_hub, "REPLAY_BUFFER", 3)
    for day_no in range(1, 6):
        event_publisher.publish_event(TRIP, "day_start", {"dayNo": day_no})
    frames, complete, upto = event_hub.replay_after(TRIP, 1)
    assert not complete, "编号 2 已被挤出，补不全"
    assert [frame.event_id for frame in frames] == [3, 4, 5] and upto == 5
    assert event_hub.replay_after(TRIP, 3)[1], "编号 3 之后都还在日志里"
    assert not event_hub.replay_after(TRIP, 99)[1], "编号来自别的进程/旧日志"


def test_reconnect_replays_missed_frames_then_streams_live_without_duplicates() -> None:
    async def scenario() -> list[tuple[int | None, dict]]:
        event_publisher.publish_event(TRIP, "research_start", {"domains": []})  # 1：客户端已收到
        event_publisher.publish_event(TRIP, "research_done", {"evidenceCount": 3})  # 2：断线期间错过
        subscription, rejected = event_hub.subscribe(TRIP, event_publisher.too_many_connections_envelope(TRIP))
        assert not rejected
        # 订阅已登记、回放尚未读取：这一帧既在实时队列里也在日志里，只能出现一次
        event_publisher.publish_event(TRIP, "day_start", {"dayNo": 1})  # 3
        frames = itinerary_events.event_frames(TRIP, subscription, None, last_event_id=1)
        received = [_parse(await frames.__anext__()) for _ in range(2)]
        event_publisher.publish_event(TRIP, "complete", {"status": 2})  # 4：重连后的实时帧
        received += [_parse(frame) async for frame in frames]
        return received

    received = asyncio.run(scenario())
    assert [(event_id, body["type"]) for event_id, body in received] == [
        (2, "research_done"),
        (3, "day_start"),
        (4, "complete"),
    ]
    assert event_hub.subscriber_count(TRIP) == 0


def test_gap_emits_resync_before_whatever_can_be_replayed(monkeypatch) -> None:
    monkeypatch.setattr(event_hub, "REPLAY_BUFFER", 2)
    for day_no in range(1, 5):
        event_publisher.publish_event(TRIP, "day_done", {"dayNo": day_no})

    async def scenario() -> list[tuple[int | None, dict]]:
        subscription, _ = event_hub.subscribe(TRIP, event_publisher.too_many_connections_envelope(TRIP))
        snapshot = event_publisher.build_envelope(TRIP, "done", {"complete": True})
        frames = itinerary_events.event_frames(TRIP, subscription, snapshot, last_event_id=1)
        return [_parse(frame) async for frame in frames]

    received = asyncio.run(scenario())
    assert received[0][0] is None and received[0][1]["type"] == "resync"
    assert received[0][1]["data"]["reason"] == "REPLAY_GAP"
    assert [event_id for event_id, _ in received[1:3]] == [3, 4]
    assert received[-1][1]["type"] == "done", "补发里没有终态帧，仍补一帧终态快照"


def test_replayed_terminal_frame_suppresses_duplicate_snapshot() -> None:
    event_publisher.publish_event(TRIP, "day_done", {"dayNo": 1})
    event_publisher.publish_event(TRIP, "complete", {"status": 2})

    async def scenario() -> list[tuple[int | None, dict]]:
        subscription, _ = event_hub.subscribe(TRIP, event_publisher.too_many_connections_envelope(TRIP))
        snapshot = event_publisher.build_envelope(TRIP, "done", {"complete": True})
        frames = itinerary_events.event_frames(TRIP, subscription, snapshot, last_event_id=1)
        return [_parse(frame) async for frame in frames]

    assert [body["type"] for _, body in asyncio.run(scenario())] == ["complete"]


def test_without_last_event_id_behaviour_is_unchanged() -> None:
    event_publisher.publish_event(TRIP, "day_done", {"dayNo": 1})

    async def scenario() -> list[tuple[int | None, dict]]:
        subscription, _ = event_hub.subscribe(TRIP, event_publisher.too_many_connections_envelope(TRIP))
        snapshot = event_publisher.build_envelope(TRIP, "done", {"complete": True})
        return [_parse(frame) async for frame in itinerary_events.event_frames(TRIP, subscription, snapshot)]

    assert [body["type"] for _, body in asyncio.run(scenario())] == ["done"], "首次连接不补发历史"


@pytest.mark.parametrize(
    "header,query,expected",
    [
        ("12", None, 12),
        (None, "7", 7),
        (" 3 ", "9", 3),
        ("abc", "5", 5),
        ("-1", None, None),
        (None, None, None),
        ("²", "5", 5),
        ("１２", None, None),
        ("9" * 5000, "5", 5),
    ],
)
def test_parse_last_event_id(header, query, expected) -> None:
    assert itinerary_events.parse_last_event_id(header, query) == expected
