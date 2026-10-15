"""一次 Agent 运行的硬性边界：Deadline、模型调用、检索、研究子预算、存在性解析、Token 预算。

没有 active run 时函数保持兼容，不会限制离线工具单测；HTTP 入口通过
``observe_run`` 自动启用。限制由执行器检查，而不是只写在 Prompt 里。
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.common.config import settings


class RunLimitExceeded(ValueError):
    """一次运行触达 Deadline、模型调用或 Token 预算。"""


class TaskStopped(RuntimeError):
    """任务被外部介入，整趟必须停止（失权或用户取消）。

    刻意**不**继承 `RunLimitExceeded`：预算耗尽是"诚实降级继续交付"的信号，研究段等处
    会捕获它并带着已有证据收尾；失权/取消则意味着这个 worker 已经没有资格再写任何东西，
    被降级分支吞掉就等于继续双写。两者必须靠类型区分，而不是靠 message 猜。
    """


class TaskOwnershipLost(TaskStopped):
    """本 worker 不再是该任务的持有者（被接管、已终态或进程身份已换）。"""


class TaskCancelled(TaskStopped):
    """用户请求取消：停止后续外呼与写入，由编排负责收敛终态。"""


@dataclass
class RunLimits:
    started_at: float = field(default_factory=time.monotonic)
    deadline_seconds: float = settings.agent_deadline_seconds
    max_llm_calls: int = settings.max_llm_calls
    max_tokens: int = settings.max_token_budget
    max_replans: int = settings.max_replans
    # 检索/证据类调用上限（研究补查、联网搜索、外部点位 API），防止无界放大
    max_retrievals: int = settings.max_retrievals
    # 研究阶段自己的额度：`max_retrievals` 是全 run 共用的，三域研究只受它约束时
    # 能把生成与落地的份数一并吃光（详见 settings.research_call_limit 的实测注释）。
    # 耗尽不是失败：研究带着已收集的证据如实降级，剩下的额度留给生成。0 = 不限。
    max_research_calls: int = settings.research_call_limit
    # 存在性解析上限（PLAN-A1 G2）：一次 run 最多问多少个点位名"真的存在吗"。
    # 实测一天行程约 30 个唯一名字，默认 24 让主行程基本覆盖、备选池有界。
    max_existence_checks: int = settings.existence_resolve_limit
    llm_calls: int = 0
    retrievals: int = 0
    research_calls: int = 0
    existence_checks: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    replans: int = 0
    no_progress_count: int = 0

    task_id: str | None = None
    user_id: int | None = None
    itinerary_id: int | None = None
    submitted_at: float = field(default_factory=time.time)
    # 绝对截止（epoch 秒）。持久化任务必填：预算剩余只由它决定，与"这个进程活了多久"无关——
    # 从 submitted_at 反推 monotonic 起点是旧 task_budget 的做法，重启越晚越容易把还在
    # 有效期内的任务误判成过期（V16/T1 要消除的那条）。None = 无持久化任务（离线/单测）。
    deadline_at: float | None = None
    usage_unknown: int = 0
    terminal_resets: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _reservations: dict[str, tuple[int, int]] = field(default_factory=dict, repr=False)
    persist: Callable[[dict], None] | None = field(default=None, repr=False)
    # 失权/取消探针：由 services 侧注入（agent 不反向依赖 services）。check() 每次调用它，
    # 探针内部的缓存与代价由注入方决定。
    guard: Callable[[], None] | None = field(default=None, repr=False)

    def remaining(self) -> float:
        if self.deadline_at is not None:
            remaining = self.deadline_at - time.time()
        elif self.deadline_seconds > 0:
            remaining = self.deadline_seconds - (time.monotonic() - self.started_at)
        else:
            remaining = float("inf")
        if self.task_id:
            remaining -= 5  # 留给终态事务的收尾窗口，不再用于外呼。
        phase = _phase_deadline.get()
        return min(remaining, phase - time.monotonic()) if phase else remaining

    def _save(self) -> None:
        if self.persist is not None:
            self.persist(self.snapshot())

    def reserve_llm(self, prompt_estimate: int, output_tokens: int) -> str:
        with self._lock:
            self.check("llm")
            prompt, output = max(0, prompt_estimate), max(0, output_tokens)
            if self.max_tokens > 0 and self.prompt_tokens + self.completion_tokens + prompt + output > self.max_tokens:
                raise RunLimitExceeded("Token 预算不足以发起本次请求")
            key = uuid.uuid4().hex
            self.llm_calls += 1
            self.prompt_tokens += prompt
            self.completion_tokens += output
            self._reservations[key] = (prompt, output)
            self._save()
            return key

    def settle_llm(self, reservation: str, usage: dict | None) -> None:
        with self._lock:
            reserved = self._reservations.pop(reservation, None)
            if reserved is None:
                return
            if usage is None or "prompt_tokens" not in usage or "completion_tokens" not in usage:
                self.usage_unknown += 1
            else:
                self.prompt_tokens += max(0, int(usage["prompt_tokens"])) - reserved[0]
                self.completion_tokens += max(0, int(usage["completion_tokens"])) - reserved[1]
            self._save()
            if self.max_tokens > 0 and self.prompt_tokens + self.completion_tokens > self.max_tokens:
                raise RunLimitExceeded("供应商报告用量超过预留预算")

    def _reserve_counter(self, field_name: str, maximum: int, reason: str, n: int = 1) -> None:
        with self._lock:
            self.check("deadline")
            value = getattr(self, field_name) + max(1, int(n or 1))
            if maximum > 0 and value > maximum:
                raise RunLimitExceeded(reason)
            setattr(self, field_name, value)
            self._save()

    def check(self, kind: str) -> None:
        # 先问持久面：失权或已取消的任务不该再发起外呼（也不该继续写库）。
        # 探针由 services 注入并自带缓存窗口，这里不重复读库。
        if self.guard is not None:
            self.guard()
        if self.remaining() <= 0:
            raise RunLimitExceeded(f"Agent 总 Deadline 已到达（{kind}）")
        if kind == "llm" and self.llm_calls >= self.max_llm_calls:
            raise RunLimitExceeded("模型调用预算已耗尽")
        if kind == "retrieval" and self.max_retrievals > 0 and self.retrievals >= self.max_retrievals:
            raise RunLimitExceeded("检索预算已耗尽")
        if kind == "research" and self.max_research_calls > 0 and self.research_calls >= self.max_research_calls:
            raise RunLimitExceeded("研究额度已用完")
        if kind == "existence" and self.max_existence_checks > 0 and self.existence_checks >= self.max_existence_checks:
            raise RunLimitExceeded("存在性解析预算已耗尽")
        if kind == "replan" and self.replans >= self.max_replans:
            raise RunLimitExceeded("局部重规划次数已耗尽")

    def record_retrieval(self, n: int = 1) -> None:
        """记录检索/证据类调用；超预算抛出，由调用方决定是否降级。"""
        self._reserve_counter("retrievals", self.max_retrievals, "检索预算已耗尽", n)

    def record_research(self, n: int = 1) -> None:
        self._reserve_counter("research_calls", self.max_research_calls, "研究额度已用完", n)

    def record_existence(self, n: int = 1) -> None:
        self._reserve_counter("existence_checks", self.max_existence_checks, "存在性解析预算已耗尽", n)

    def record_llm(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        reservation = self.reserve_llm(prompt_tokens, completion_tokens)
        self.settle_llm(reservation, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens})

    def record_replan(self, progressed: bool) -> None:
        with self._lock:
            self.check("replan")
            self.replans += 1
            self.no_progress_count = 0 if progressed else self.no_progress_count + 1
            self._save()
            if self.no_progress_count >= settings.no_progress_limit:
                raise RunLimitExceeded("检测到连续重规划无进展")

    def record_terminal_reset(self) -> None:
        with self._lock:
            self.terminal_resets += 1
            self._save()

    def snapshot(self) -> dict:
        elapsed = time.monotonic() - self.started_at
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "itinerary_id": self.itinerary_id,
            "submitted_at": self.submitted_at,
            "deadline_at": self.deadline_at,
            "usage_unknown": self.usage_unknown,
            "terminal_resets": self.terminal_resets,
            "elapsed_ms": round(elapsed * 1000, 2),
            "deadline_seconds": self.deadline_seconds,
            "llm_calls": self.llm_calls,
            "max_llm_calls": self.max_llm_calls,
            "retrievals": self.retrievals,
            "max_retrievals": self.max_retrievals,
            "research_calls": self.research_calls,
            "max_research_calls": self.max_research_calls,
            "existence_checks": self.existence_checks,
            "max_existence_checks": self.max_existence_checks,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "max_tokens": self.max_tokens,
            "replans": self.replans,
            "max_replans": self.max_replans,
            "no_progress_count": self.no_progress_count,
        }


_active_limits: contextvars.ContextVar[RunLimits | None] = contextvars.ContextVar(
    "active_agent_run_limits", default=None
)


def begin_limits() -> contextvars.Token:
    return _active_limits.set(current_limits() or RunLimits())


def end_limits(token: contextvars.Token) -> None:
    _active_limits.reset(token)


def current_limits() -> RunLimits | None:
    return _active_limits.get()


_phase_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("agent_phase_deadline", default=None)


@contextmanager
def phase_scope(seconds: float) -> Iterator[None]:
    previous = _phase_deadline.get()
    deadline = time.monotonic() + seconds
    token = _phase_deadline.set(min(previous, deadline) if previous else deadline)
    try:
        yield
    finally:
        _phase_deadline.reset(token)


@contextmanager
def use_task_limits(limits: RunLimits) -> Iterator[RunLimits]:
    token = _active_limits.set(limits)
    try:
        yield limits
    finally:
        _active_limits.reset(token)


def check_task_write() -> None:
    limits = current_limits()
    if limits is not None and limits.task_id:
        limits.check("write")
