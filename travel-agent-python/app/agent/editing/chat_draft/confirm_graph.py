"""HITL 确认流（PR-6）：`interrupt()` 暂停 + `Command(resume=…)` 续跑，暂停态落 checkpoint。

酒店确认（hotel_proposal 的候选卡片）与草稿确认（plan_update/rewrite_plan 的
「图外草稿」）此前是 HTTP 绕行：提案返回前端后，服务端只剩 chat 消息的 pending_action
记账，确认请求凭 message id 回表缝合。本模块把「提案 → 等确认 → 按确认收尾」搬进
一张可暂停的小图：

```text
START ──► await_confirmation（interrupt(提案) 暂停，状态落 checkpoint）──► END
```

- 提案本体由 decide 的既有路径构建（LLM/规则工作不重跑），经 state 进图；
  `interrupt()` 落在纯读节点里——恢复时节点整体重执行是安全的（langgraph 语义）；
- `Command(resume=choice)` 续跑后在**服务端复核**选择 ∈ 提案（不再只信前端回显，
  与 services 层 message 级校验形成纵深），复核结论经 state 返回；
- 超时未确认：暂停态随检查点保留期清理过期（`runtime/checkpoint.cleanup_old_threads`
  挂 main.py cron，PR-3）；`resume_confirmation` 找不到待确认提案返回 None，
  调用方回退既有 message 级语义（存量兼容，不新增硬失败）。

thread 语义：`confirm_thread(itinerary_id)` 确定性命名（提案与确认两侧同源可找回，
截断 ≤255 由 run_config 保证）；同一行程的新提案覆盖旧暂停态（「当前待确认提案」）。

**可 mock 契约**：本模块无外呼；图与 resume 共用 `runtime/checkpoint` 进程级检查点。
"""

from __future__ import annotations

from typing import NamedTuple, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agent.research.agent_state import UnifiedAgentState
from app.agent.runtime import checkpoint
from app.agent.runtime.trace import traced
from app.schemas.trip import ChatTurnResponse

CONFIRM_THREAD_PREFIX = "chat"


class ConfirmResume(NamedTuple):
    """`Command(resume)` 续跑的判定结果：确认值与提案的服务端复核结论。"""

    confirmed: bool
    kind: str
    choice: dict
    reason: str | None


def confirm_thread(itinerary_id: int | str) -> str:
    """确认流 thread_id（确定性：提案/确认两侧同源可找回）。"""
    return f"{CONFIRM_THREAD_PREFIX}-{itinerary_id}"


def _proposal_kind(reply: ChatTurnResponse) -> str:
    pending = reply.pending_action if isinstance(reply.pending_action, dict) else {}
    if reply.hotel_options:
        return str(pending.get("type") or "replace_hotel")
    return "apply_plans"


def _validate_choice(reply: ChatTurnResponse, kind: str, choice: dict) -> ConfirmResume:
    """服务端复核：确认值必须落在当时提案内（前端回显只作输入，不作凭据）。"""
    action = str(choice.get("action") or "")
    if kind == "replace_hotel":
        if action != "replace_hotel":
            return ConfirmResume(False, kind, choice, "确认动作与提案类型不符")
        name = str(choice.get("hotel_name") or "")
        proposed = {str(o.hotel_name) for o in reply.hotel_options} | {str(o.id) for o in reply.hotel_options}
        if name not in proposed:
            return ConfirmResume(False, kind, choice, "所选酒店不在提案候选内")
        return ConfirmResume(True, kind, choice, None)
    if action in ("apply_plans", "reject_plans"):
        return ConfirmResume(True, kind, choice, None)
    return ConfirmResume(False, kind, choice, "确认动作与提案类型不符")


@traced("node", "chat.await_confirmation")
def await_confirmation(state: UnifiedAgentState) -> dict:
    """提案落 checkpoint 后 interrupt 暂停；resume 值与提案复核后落 chat_confirm。"""
    reply = cast("ChatTurnResponse", state.chat_reply)
    kind = _proposal_kind(reply)
    # interrupt：暂停并把提案落 checkpoint。本节点 interrupt 之前只读 state，
    # 恢复时的整节点重执行是安全的（提案构建不在这里，LLM/规则工作不重跑）。
    raw_choice = interrupt({"kind": kind, "pending_action": reply.pending_action})
    choice = dict(raw_choice) if isinstance(raw_choice, dict) else {"action": str(raw_choice or "")}
    verdict = _validate_choice(reply, kind, choice)
    return {
        "chat_confirm": {
            "confirmed": verdict.confirmed,
            "kind": verdict.kind,
            "choice": verdict.choice,
            "reason": verdict.reason,
        }
    }


def _build_confirm_graph() -> StateGraph:
    graph = StateGraph(UnifiedAgentState)
    graph.add_node("await_confirmation", await_confirmation)
    graph.add_edge(START, "await_confirmation")
    graph.add_edge("await_confirmation", END)
    return graph


confirm_graph = _build_confirm_graph().compile(checkpointer=checkpoint.get_checkpointer())


def run_confirmation(reply: ChatTurnResponse, *, thread: str) -> ChatTurnResponse:
    """提案进入确认流：interrupt 暂停（提案落 checkpoint）后原样返回提案回复。

    回复形状与直调一致（前端零改动）；同一 thread 的 `resume_confirmation` 续跑。
    """
    confirm_graph.invoke({"chat_reply": reply}, checkpoint.run_config(thread))
    return reply


def resume_confirmation(thread: str, choice: dict) -> ConfirmResume | None:
    """确认侧续跑（业务薄适配调用）：`Command(resume=choice)` → 服务端复核。

    无待确认提案（超时被保留期清理 / 从未提案）返回 None——调用方回退既有
    message 级语义（存量兼容）。重复确认对已完成 thread 幂等（复核结论不变）。
    """
    config = checkpoint.run_config(thread)
    if checkpoint.get_checkpointer().get(config) is None:
        return None
    result = confirm_graph.invoke(Command(resume=choice), config)
    raw = (result or {}).get("chat_confirm")
    if not isinstance(raw, dict):
        return None
    return ConfirmResume(
        confirmed=bool(raw.get("confirmed")),
        kind=str(raw.get("kind") or ""),
        choice=dict(raw.get("choice") or {}),
        reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
    )
