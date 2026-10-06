"""一次 Agent 运行的硬性边界：Deadline、模型调用、检索、研究子预算、存在性解析、Token 预算。

没有 active run 时函数保持兼容，不会限制离线工具单测；HTTP 入口通过
``observe_run`` 自动启用。限制由执行器检查，而不是只写在 Prompt 里。
"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field

from app.common.config import settings


class RunLimitExceeded(ValueError):
    """一次运行触达 Deadline、模型调用或 Token 预算。"""


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

    def check(self, kind: str) -> None:
        if self.deadline_seconds > 0 and time.monotonic() - self.started_at >= self.deadline_seconds:
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
        self.retrievals += max(1, int(n or 1))
        if self.max_retrievals > 0 and self.retrievals > self.max_retrievals:
            raise RunLimitExceeded("检索预算已耗尽")

    def record_research(self, n: int = 1) -> None:
        """记录一次研究取数；超预算抛出，由研究阶段带着已有证据降级。"""
        self.research_calls += max(1, int(n or 1))
        if self.max_research_calls > 0 and self.research_calls > self.max_research_calls:
            raise RunLimitExceeded("研究额度已用完")

    def record_existence(self, n: int = 1) -> None:
        """记录一次存在性解析；超预算抛出，由调用方降级为 UNKNOWN（不得当成不存在）。"""
        self.existence_checks += max(1, int(n or 1))
        if self.max_existence_checks > 0 and self.existence_checks > self.max_existence_checks:
            raise RunLimitExceeded("存在性解析预算已耗尽")

    def record_llm(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.llm_calls += 1
        self.prompt_tokens += max(int(prompt_tokens or 0), 0)
        self.completion_tokens += max(int(completion_tokens or 0), 0)
        if self.max_tokens > 0 and self.prompt_tokens + self.completion_tokens > self.max_tokens:
            raise RunLimitExceeded("Token 预算已耗尽")

    def record_replan(self, progressed: bool) -> None:
        self.replans += 1
        self.no_progress_count = 0 if progressed else self.no_progress_count + 1
        if self.no_progress_count >= settings.no_progress_limit:
            raise RunLimitExceeded("检测到连续重规划无进展")

    def snapshot(self) -> dict:
        elapsed = time.monotonic() - self.started_at
        return {
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
    return _active_limits.set(RunLimits())


def end_limits(token: contextvars.Token) -> None:
    _active_limits.reset(token)


def current_limits() -> RunLimits | None:
    return _active_limits.get()
