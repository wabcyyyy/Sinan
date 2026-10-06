"""有界后台任务池（对应 Java `AsyncConfig` 的三个 ThreadPoolTaskExecutor）。

为什么自己包一层：Python 的 `ThreadPoolExecutor` 队列**无界**，直接用就没有
「队列满 → 立刻拒绝」这条语义，而它是用户可见行为——生成任务满了要返回 429
「系统繁忙，请稍后再试」，而不是把请求挂到超时。

`slots` = 「正在跑」+「已排队」的总量（Java 是 core/max + queueCapacity，
超出 max 后进队列、队列满才拒），所以等价的入口拦截就是 `max + capacity`。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor


class TaskRejected(RuntimeError):
    """池已满。调用方要么转 429，要么按 CallerRuns 语义就地执行。"""


class SlotExecutor:
    def __init__(self, name: str, workers: int, slots: int) -> None:
        self.name = name
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=name)
        self._slots = threading.BoundedSemaphore(slots)

    def submit(self, task: Callable[..., object], *args: object) -> Future:
        if not self._slots.acquire(blocking=False):
            raise TaskRejected(f"{self.name} queue full")
        try:
            return self._pool.submit(self._guarded, task, args)
        except BaseException:
            self._slots.release()
            raise

    def _guarded(self, task: Callable[..., object], args: tuple[object, ...]) -> object:
        try:
            return task(*args)
        finally:
            # 任务体自己负责异常处理与终态落库；这里只保证令牌一定归还
            self._slots.release()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=False)
