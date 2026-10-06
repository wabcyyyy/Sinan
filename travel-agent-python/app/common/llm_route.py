"""LLM 路由上下文（BYOK 网关）：ContextVar 携带"当前这次 LLM 调用走谁的网关"。

职责边界（防环，机检之外的硬约束）：
- 本模块**禁止** import app.services / app.agent——services→common 依赖链早已密集
  （envelope/config/session 全线），反向即成环。"哪个用户的网关生效"的解析是
  service 层职责（llm_gateway_service.route_scope），common 只放路由对象与上下文原语。
- LLMRoute 携带明文 api_key：它只允许出现在上游请求头构造的内存瞬间（llm_client），
  不进日志/异常/VO。

注入点纪律：Python 3.12 的 contextvars **不随 ThreadPoolExecutor/Thread 传播**（实测，
executor 与裸线程内均读到默认值），而全部 worker 入口（plan_days / _stream_turn /
recover_one / enrich_itinerary…）都显式持有 user_id——所以 use_route 必须在这些入口
内部进入，与既有 use_scene/observe_run 就地成对的惯例对称。`use_route(None)` 显式
压栈是合法的（子树强制回默认通道），嵌套压栈退出自动还原。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMRoute:
    """一次 BYOK 路由的三要素 + 展示用 label（不含密钥材料的日志安全名）。"""

    base_url: str
    # repr=False：dataclass 自动 __repr__ 含全字段，一行 logger.debug("%s", route)
    # 或断言失败输出就会把明文密钥打进日志——与"路由不进日志"的红线只差一次
    # 误用，这里结构性堵死（2026-10-02 终审）。
    api_key: str = field(repr=False)
    model: str
    label: str = ""


_route_var: ContextVar[LLMRoute | None] = ContextVar("ta_llm_route", default=None)


def current_route() -> LLMRoute | None:
    """当前上下文生效的 BYOK 路由；None = 默认通道（settings 主配置单例）。"""
    return _route_var.get()


@contextmanager
def use_route(route: LLMRoute | None) -> Iterator[LLMRoute | None]:
    """把路由压进上下文（None 也是显式压栈：子树强制默认通道），退出还原。"""
    token = _route_var.set(route)
    try:
        yield route
    finally:
        _route_var.reset(token)
