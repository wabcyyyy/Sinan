"""进程内 SSE 事件总线（取代 Java 的「Redis pub/sub → SseEmitter 转发桥」）。

为什么值得有这一份：M5-c 之后"生成"与"订阅"在同一个进程里，事件从产生到送达只是一次
入队；再绕一圈 Redis 等于要求本机演示也必须跑着 Redis 才看得到进度。双跑期对外提供 SSE
的仍是 Java 网关，本模块只服务已经指向 Python 的连接（M7 切流量后是唯一路径）。

**为什么是 asyncio 队列而不是线程队列**：SSE 连接的寿命是分钟级，同步生成器
`queue.get(timeout=15)` 会把 starlette 线程池（默认 40 个工作线程）里的一个线程按小时占住，
40 个并发订阅就能让整个服务停摆，而且客户端断开时无法打断阻塞中的线程。
asyncio 侧的 `wait_for(queue.get(), …)` 可被取消：连接一断，生成器立即结束、订阅立即摘除。

与 Java `ItinerarySseGateway` 对齐的三点：
- 同一行程最多 5 条连接；超限时**不抛 HTTP 错误**，只给这条新连接发一条
  `error`(TOO_MANY_CONNECTIONS) 信封后收尾——抛错会被 EventSource 当成网络故障引发重连风暴；
- 心跳信封 `type=heartbeat, seq=0, data={}`，15 秒一次，防浏览器/代理层空闲断开；
- 投递失败或缓冲积压满的连接立即摘除，不给后续广播留死连接。

刻意不移植：Java 的「Redis 失联守卫 → closeAll() 断开所有 SSE」。事件源已经在进程内，
切断健康的本地流只会让前端白白重连一次。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)

MAX_SUBSCRIBERS_PER_ITINERARY = 5
HEARTBEAT_SECONDS = 15.0
# 慢消费者缓冲：一条生成事件约 200B，200 条足够覆盖一次前端卡顿；再满即判定连接已死
SINK_BUFFER = 200

# 队列排空且连接已关闭：响应流据此立即收尾（被拒的连接不该再等一次心跳）
CLOSED = object()


class Subscription:
    """一条 SSE 连接。生产端可能在任意线程，消费端固定在自己的事件循环上。"""

    __slots__ = ("_queue", "itinerary_id", "loop", "open")

    def __init__(self, itinerary_id: int, loop: asyncio.AbstractEventLoop) -> None:
        self.itinerary_id = itinerary_id
        self.loop = loop
        self.open = True
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=SINK_BUFFER)

    async def take(self, timeout: float | None = None) -> Any:
        """取一条信封 JSON。空闲超时返回 None（调用方据此发心跳）；关闭且排空返回 CLOSED。"""
        wait = HEARTBEAT_SECONDS if timeout is None else timeout
        try:
            return await asyncio.wait_for(self._queue.get(), wait)
        except TimeoutError:
            return None if self.open else CLOSED

    def offer(self, envelope: str) -> bool:
        """从任意线程投递一帧；返回 False 表示连接已死（调用方应摘除它）。"""
        if not self.open:
            return False
        try:
            self.loop.call_soon_threadsafe(self._put, envelope)
        except RuntimeError:
            # 事件循环已关闭（客户端断开后仍在广播）：这条连接彻底作废
            self.open = False
            return False
        return True

    def _put(self, envelope: str) -> None:
        # 只在所属事件循环线程里执行：积压满即自杀，宁可丢死连接也不阻塞生产端。
        # 这里**不**判 self.open：被拒连接正是"先排一帧提示、随即标记关闭"，
        # 判了就会把那条可读的 error 一起丢掉（take 只在队列排空后才报 CLOSED）。
        try:
            self._queue.put_nowait(envelope)
        except asyncio.QueueFull:
            logger.info("sse subscriber saturated, dropping: itinerary=%s", self.itinerary_id)
            self.open = False


_lock = threading.Lock()
_subscribers: dict[int, list[Subscription]] = {}


def subscribe(itinerary_id: int, limit_envelope: str) -> tuple[Subscription, bool]:
    """登记一条连接，必须在事件循环里调用（要捕获它所属的 loop 才能跨线程投递）。

    返回 (订阅, 是否因超限被拒)；被拒的订阅里已经放好了那条 error 信封。
    """
    loop = asyncio.get_running_loop()
    subscription = Subscription(itinerary_id, loop)
    with _lock:
        sinks = _subscribers.setdefault(itinerary_id, [])
        rejected = len(sinks) >= MAX_SUBSCRIBERS_PER_ITINERARY
        if not rejected:
            sinks.append(subscription)
    if rejected:
        # 与 Java 同：对被拒连接也先把提示写出去再收尾，前端才拿得到可读的原因
        subscription.offer(limit_envelope)
        subscription.open = False
    return subscription, rejected


def unsubscribe(subscription: Subscription) -> None:
    with _lock:
        sinks = _subscribers.get(subscription.itinerary_id)
        if not sinks:
            return
        if subscription in sinks:
            sinks.remove(subscription)
        if not sinks:
            # 空表必须删 key，否则注册表随行程数无界增长
            _subscribers.pop(subscription.itinerary_id, None)
    subscription.open = False


def close_after_terminal(itinerary_id: int) -> None:
    """终态帧（complete/error）广播后收尾该行程的全部订阅（R3-F4）。

    只置 open=False、**不摘表**：各连接队列里已入队的帧（含终态帧本身）会先被
    消费端排空——`take` 只在"队列空转一个心跳周期且 open=False"才报 CLOSED——
    排空后响应流自然收尾，其 finally 再逐个退订。硬 unsubscribe 会在终态帧还在
    队列里时把它掐断。终态后才重连的客户端走 terminal_snapshot 补帧，不受影响。
    """
    with _lock:
        sinks = list(_subscribers.get(itinerary_id, ()))
    for sink in sinks:
        sink.open = False


def broadcast(itinerary_id: int, envelope: str) -> int:
    """把一帧信封投递给该行程的全部连接，返回尽力送达数；已死连接就地摘除。"""
    with _lock:
        sinks = list(_subscribers.get(itinerary_id, ()))
    if not sinks:
        return 0
    delivered = 0
    dead: list[Subscription] = []
    for sink in sinks:
        if sink.offer(envelope):
            delivered += 1
        else:
            dead.append(sink)
    for sink in dead:
        unsubscribe(sink)
    return delivered


def subscriber_count(itinerary_id: int) -> int:
    with _lock:
        return len(_subscribers.get(itinerary_id, ()))


def reset_for_tests() -> None:
    with _lock:
        _subscribers.clear()
