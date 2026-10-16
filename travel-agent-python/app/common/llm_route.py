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
# 归属降级原因（V17/T2）：用户配了 BYOK 网关、但**查它这件事本身**失败（网关表读不出来），
# 于是这一腿回落到运营方 key。账必须记 platform（那是真话），但原因得留痕——
# 「路由故障悄悄把用户的成本转给平台」是 PLAN §5 T2 点名的失败模式。
_degradation_var: ContextVar[str | None] = ContextVar("ta_llm_route_degradation", default=None)


def current_route() -> LLMRoute | None:
    """当前上下文生效的 BYOK 路由；None = 默认通道（settings 主配置单例）。"""
    return _route_var.get()


def route_degradation() -> str | None:
    """当前路由上下文里的归属降级原因；None = 没有降级。"""
    return _degradation_var.get()


def mark_route_degradation(reason: str) -> None:
    """登记一次归属降级（只有 `resolve_route` 的 fail-open 分支会调）。

    写在**当前上下文**里，紧跟着被 `use_route` 接走：线程池复用线程，
    不随 use_route 复位就会把上一条请求的"BYOK 查不到"抹到下一条正常路由的腿上。
    """
    _degradation_var.set(reason)


@contextmanager
def use_route(route: LLMRoute | None) -> Iterator[LLMRoute | None]:
    """把路由压进上下文（None 也是显式压栈：子树强制默认通道），退出还原。

    进入时接走同上下文里刚登记的降级原因：`route` 为空 + 有降级 = 用户配了 BYOK 但查不到
    而回落运营方 key（记账时留 `attribution_note`）；`route` 非空时不可能有降级，强制清空。
    """
    pending = _degradation_var.get()
    value_token = _route_var.set(route)
    degrade_token = _degradation_var.set(None if route is not None else pending)
    try:
        yield route
    finally:
        _degradation_var.reset(degrade_token)
        _route_var.reset(value_token)
