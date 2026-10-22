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

断线续传（2026-10-12）：业务事件进入按行程的有界回放日志，分配本进程内单调的回放编号
（SSE `id:` 行）。重连带 `Last-Event-ID` 时先补发编号之后的帧，再接实时流。登记与投递
在同一把锁里，所以每条连接收到的顺序就是日志顺序，按编号切分不会漏帧也不会重复。
补不全（日志已被挤出、进程重启、编号来自别的进程）时如实告知调用方，由它发 resync 帧，
前端回到「以 DB 为准」的对账路径。心跳与会话私有帧（对话流式）不进日志。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict, deque
from typing import Any

logger = logging.getLogger(__name__)

MAX_SUBSCRIBERS_PER_ITINERARY = 5
HEARTBEAT_SECONDS = 15.0
# 慢消费者缓冲：一条生成事件约 200B，200 条足够覆盖一次前端卡顿；再满即判定连接已死
SINK_BUFFER = 200
# 回放日志：每个行程保留最近 256 帧（一次多日生成的进度 + 候选预览帧量级），
# 最多记 512 个行程（按最近活跃淘汰），内存上界约 256×512×~300B ≈ 40MB 的最坏情况
REPLAY_BUFFER = 256
REPLAY_MAX_ITINERARIES = 512

# 队列排空且连接已关闭：响应流据此立即收尾（被拒的连接不该再等一次心跳）
CLOSED = object()


class Envelope(str):
    """带回放编号的信封：值就是信封 JSON（与普通 str 等价比较、可直接 json.loads），
    event_id 是本进程内该行程的回放编号；不进回放日志的帧（心跳/会话私有）为 None。"""

    event_id: int | None = None


class _ReplayLog:
    __slots__ = ("evicted_upto", "frames", "last_id")

    def __init__(self) -> None:
        self.frames: deque[Envelope] = deque(maxlen=REPLAY_BUFFER)
        self.last_id = 0
        # 已被挤出日志的最大编号：Last-Event-ID 比它小，说明中间有补不回来的帧
        self.evicted_upto = 0

    def append(self, envelope: str) -> Envelope:
        self.last_id += 1
        if len(self.frames) == self.frames.maxlen:
            self.evicted_upto = self.frames[0].event_id or self.evicted_upto
        frame = Envelope(envelope)
        frame.event_id = self.last_id
        self.frames.append(frame)
        return frame


class Subscription:
    """一条 SSE 连接。生产端可能在任意线程，消费端固定在自己的事件循环上。"""

    __slots__ = ("_queue", "itinerary_id", "loop", "open", "rejected")

    def __init__(self, itinerary_id: int, loop: asyncio.AbstractEventLoop) -> None:
        self.itinerary_id = itinerary_id
        self.loop = loop
        self.open = True
        self.rejected = False
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
_replays: OrderedDict[int, _ReplayLog] = OrderedDict()


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
        subscription.rejected = True
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


def _record(itinerary_id: int, envelope: str) -> Envelope:
    """把一帧写进回放日志（调用方持锁）；日志表按最近活跃淘汰。"""
    log = _replays.pop(itinerary_id, None) or _ReplayLog()
    while len(_replays) >= REPLAY_MAX_ITINERARIES:
        _replays.popitem(last=False)
    _replays[itinerary_id] = log
    return log.append(envelope)


def broadcast(itinerary_id: int, envelope: str, *, replayable: bool = False) -> int:
    """把一帧信封投递给该行程的全部连接，返回尽力送达数；已死连接就地摘除。

    replayable=True 的帧先进回放日志再投递，二者同在一把锁里（offer 只是
    call_soon_threadsafe 入队，不阻塞），保证各连接的接收顺序 = 日志顺序。
    """
    dead: list[Subscription] = []
    delivered = 0
    with _lock:
        frame = _record(itinerary_id, envelope) if replayable else envelope
        for sink in _subscribers.get(itinerary_id, ()):
            if sink.offer(frame):
                delivered += 1
            else:
                dead.append(sink)
    for sink in dead:
        unsubscribe(sink)
    return delivered


def replay_after(itinerary_id: int, last_event_id: int) -> tuple[list[Envelope], bool, int]:
    """取回放编号 > last_event_id 的帧，返回 (帧列表, 是否完整, 日志当前最新编号)。

    不完整的三种情况都意味着中间有帧补不回来：日志里没有这个行程（进程重启/被淘汰）、
    编号比日志最新编号还大（编号出自别的进程或旧日志）、编号早于已挤出的部分。
    最新编号给调用方做实时流去重：订阅先于回放登记，编号 ≤ 它的实时帧已经补发过或补不回来。
    """
    with _lock:
        log = _replays.get(itinerary_id)
        if log is None:
            return [], False, 0
        frames = [frame for frame in log.frames if (frame.event_id or 0) > last_event_id]
        complete = log.evicted_upto <= last_event_id <= log.last_id
        return frames, complete, log.last_id


def subscriber_count(itinerary_id: int) -> int:
    with _lock:
        return len(_subscribers.get(itinerary_id, ()))


def reset_for_tests() -> None:
    with _lock:
        _subscribers.clear()
        _replays.clear()
