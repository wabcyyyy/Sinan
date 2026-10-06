"""Agent 运行指标与安全轨迹收口。

默认使用进程内聚合，适合本地演示和单实例部署；生产多实例时应把同样的计数器
接入 Prometheus/OTel。指标不保存 Prompt、用户原文或完整计划。
"""

from __future__ import annotations

import contextvars
import functools
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Lock
from typing import Any

from app.agent.runtime.run_limits import begin_limits, current_limits, end_limits
from app.agent.runtime.tool_budget import begin_tool_budget, end_tool_budget
from app.agent.runtime.trace import TraceRecorder, trace_run
from app.agent.runtime.trace_store import TraceStore
from app.common.config import settings

# 场景标记：用于 token 用量按业务场景拆分（generate/clarify/chat/assist/other）。
_active_scene: contextvars.ContextVar[str | None] = contextvars.ContextVar("active_scene", default=None)


def current_scene() -> str:
    return _active_scene.get() or "other"


@contextmanager
def use_scene(scene: str) -> Iterator[None]:
    token = _active_scene.set(scene)
    try:
        yield
    finally:
        _active_scene.reset(token)


def scene(name: str) -> Callable:
    """路由装饰器：把 handler 内的 LLM 调用归到指定业务场景。"""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with use_scene(name):
                return fn(*args, **kwargs)

        return wrapper

    return decorator


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = Lock()
        self._counts = {
            "runs": 0,
            "successes": 0,
            "degraded_runs": 0,
            # 客户端断开导致的取消：用户/连接侧行为，既不算成功也不算失败
            "cancelled_runs": 0,
            "failures": 0,
            "llm_calls": 0,
            "tool_calls": 0,
            "validation_retries": 0,
            "fallback_runs": 0,
            "route_violations": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "mcp_calls": 0,
            "mcp_failures": 0,
            "retrieval_calls": 0,
            "retrieval_fallbacks": 0,
            "retrieval_cache_hits": 0,
            "route_enumerate": 0,
            "route_lexical": 0,
            "route_hybrid": 0,
            "tool_budget_exhausted": 0,
            # 多 Agent 研究编排（P3）：证据包规模/推理轮次/降级/补查次数
            "research_items": 0,
            "research_rounds": 0,
            "research_degraded": 0,
            "research_refills": 0,
        }
        self._latency_ms = 0.0
        self._last_failures: list[dict] = []
        self._traces: dict[str, dict] = {}
        # 按 name 计数的 decision 事件（如 llm_json_repair）——修复率此前只进
        # trace 无归集出口，"过去一个月修复率"无人能答（P1-6）。进程内聚合，
        # 重启丢失可接受。
        self._decision_counts: dict[str, int] = {}
        # 按业务场景拆分的 token 用量：scene -> {llm_calls, prompt_tokens, completion_tokens}
        self._scene_counts: dict[str, dict[str, int]] = {}
        # 最近 60 分钟逐分钟 token 消耗（内存环形桶，Agent 重启清零）
        self._timeline: deque[dict] = deque(maxlen=60)
        self._store = TraceStore(settings.trace_storage_path) if settings.trace_storage_enabled else None

    def record_llm_call(self, prompt_tokens: int, completion_tokens: int) -> None:
        """登记一次真实 LLM 调用的 token 用量（由 llm_client 统一上报，覆盖未包 trace 的调用）。"""
        prompt = max(int(prompt_tokens or 0), 0)
        completion = max(int(completion_tokens or 0), 0)
        scene = current_scene()
        minute = int(time.time() // 60)
        with self._lock:
            self._counts["llm_calls"] += 1
            self._counts["prompt_tokens"] += prompt
            self._counts["completion_tokens"] += completion
            bucket = self._scene_counts.setdefault(scene, {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            bucket["llm_calls"] += 1
            bucket["prompt_tokens"] += prompt
            bucket["completion_tokens"] += completion
            if not self._timeline or self._timeline[-1]["ts"] != minute:
                self._timeline.append({"ts": minute, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            slot = self._timeline[-1]
            slot["calls"] += 1
            slot["prompt_tokens"] += prompt
            slot["completion_tokens"] += completion

    def record_research(self, *, items: int, rounds: int, degraded: int) -> None:
        """登记一次研究阶段汇总：证据包规模/推理轮次/降级包数（由 Supervisor 上报）。"""
        with self._lock:
            self._counts["research_items"] += max(int(items), 0)
            self._counts["research_rounds"] += max(int(rounds), 0)
            self._counts["research_degraded"] += max(int(degraded), 0)

    def record_research_refill(self) -> None:
        """登记一次 Supervisor 缺口补查（refill 路由）。"""
        with self._lock:
            self._counts["research_refills"] += 1

    def record(self, trace: dict, *, success: bool) -> None:
        events = trace.get("events") or []
        durations = [float(e.get("duration_ms") or 0) for e in events]
        status_events = [e for e in events if e.get("kind") == "decision" and e.get("name") == "run_status"]
        run_status = (status_events[-1].get("metadata") or {}).get("status") if status_events else None
        is_degraded = run_status == "degraded"
        is_cancelled = run_status == "cancelled"
        is_failed = not success or run_status == "failed"
        delta = {
            "runs": 1,
            # 三态互斥：fallback 完成属于 degraded，不计入 successes；
            # cancelled（客户端断开）单列，不计成功也不计失败。
            "successes": int(success and not is_degraded and not is_failed and not is_cancelled),
            "failures": int(is_failed),
            "cancelled_runs": int(is_cancelled),
            # llm_calls / token 用量由 llm_client.record_llm_call 直接上报，
            # 避免遗漏未包 trace 的调用（clarify/city-guide 等）造成双算或漏算。
            "tool_calls": sum(e.get("kind") == "tool" for e in events),
            "validation_retries": sum(e.get("kind") == "route" and e.get("name") in ("fix", "retry") for e in events),
            "fallback_runs": sum(e.get("kind") == "route" and e.get("name") == "fallback" for e in events),
            "route_violations": sum(
                e.get("kind") == "decision"
                and e.get("name") in ("reflect_result", "day.reflect_result")
                and (e.get("metadata") or {}).get("needs_fix") is True
                for e in events
            ),
            "mcp_calls": sum(e.get("kind") == "mcp" for e in events),
            "mcp_failures": sum(e.get("kind") == "mcp" and e.get("status") == "error" for e in events),
            "retrieval_calls": sum(e.get("kind") == "retrieval" for e in events),
            "retrieval_fallbacks": sum(
                e.get("kind") == "retrieval" and (e.get("metadata") or {}).get("fallback") is True for e in events
            ),
            # P2 检索路由/缓存：命中率与路由分布，供缓存调优与回归对比。
            "retrieval_cache_hits": sum(e.get("kind") == "retrieval" and e.get("name") == "cache_hit" for e in events),
            "route_enumerate": sum(
                e.get("kind") == "retrieval" and (e.get("metadata") or {}).get("route") == "enumerate" for e in events
            ),
            "route_lexical": sum(
                e.get("kind") == "retrieval" and (e.get("metadata") or {}).get("route") == "lexical" for e in events
            ),
            "route_hybrid": sum(
                e.get("kind") == "retrieval" and (e.get("metadata") or {}).get("route") == "hybrid" for e in events
            ),
            "tool_budget_exhausted": sum(
                e.get("kind") == "tool" and (e.get("metadata") or {}).get("budget_exhausted") is True for e in events
            ),
            "degraded_runs": int(is_degraded),
        }
        with self._lock:
            for key, value in delta.items():
                self._counts[key] += value
            # decision 事件按 name 归集（llm_json_repair_total 等，见 snapshot）
            for event in events:
                if event.get("kind") == "decision":
                    name = str(event.get("name") or "")
                    if name:
                        self._decision_counts[name] = self._decision_counts.get(name, 0) + 1
            self._latency_ms += sum(durations)
            run_id = trace.get("run_id")
            if run_id:
                # 只保留最近 100 条脱敏轨迹，避免进程内指标无限增长。
                self._traces[run_id] = trace
                self._traces = dict(list(self._traces.items())[-100:])
                if self._store is not None:
                    self._store.append(trace)
            if is_failed:
                self._last_failures.append(
                    {
                        "run_id": trace.get("run_id"),
                        "events": [
                            {
                                "kind": e.get("kind"),
                                "name": e.get("name"),
                                "status": e.get("status"),
                                "error": e.get("error"),
                            }
                            for e in events
                            if e.get("status") == "error"
                        ],
                    }
                )
                self._last_failures = self._last_failures[-20:]

    def snapshot(self) -> dict:
        with self._lock:
            # 值域是混合的（计数、比率、列表、嵌套 dict），显式标注以免类型推断
            # 收窄成 dict[str, int] 后在附加 decisions_by_name 时报错。
            data: dict[str, Any] = dict(self._counts)
            data["success_rate"] = round(data["successes"] / data["runs"], 4) if data["runs"] else 0.0
            data["degraded_rate"] = round(data["degraded_runs"] / data["runs"], 4) if data["runs"] else 0.0
            data["failure_rate"] = round(data["failures"] / data["runs"], 4) if data["runs"] else 0.0
            data["mcp_failure_rate"] = round(data["mcp_failures"] / data["mcp_calls"], 4) if data["mcp_calls"] else 0.0
            data["avg_event_latency_ms"] = round(self._latency_ms / data["runs"], 2) if data["runs"] else 0.0
            data["recent_failures"] = list(self._last_failures)
            data["llm_json_repair_total"] = self._decision_counts.get("llm_json_repair", 0)
            data["decisions_by_name"] = dict(self._decision_counts)
            data["tokens_by_scene"] = {k: dict(v) for k, v in self._scene_counts.items()}
            data["tokens_timeline"] = [dict(slot) for slot in self._timeline]
            try:
                from app.common import db_pool

                data["db_pool"] = db_pool.pool_stats()
            except Exception:
                data["db_pool"] = None
            return data

    def get_trace(self, run_id: str) -> dict | None:
        """按运行 ID 查询最近的脱敏轨迹。"""
        with self._lock:
            trace = self._traces.get(run_id)
            if trace is not None:
                return dict(trace)
        return self._store.get(run_id) if self._store is not None else None

    def reset(self) -> None:
        with self._lock:
            for key in self._counts:
                self._counts[key] = 0
            self._latency_ms = 0.0
            self._last_failures.clear()
            self._traces.clear()
            self._decision_counts.clear()
            self._scene_counts.clear()
            self._timeline.clear()

    def prometheus_text(self) -> str:
        snapshot = self.snapshot()
        lines = ["# HELP travel_agent_runs_total Agent runs.", "# TYPE travel_agent_runs_total counter"]
        for key, value in snapshot.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metric = "travel_agent_" + key.replace(".", "_")
                lines.extend([f"# TYPE {metric} gauge", f"{metric} {value}"])
        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()


@contextmanager
def observe_run(
    run_id: str | None = None, request_id: str | None = None, action_id: str | None = None
) -> Iterator[TraceRecorder]:
    budget_token = begin_tool_budget()
    limits_token = begin_limits()
    with trace_run(run_id, request_id, action_id) as recorder:
        try:
            yield recorder
        except Exception as exc:
            limits = current_limits()
            recorder.record(
                "decision",
                "run_status",
                status="error",
                metadata={
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "limits": limits.snapshot() if limits else {},
                },
                error=str(exc),
            )
            recorder.record("decision", "run_limits", metadata=limits.snapshot() if limits else {})
            metrics.record(recorder.to_dict(), success=False)
            raise
        else:
            limits = current_limits()
            recorder.record("decision", "run_limits", metadata=limits.snapshot() if limits else {})
            metrics.record(recorder.to_dict(), success=True)
        finally:
            end_limits(limits_token)
            end_tool_budget(budget_token)
