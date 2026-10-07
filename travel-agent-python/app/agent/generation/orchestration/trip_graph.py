"""统一生成图：整段行程（trip）与单日（day）共用一张 LangGraph StateGraph。

历史上 workflow 与 day_workflow 各持一张图，现已合并为 **一张**
``unified_agent_graph``，用 ``mode`` 字段分流（workflow 提供整段节点函数、
day_stream 提供逐日节点函数）：

```text
dispatch ──mode=stream──► stream_generate（见 stream_branch.py）──► END
        ├─mode=day──► day_generate ⇄ day.reflect/retry/fallback ──► END
        └─mode=trip─► parse → research → generate → reflect ⇄ fix/refill → format ──► END
```

事实层（Prompt/坐标落地）仍在 ``day_stream``；产品口径在 ``generation_core``。
``workflow.run_generate`` / ``day_workflow.run_day_agent`` 变为本图的薄门面，
HTTP 契约不变。测试可 monkeypatch ``day_workflow.generate_day_once``
（day 节点经该模块属性调用，保证补丁生效）。
"""

from __future__ import annotations

import logging
from typing import NamedTuple, cast
from uuid import uuid4

from langgraph.graph import END, StateGraph

from app.agent.generation.content.reflect import build_feedback, validate_plans
from app.agent.generation.orchestration.stream_branch import stream_generate
from app.agent.generation.rules.generation_core import MAX_DAY_ATTEMPTS
from app.agent.research.agent_state import MODE_DAY, MODE_STREAM, MODE_TRIP, UnifiedAgentState
from app.agent.runtime import checkpoint
from app.agent.runtime.trace import record_event, traced
from app.schemas.trip import DailyPlan, GenerateDayRequest, GenerateRequest, GenerateResponse

logger = logging.getLogger(__name__)


def _is_day(state: UnifiedAgentState) -> bool:
    return state.mode == MODE_DAY


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


def dispatch(state: UnifiedAgentState) -> dict:
    """入口：仅规范化 mode，便于条件边分流。"""
    mode = state.mode or MODE_TRIP
    return {"mode": mode}


def route_entry(state: UnifiedAgentState) -> str:
    if state.mode == MODE_STREAM:
        return "stream_generate"
    return "day_generate" if _is_day(state) else "parse"


# ---------------------------------------------------------------------------
# day 分支
# ---------------------------------------------------------------------------


@traced("node", "day.generate")
def day_generate(state: UnifiedAgentState) -> dict:
    # 经 day_workflow 模块属性调用，保证 tests monkeypatch 生效
    from app.agent.generation.orchestration import day_workflow as dw

    request: GenerateDayRequest = cast("GenerateDayRequest", state.day_request)
    feedback = state.feedback
    if feedback != request.feedback:
        request = request.model_copy(update={"feedback": feedback})
    try:
        plan, source = dw.generate_day_once(request, force_fallback=state.force_fallback)
        return {
            "day_request": request,
            "plan": plan,
            "source": source,
            "attempts": state.attempts + 1,
            "error": None,
        }
    except Exception as exc:
        return {
            "day_request": request,
            "plan": None,
            "source": "error",
            "attempts": state.attempts + 1,
            "error": str(exc),
        }


@traced("node", "day.reflect")
def day_reflect(state: UnifiedAgentState) -> dict:
    plan = state.plan
    if plan is None:
        return {
            "validation_issues": [state.error or "单日行程生成失败"],
            "validation_log": ["单日生成失败，准备重试或最后一次无反馈重试"],
            "feedback": state.error or "单日行程生成失败，请重新生成",
        }
    raw = plan.model_dump()
    request = state.day_request
    persons = getattr(request, "persons", 1) or 1 if request is not None else 1
    # BIZ-1：预算硬约束不在这里判——单天列表 days=1 的估算（交通×1、酒店恒计入）
    # 永远够不到「整趟预算×(1+超差比)」的门槛，传整趟预算等于恒不触发。预算超支由
    # **整趟口径**终检负责：业务面 _revalidate_stream_days 把全部天聚合成一次
    # validate_plans（BUDGET_OVERAGE_MARK 认领预算 issue），agent 面 trip 模式
    # reflect 节点全量校验。本节点维持时间/营业时段/饱和度的单天规则。
    issues, log = validate_plans([raw], persons=persons)
    # 空天必须进 issues：validate_plans 对无 items 只记 log（落库时由
    # day_delivery_gate 拦成 PENDING），但那样下面的 retry/fallback 环永远不触发
    # ——json_object 降级档的间歇空天（P2）就是空 plan 一次过直达 PARTIAL。
    # 补 issue 后重采样走既有机制：retry 带 feedback 再采样，耗尽后无反馈兜底一次。
    if not plan.items:
        issues = [*issues, f"第 {plan.day_no} 天没有生成任何条目，请重新生成本日完整行程（景点/餐饮与时间安排）"]
    record_event(
        "decision",
        "day.reflect_result",
        metadata={
            "issue_count": len(issues),
            "needs_fix": bool(issues),
        },
    )
    return {
        "validation_issues": issues,
        "validation_log": log,
        "feedback": build_feedback(issues),
    }


def day_route_after_reflect(state: UnifiedAgentState) -> str:
    if not state.validation_issues:
        record_event("route", "finish", metadata={"reason": "validation_pass"})
        return "finish"
    request = state.day_request
    # BIZ-2：重试预算看 per_day_chain 独立信号（单日链=MAX，整段链=1 次由外层管），
    # 不再从 days 推断——单日链现在会带真实 days 进来修预算分档
    if request is not None and request.per_day_chain:
        max_attempts = MAX_DAY_ATTEMPTS
    else:
        max_attempts = 1 if request is not None and (request.days or 1) > 1 else MAX_DAY_ATTEMPTS
    if state.attempts < max_attempts and not state.force_fallback:
        record_event("route", "retry", metadata={"reason": "validation_issue"})
        return "retry"
    if not state.force_fallback:
        record_event("route", "fallback", metadata={"reason": "retry_exhausted"})
        return "fallback"
    record_event("route", "finish", metadata={"reason": "fallback_result"})
    return "finish"


def day_prepare_retry(state: UnifiedAgentState) -> dict:
    return {"force_fallback": False}


def day_prepare_fallback(state: UnifiedAgentState) -> dict:
    return {"force_fallback": True, "feedback": ""}


# ---------------------------------------------------------------------------
# trip 分支：节点实现委托 workflow 模块（延迟导入避免环）
# ---------------------------------------------------------------------------


def _wf():
    from app.agent.generation.orchestration import workflow as wf

    return wf


@traced("node", "parse")
def parse(state: UnifiedAgentState) -> dict:
    return _wf().parse_requirements(state)


@traced("node", "research")
def research(state: UnifiedAgentState) -> dict:
    return _wf().research_pois(state)


@traced("node", "generate")
def trip_generate(state: UnifiedAgentState) -> dict:
    return _wf().generate_itinerary(state)


@traced("node", "reflect")
def trip_reflect(state: UnifiedAgentState) -> dict:
    return _wf().reflect(state)


@traced("node", "refill_research")
def trip_refill(state: UnifiedAgentState) -> dict:
    return _wf().research_refill(state)


@traced("node", "format")
def trip_format(state: UnifiedAgentState) -> dict:
    return _wf().format_output(state)


def trip_needs_fix(state: UnifiedAgentState) -> str:
    return _wf().needs_fix(state)


# ---------------------------------------------------------------------------
# 统一图
# ---------------------------------------------------------------------------


def build_unified_graph() -> StateGraph:
    graph = StateGraph(UnifiedAgentState)

    graph.add_node("dispatch", dispatch)

    # stream（PR-4 归一：原旁路 trip_stream 收编，逐天 custom stream，见 stream_branch）
    graph.add_node("stream_generate", stream_generate)
    graph.add_edge("stream_generate", END)

    # day
    graph.add_node("day_generate", day_generate)
    graph.add_node("day.reflect", day_reflect)
    graph.add_node("day.retry", day_prepare_retry)
    graph.add_node("day.fallback", day_prepare_fallback)

    # trip
    graph.add_node("parse", parse)
    graph.add_node("research", research)
    graph.add_node("generate", trip_generate)
    graph.add_node("reflect", trip_reflect)
    graph.add_node("refill_research", trip_refill)
    graph.add_node("format", trip_format)

    graph.set_entry_point("dispatch")
    graph.add_conditional_edges(
        "dispatch",
        route_entry,
        {
            "day_generate": "day_generate",
            "stream_generate": "stream_generate",
            "parse": "parse",
        },
    )

    # day edges
    graph.add_edge("day_generate", "day.reflect")
    graph.add_conditional_edges(
        "day.reflect",
        day_route_after_reflect,
        {
            "finish": END,
            "retry": "day.retry",
            "fallback": "day.fallback",
        },
    )
    graph.add_edge("day.retry", "day_generate")
    graph.add_edge("day.fallback", "day_generate")

    # trip edges
    graph.add_edge("parse", "research")
    graph.add_edge("research", "generate")
    graph.add_edge("generate", "reflect")
    graph.add_conditional_edges(
        "reflect",
        trip_needs_fix,
        {
            "fix": "generate",
            "refill": "refill_research",
            "pass": "format",
        },
    )
    graph.add_edge("refill_research", "generate")
    graph.add_edge("format", END)

    return graph


# 挂检查点（PR-3）：断点续跑与 time-travel 的地基（thread = 生成任务标识，
# 见 runtime/checkpoint.py 的 thread_id 约定）。checkpoint 自建 SQLite 表，
# 不动 MySQL 迁移（INV-3 不受影响）。
unified_agent_graph = build_unified_graph().compile(checkpointer=checkpoint.get_checkpointer())


def empty_trip_state(req: GenerateRequest) -> dict:
    return {
        "mode": MODE_TRIP,
        "request": req,
        "requirements": {},
        "candidates": [],
        "foods": [],
        "hotels": [],
        "consumption": None,
        "weather": None,
        "daily_plans": [],
        "budget_estimate": {},
        "attempts": 0,
        "fix_count": 0,
        "error": None,
        "feedback": "",
        "validation_issues": [],
        "validation_log": [],
        "degraded_reason": None,
        "schedule_report": {},
        "critic_report": {},
        "research_report": {},
        "refill_count": 0,
    }


def empty_day_state(req: GenerateDayRequest) -> dict:
    return {
        "mode": MODE_DAY,
        "day_request": req,
        "plan": None,
        "source": "",
        "attempts": 0,
        "force_fallback": False,
        "error": None,
        "feedback": req.feedback,
        "validation_issues": [],
        "validation_log": [],
    }


def run_trip(req: GenerateRequest, *, thread_id: str | None = None) -> GenerateResponse:
    config = checkpoint.run_config(thread_id or f"trip-{uuid4().hex}")
    result = unified_agent_graph.invoke(empty_trip_state(req), config)
    return result["result"]


def run_day(req: GenerateDayRequest, *, thread_id: str | None = None) -> DailyPlan:
    # 默认 thread = action_id（`day-{itinerary_id}-{day_no}`）：确定性任务标识，
    # 进程重启后恢复侧找得到（PR-3）；一次性调用（无 action_id）退化为随机 thread。
    config = checkpoint.run_config(thread_id or req.action_id or f"day-{uuid4().hex}")
    result = unified_agent_graph.invoke(empty_day_state(req), config)
    if result.get("plan") is None or not result["plan"].items:
        raise ValueError(result.get("error") or "单日行程生成失败")
    return result["plan"]


class DayResume(NamedTuple):
    """从检查点续出来的单日产出（PR-3）。

    plan=None = 续完仍无产出（交给兜底重排）；context = 该天携带的研究上下文——
    找回它，兜底排程就不必整段研究重跑（"僵尸整段续跑"的成本主体）。
    """

    plan: DailyPlan | None
    context: dict | None


def resume_run(thread_id: str) -> dict | None:
    """把断在图里的 run 从检查点**续跑到完成**；无可续检查点返回 None。

    已完成的超步不重跑（langgraph 检查点语义：只补没落盘的部分），所以续跑 ≠ 整段重来。
    """
    config = checkpoint.run_config(thread_id)
    if checkpoint.get_checkpointer().get(config) is None:
        return None
    return unified_agent_graph.invoke(None, config)


def resume_day(thread_id: str) -> DayResume | None:
    """单日生成的断点续跑入口（generation_recovery 消费）；无可续检查点返回 None。"""
    result = resume_run(thread_id)
    if result is None:
        return None
    day_request = result.get("day_request")
    context = day_request.context if day_request is not None else None
    return DayResume(plan=result.get("plan"), context=context)
