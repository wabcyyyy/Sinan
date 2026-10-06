"""工具调用预算：一次运行内"共调了几次工具、每个工具各几次"的计数状态。

为什么在 runtime：这是**运行边界**，与 ``run_limits`` 同层。observability 在一次运行
开始时开预算、tool_registry 在每次派发时占用额度，两侧都向下依赖本模块——工具面与
指标面因此互不 import（2026-09-23 域化改造前 observability 反向 import tool_registry，
把 runtime 拖到了 data 之上）。

口径不变：预算状态挂在 contextvar 上，未开预算的调用自建并在退出时还原。
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_budget: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("agent_tool_budget", default=None)


def begin_tool_budget() -> contextvars.Token:
    """开一次工具预算（observability 在运行入口调用），返回还原用的 token。"""
    return _budget.set({"total": 0, "by_tool": {}})


def end_tool_budget(token: contextvars.Token) -> None:
    _budget.reset(token)


def tool_budget_snapshot() -> dict[str, Any]:
    current = _budget.get() or {"total": 0, "by_tool": {}}
    return {"total": int(current.get("total", 0)), "by_tool": dict(current.get("by_tool", {}))}


@contextmanager
def tool_budget_scope() -> Iterator[dict[str, Any]]:
    """确保当前上下文有预算状态：本次运行已开则复用，未开则自建、退出时还原。

    yield 的是**可变状态本身**（与 ``begin_tool_budget`` 开的是同一份），调用方就地
    自增计数；判定与抛错留在注册表（错误类型属于工具面，不在 runtime）。
    """
    state = _budget.get()
    if state is not None:
        yield state
        return
    fresh: dict[str, Any] = {"total": 0, "by_tool": {}}
    token = _budget.set(fresh)
    try:
        yield fresh
    finally:
        _budget.reset(token)
