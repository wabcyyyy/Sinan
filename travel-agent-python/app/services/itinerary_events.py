"""生成进度 SSE 的读模型：终态补帧 + 事件帧生成。

从 `app/api/business/itinerary.py` 拆出（第 3 轮结构整改）。留在路由里有两个代价：
① `terminal_snapshot` 组合 `itinerary_main.gen_state` × `day_persistence.unfinished_day_nos`
   再拼成 `done` 信封，是**读模型**而不是 HTTP 编排，与"瘦路由、逻辑进 service"的
   既有分工相反；② 它连带把 router 文件顶到巨大文件棘轮的顶格（11/11），此后任何
   新路由都会把 `just check` 判红，而 INV-1 禁止放宽门禁。

终态补帧本身是 R2-2 的修复：`event_hub` 只广播不留历史，所以"生成结束后才连上"
（EventSource 断线重连、或用户在结果页重新打开）永远等不到终态帧。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastapi.responses import StreamingResponse

from app.common import event_hub, event_publisher
from app.services import day_persistence


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


async def event_frames(itinerary_id: int, subscription: Any, snapshot: str | None = None) -> AsyncIterator[str]:
    try:
        if snapshot is not None:
            yield f"data:{snapshot}\n\n"
            return
        while True:
            envelope = await subscription.take()
            if envelope is event_hub.CLOSED:
                return
            if envelope is None:
                # 空闲一个心跳周期才补心跳帧：连接保持活跃，但业务事件永远优先
                envelope = event_publisher.heartbeat_envelope(itinerary_id)
            yield f"data:{envelope}\n\n"
    finally:
        # 客户端断开/服务收尾都要摘除，否则注册表里留死连接
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
