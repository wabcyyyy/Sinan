"""周期任务注册表（G-3.3）。

职责：把进程内所有周期任务收成 `register(name, interval, fn)` 一处登记，
`start_all()` 统一启动、`stop_all()` 统一停止（可中断等待，不靠 join 超时）。

设计要点：
- **pytest 环境自动 no-op**：单测/评测里绝不真的起后台线程（否则定时器会在
  用例之间偷偷跑动，制造"连跑三次结果不同"的 flaky）。判定用 sys.modules
  有无 pytest——比环境变量可靠（CI 与本地一致）。
- 每个任务一条 daemon 线程 + `threading.Event`：`stop_all()` set 事件后
  `Event.wait(interval)` 立即返回，收尾快且不需要轮询。
- 单次执行失败不退出循环（与 generation_recovery 既有纪律一致）：一次数据库
  抖动不能让任务永久停摆；异常记 warning 后继续等下一轮。
- `startup_delay` 与 `run_on_start` 分开：generation_recovery 需要在服务
  就绪后稍等再扫（对应 Java ApplicationReadyEvent），usage 清理可以立刻跑。

依赖：仅标准库。
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CronTask:
    name: str
    interval_seconds: float
    fn: Callable[[], object]
    startup_delay_seconds: float = 0.0
    thread: threading.Thread | None = field(default=None, repr=False)


_tasks: dict[str, CronTask] = {}
_stop = threading.Event()
_started = False
_lock = threading.Lock()


def _in_pytest() -> bool:
    return "pytest" in sys.modules


def register(
    name: str,
    interval_seconds: float,
    fn: Callable[[], object],
    *,
    startup_delay_seconds: float = 0.0,
) -> CronTask:
    """登记一个周期任务（重复登记同名任务会覆盖，便于测试与热改）。"""
    task = CronTask(
        name=name,
        interval_seconds=max(float(interval_seconds), 0.1),
        fn=fn,
        startup_delay_seconds=max(float(startup_delay_seconds), 0.0),
    )
    with _lock:
        _tasks[name] = task
    return task


def registered() -> list[str]:
    with _lock:
        return sorted(_tasks)


def _run(task: CronTask) -> None:
    logger.info(
        "cron[%s] starting (first run in %ss, then every %ss)",
        task.name,
        task.startup_delay_seconds,
        task.interval_seconds,
    )
    deadline = task.startup_delay_seconds
    while not _stop.wait(deadline):
        deadline = task.interval_seconds
        try:
            task.fn()
        except Exception as exc:
            logger.warning("cron[%s] run failed: %s", task.name, exc)


def start_all() -> list[str]:
    """启动全部已登记任务；pytest 环境直接 no-op（返回空列表）。

    首次调用生效；重复调用只返回已启动的任务名，不会重复起线程。
    """
    global _started
    with _lock:
        if _in_pytest():
            logger.info("cron: pytest environment detected, all tasks are no-op")
            return []
        if _started:
            return sorted(name for name, task in _tasks.items() if task.thread is not None)
        _started = True
        _stop.clear()
        started: list[str] = []
        for name, task in _tasks.items():
            thread = threading.Thread(target=_run, args=(task,), name=f"cron-{name}", daemon=True)
            task.thread = thread
            thread.start()
            started.append(name)
        return sorted(started)


def stop_all() -> None:
    """通知全部任务退出并等待收尾（Event.wait 立即返回，不需要 join 超时兜底）。"""
    global _started
    _stop.set()
    with _lock:
        threads = [task.thread for task in _tasks.values() if task.thread is not None]
        _started = False
    for thread in threads:
        thread.join(timeout=5.0)
    with _lock:
        for task in _tasks.values():
            task.thread = None


def reset() -> None:
    """测试用：清空登记与停止状态。"""
    global _started
    with _lock:
        _tasks.clear()
        _started = False
        _stop.clear()
