"""一次 Agent 运行的硬性边界：Deadline、模型调用、检索、研究子预算、存在性解析、Token 预算。

没有 active run 时函数保持兼容，不会限制离线工具单测；HTTP 入口通过
``observe_run`` 自动启用。限制由执行器检查，而不是只写在 Prompt 里。

成本准入（V17/T2）也挂在这条接缝上：`reserve_llm` 在**外呼之前**把预留交给持久账本
（`app/services/cost_ledger.py`，两条传输路径见该文件 docstring），`settle_llm` 用真实
usage 结算、usage 未知时保留保守值。上限比较发生在账本的守卫更新里，所以
「并发不超卖」「一笔预留恰好结算一次」「崩溃未结算有界」都是同一条接缝的性质，
不是每个入口各自自觉调用的闸门。
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.common import cost_policy as policy
from app.common.config import settings
from app.common.llm_route import current_route, route_degradation

logger = logging.getLogger(__name__)

#: 成本账本后端：由 services 侧在启动时注入（`cost_ledger.install_backend`）。
#: 有规划任务的腿走 `persist`（预算与成本同一条写入线），没有任务的腿走这里——
#: 两者最终都进同一个 `cost_ledger.apply_events`，只有事务边界不同。
_cost_backend: Callable[[list[dict]], None] | None = None


def set_cost_backend(backend: Callable[[list[dict]], None] | None) -> None:
    """登记/撤除成本账本后端（测试与 lifespan 用；agent 层不反向 import services）。"""
    global _cost_backend
    _cost_backend = backend


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
    # 待交给持久账本的成本事件（预留/结算）。只在 `_save()` 成功后清空：落账失败时这批
    # 事件必须留给下一次写入重试，与 `_persist` 的"基线只在成功后推进"同一条纪律。
    _cost_events: list[dict] = field(default_factory=list, repr=False)
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
        """把快照交给持久面；有成本事件时它们**必须**跟着一起落（成本先于外呼定稿）。

        两条传输路径共用 services 的唯一实现：有任务 → `persist`（预算 + 续租 + 失权判定），
        没任务 → 启动时登记的 `cost_backend`。两者都不在（离线脚本/单测没有业务库）时，
        第一次丢弃会留一条 WARNING：硬费用账不能"没人接就走静默"。
        """
        events = self._cost_events
        if self.persist is None and not events:
            return
        if self.persist is not None:
            self.persist(self.snapshot())
        elif _cost_backend is not None:
            _cost_backend(list(events))
        else:
            _note_cost_dropped(len(events))
        self._cost_events = []

    def _cost_event(
        self, op: str, key: str, prompt: int, completion: int, usage: dict | None = None
    ) -> tuple[dict, bool]:
        """构造一条成本事件，并回答"这次结算拿到的是真 usage 还是保守值"。

        归属就在发起点判：`current_route()` 非空 = 用户自带网关（BYOK），
        空 = 运营方 key。降级原因（BYOK 查不到而回落）由 `llm_route` 的上下文带出来，
        账记 platform 是真话、但原因必须留痕——静默转嫁是 PLAN §5 T2 点名的失败模式。
        """
        from app.agent.runtime.observability import current_scene  # 延迟导入：observability 依赖本模块

        route = current_route()
        event = {
            "op": op,
            "id": key,
            "attribution": policy.ATTR_BYOK if route is not None else policy.ATTR_PLATFORM,
            "user_id": self.user_id,
            "itinerary_id": self.itinerary_id,
            "task_id": self.task_id,
            "scene": current_scene(),
        }
        if op == "reserve":
            degraded = route_degradation()
            if degraded:
                event["note"] = degraded
            event["prompt"], event["completion"] = max(0, prompt), max(0, completion)
            return event, False
        charged_prompt, charged_completion, complete = policy.settle_charge(prompt, completion, usage)
        event["prompt"], event["completion"], event["usage_complete"] = charged_prompt, charged_completion, complete
        return event, complete

    def reserve_llm(self, prompt_estimate: int, output_tokens: int) -> str:
        with self._lock:
            self.check("llm")
            prompt, output = max(0, prompt_estimate), max(0, output_tokens)
            if self.max_tokens > 0 and self.prompt_tokens + self.completion_tokens + prompt + output > self.max_tokens:
                raise RunLimitExceeded("Token 预算不足以发起本次请求")
            key = uuid.uuid4().hex
            event, _ = self._cost_event("reserve", key, prompt, output)
            self.llm_calls += 1
            self.prompt_tokens += prompt
            self.completion_tokens += output
            self._reservations[key] = (prompt, output)
            self._cost_events.append(event)
            try:
                self._save()  # 持久预留：请求还没出去，账已经落定（触顶即抛，下面一起回退）
            except BaseException:
                self._cost_events = [item for item in self._cost_events if item["id"] != key]
                del self._reservations[key]
                self.llm_calls -= 1
                self.prompt_tokens -= prompt
                self.completion_tokens -= output
                raise
            return key

    def settle_llm(self, reservation: str, usage: dict | None) -> None:
        with self._lock:
            reserved = self._reservations.pop(reservation, None)
            if reserved is None:
                return  # 幂等：没有预留可结算（已结算过 / 从未持久化）就不动账
            event, complete = self._cost_event("settle", reservation, reserved[0], reserved[1], usage)
            if not complete:
                self.usage_unknown += 1
            else:
                self.prompt_tokens += event["prompt"] - reserved[0]
                self.completion_tokens += event["completion"] - reserved[1]
            self._cost_events.append(event)
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
            # 待落账的成本事件（预留/结算）。持久化时由 services 侧剥掉再进 budget_json：
            # 它是"这一次写入要干的事"，不是"当前状态"，留在快照里会被接管后的新 worker 重放。
            "cost_events": list(self._cost_events),
        }


_cost_drop_noted = False


def _note_cost_dropped(count: int) -> None:
    """没有持久面可接成本事件时留一次痕迹（离线脚本/单测的正常形态，生产不该出现）。

    只告警一次、不抛错：抛错会让"没有业务库的离线工具"直接不能跑，而那正是
    `RunLimits` 无 active run 时保持兼容的既有口径。真正的硬顶在 services 侧装好后生效。
    """
    global _cost_drop_noted
    if _cost_drop_noted or count <= 0:
        return
    _cost_drop_noted = True
    logger.warning(
        "no durable cost backend installed; %d cost event(s) stayed in-memory only "
        "(expected for offline scripts/tests, NOT for the served app)",
        count,
    )


def cost_dropped_noted() -> bool:
    """上述告警是否已经发生过（测试与深探读它，不用去抓日志）。"""
    return _cost_drop_noted


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
