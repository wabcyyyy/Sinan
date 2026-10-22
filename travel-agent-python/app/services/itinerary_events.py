"""生成进度 SSE 的读模型：终态补帧 + 事件帧生成。

从 `app/api/business/itinerary.py` 拆出（第 3 轮结构整改）。留在路由里有两个代价：
① `terminal_snapshot` 组合 `itinerary_main.gen_state` × `day_persistence.unfinished_day_nos`
   再拼成 `done` 信封，是**读模型**而不是 HTTP 编排，与"瘦路由、逻辑进 service"的
   既有分工相反；② 它连带把 router 文件顶到巨大文件棘轮的顶格（11/11），此后任何
   新路由都会把 `just check` 判红，而 INV-1 禁止放宽门禁。

终态补帧本身是 R2-2 的修复：生成结束后才连上（断线重连、或用户在结果页重新打开）的连接
等不到实时终态帧。2026-10-12 起 `event_hub` 另有按行程的有界回放日志：重连带
`Last-Event-ID`（或 `lastEventId` 查询参数）时先补发错过的帧，补不全发 `resync` 帧；
终态补帧仍保留，覆盖回放日志里已经没有该行程的情况。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

from app.common import event_hub, event_publisher, generation_event_log
from app.db.models import ItineraryMain
from app.db.session import session_scope
from app.services import day_persistence

logger = logging.getLogger(__name__)
POLL_SECONDS = 0.5


def terminal_snapshot(itinerary_id: int, main: Any) -> str | None:
    """订阅时行程已经终态 → 补一帧收尾，别让这条连接永远只发心跳。

    终态判定读库里的 `gen_state`，不猜缓存。
    """
    gen_state = getattr(main, "gen_state", None)
    if gen_state == "FAILED":
        return event_publisher.error_envelope(itinerary_id, "GENERATION_FAILED", "生成未完成，可重新发起生成")
    if gen_state not in ("COMPLETED", "PARTIAL"):
        return None
    expected = int(getattr(main, "days", 0) or 0)
    missing = set(day_persistence.unfinished_day_nos(itinerary_id))
    return event_publisher.build_envelope(
        itinerary_id,
        "done",
        {
            "daysExpected": expected,
            "daysEmitted": [day_no for day_no in range(1, expected + 1) if day_no not in missing],
            "tripTheme": getattr(main, "trip_theme", None),
            "complete": gen_state == "COMPLETED",
            "message": "该行程已生成完成",
        },
    )


_TERMINAL_TYPES = ("complete", "done", "error")


def parse_last_event_id(*candidates: str | None) -> int | None:
    """取第一个合法的非负整数回放编号（请求头 Last-Event-ID 优先，其次查询参数）；都没有返回 None。"""
    for raw in candidates:
        text = raw.strip() if isinstance(raw, str) else ""
        # HTTP 游标只收有界 ASCII 数字：isdigit 还接受 int 无法解析的上标数字，
        # 超长数字也会触发 Python 的转换上限，不能让坏请求把 SSE 路由变成 500。
        if text.isascii() and text.isdecimal() and len(text) <= 20:
            return int(text)
    return None


def _sse_frame(envelope: str) -> str:
    """一帧 SSE：进回放日志的帧带 `id:` 行（客户端重连时作为 Last-Event-ID 带回）。"""
    event_id = getattr(envelope, "event_id", None)
    prefix = f"id:{event_id}\n" if event_id is not None else ""
    return f"{prefix}data:{envelope}\n\n"


def _is_terminal(envelope: str) -> bool:
    try:
        return json.loads(envelope).get("type") in _TERMINAL_TYPES
    except (ValueError, AttributeError):
        return False


async def _local_event_frames(
    itinerary_id: int, subscription: Any, snapshot: str | None = None, last_event_id: int | None = None
) -> AsyncIterator[str]:
    """衔接有界事件回放与实时订阅，断线后显式提示无法补齐的缺口。

    回放不完整时发 resync，由客户端回源详情；终态快照避免重连后
    永远等待结束帧。连接退出时无论成功与否都注销订阅。

    Args:
        itinerary_id: 事件所属行程。
        subscription: 已注册的实时事件订阅。
        snapshot: 订阅时已知的终态信封，可为空。
        last_event_id: 客户端最后收到的事件编号，可为空。

    Yields:
        带 SSE 分隔符的事件帧，包括必要的回放、同步提示与心跳。
    """
    replayed_upto = 0
    replayed_terminal = False
    if last_event_id is not None:
        missed, complete, replayed_upto = event_hub.replay_after(itinerary_id, last_event_id)
        if not complete:
            yield _sse_frame(event_publisher.resync_envelope(itinerary_id))
        for frame in missed:
            replayed_terminal = replayed_terminal or _is_terminal(frame)
            yield _sse_frame(frame)
    if snapshot is not None:
        # 补发里已经有真实终态帧就不再叠一帧快照（前端终态收尾只该跑一次）
        if not replayed_terminal:
            yield _sse_frame(snapshot)
        return
    while True:
        envelope = await subscription.take()
        if envelope is event_hub.CLOSED:
            return
        if envelope is None:
            # 空闲一个心跳周期才补心跳帧：连接保持活跃，但业务事件永远优先
            envelope = event_publisher.heartbeat_envelope(itinerary_id)
        elif (getattr(envelope, "event_id", None) or 0) and envelope.event_id <= replayed_upto:
            continue  # 订阅先于回放登记：这帧已在补发里出现过（或属于补不回来的那段）
        yield _sse_frame(envelope)


def _snapshot(itinerary_id: int) -> str | None:
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        return terminal_snapshot(itinerary_id, main) if main is not None else None


async def event_frames(
    itinerary_id: int, subscription: Any, snapshot: str | None = None, last_event_id: int | None = None
) -> AsyncIterator[str]:
    """共享日志决定事件顺序；本地队列只加速唤醒并承载会话私有事件。"""
    cursor = last_event_id if last_event_id is not None else getattr(subscription, "start_event_id", None)
    heartbeat_at = time.monotonic()
    gap_reported = False
    try:
        if getattr(subscription, "rejected", False):
            async for frame in _local_event_frames(itinerary_id, subscription):
                yield frame
            return
        while True:
            try:
                result = await asyncio.to_thread(generation_event_log.read_after, itinerary_id, cursor)
                if result is None:
                    # 无业务行程的内部事件流仍可使用本地总线。
                    async for frame in _local_event_frames(itinerary_id, subscription, snapshot, last_event_id):
                        yield frame
                    return
                frames, complete, latest = result
                if not complete:
                    yield _sse_frame(event_publisher.resync_envelope(itinerary_id))
                for frame in frames:
                    yield _sse_frame(frame)
                    if _is_terminal(frame):
                        return
                cursor = latest
                gap_reported = False
                # 发布前崩溃/日志过期也不会留下无限心跳的终态连接；始终从 DB 补结论。
                terminal = await asyncio.to_thread(_snapshot, itinerary_id)
                if terminal is not None:
                    yield _sse_frame(terminal)
                    return
            except Exception:
                logger.warning("SSE shared replay read failed: itinerary=%s", itinerary_id, exc_info=True)
                if not gap_reported:
                    yield _sse_frame(event_publisher.resync_envelope(itinerary_id))
                    gap_reported = True
            local = await subscription.take(timeout=POLL_SECONDS)
            if local is not None and local is not event_hub.CLOSED and getattr(local, "event_id", None) is None:
                yield _sse_frame(local)  # 聊天等会话私有帧不进入共享回放
            if time.monotonic() - heartbeat_at >= event_hub.HEARTBEAT_SECONDS:
                yield _sse_frame(event_publisher.heartbeat_envelope(itinerary_id))
                heartbeat_at = time.monotonic()
    finally:
        event_hub.unsubscribe(subscription)


async def sse_frames(envelopes: Any) -> AsyncIterator[str]:
    """把信封异步流转成 SSE 帧（一帧 = `data:<json>` 加一个空行）。"""
    async for envelope in envelopes:
        yield f"data:{envelope}\n\n"


def sse_response(frames: Any) -> StreamingResponse:
    """SSE 响应外壳。

    `X-Accel-Buffering: no` 是必需的：反向代理默认缓冲响应，SSE 会被攒成一坨。
    """
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
