"""HITL 确认流单测（PR-6）：interrupt 暂停 → resume 续跑 → 服务端复核；超时未确认的保留期清理。

覆盖验收三件套：
1. interrupt → resume → 续跑（图可暂停/恢复，提案落 checkpoint）；
2. 超时未确认的保留期清理（暂停态随检查点保留期过期，resume 返回 None 回退）；
3. 服务端复核：确认值必须落在当时提案内（前端回显只作输入，不作凭据）。
"""

from __future__ import annotations

from uuid import uuid4

from app.agent.editing.chat_draft import confirm_graph as cg
from app.agent.editing.chat_draft import decide
from app.agent.runtime import checkpoint
from app.schemas.trip import ChatTurnRequest, ChatTurnResponse, HotelOption


def _hotel_option(name: str) -> HotelOption:
    return HotelOption(
        id=f"opt-{name}",
        hotel_name=name,
        tier="comfort",
        base_price=300,
        season_factor=1.0,
        season_label="平季",
        nightly_price=300,
        nights=2,
        rooms=1,
        total_price=600,
        within_budget=True,
        reason="近地铁",
    )


def _proposal() -> ChatTurnResponse:
    return ChatTurnResponse(
        reply="请选择酒店",
        hotel_options=[_hotel_option("柏悦"), _hotel_option("文华东方")],
        requires_confirmation=True,
        pending_action={"type": "replace_hotel", "requires_confirmation": True},
    )


def _thread() -> str:
    return cg.confirm_thread(f"test-{uuid4().hex[:8]}")


class TestConfirmRoundtrip:
    def test_interrupt_resume_roundtrip(self):
        thread = _thread()
        reply = cg.run_confirmation(_proposal(), thread=thread)
        # 提案原样返回（wire 形状不变，前端零改动），暂停态已落 checkpoint
        assert [o.hotel_name for o in reply.hotel_options] == ["柏悦", "文华东方"]
        assert checkpoint.get_checkpointer().get(checkpoint.run_config(thread)) is not None
        verdict = cg.resume_confirmation(thread, {"action": "replace_hotel", "hotel_name": "文华东方"})
        assert verdict is not None
        assert verdict.confirmed and verdict.kind == "replace_hotel" and verdict.reason is None

    def test_repeat_resume_after_completion_is_idempotent(self):
        """确认按钮双击 / 重试：对已完成 thread 的重复 resume 给出同一复核结论，不炸。"""
        thread = _thread()
        cg.run_confirmation(_proposal(), thread=thread)
        first = cg.resume_confirmation(thread, {"action": "replace_hotel", "hotel_name": "柏悦"})
        second = cg.resume_confirmation(thread, {"action": "replace_hotel", "hotel_name": "柏悦"})
        assert first is not None and second is not None
        assert first.confirmed and second.confirmed

    def test_resume_rejects_choice_outside_proposal(self):
        thread = _thread()
        cg.run_confirmation(_proposal(), thread=thread)
        verdict = cg.resume_confirmation(thread, {"action": "replace_hotel", "hotel_name": "不存在的酒店"})
        assert verdict is not None
        assert not verdict.confirmed and verdict.reason

    def test_resume_rejects_mismatched_action(self):
        thread = _thread()
        cg.run_confirmation(_proposal(), thread=thread)
        verdict = cg.resume_confirmation(thread, {"action": "apply_plans"})
        assert verdict is not None and not verdict.confirmed

    def test_plan_confirm_accepts_apply_and_reject(self):
        proposal = ChatTurnResponse(reply="草稿已生成", plans=[{"day_no": 1}], changed=True)
        thread_apply, thread_reject = _thread(), _thread()
        cg.run_confirmation(proposal, thread=thread_apply)
        cg.run_confirmation(proposal, thread=thread_reject)
        applied = cg.resume_confirmation(thread_apply, {"action": "apply_plans"})
        rejected = cg.resume_confirmation(thread_reject, {"action": "reject_plans"})
        assert applied is not None and applied.confirmed
        assert rejected is not None and rejected.confirmed


class TestPendingRetention:
    def test_resume_without_any_proposal_returns_none(self):
        verdict = cg.resume_confirmation(cg.confirm_thread(f"absent-{uuid4().hex[:8]}"), {"action": "apply_plans"})
        assert verdict is None

    def test_timed_out_confirmation_is_cleaned_by_retention(self):
        """超时未确认：暂停态随检查点保留期清理过期，此后 resume 返回 None（调用方回退）。"""
        thread = _thread()
        cg.run_confirmation(_proposal(), thread=thread)
        assert checkpoint.cleanup_old_threads(0) >= 1
        assert cg.resume_confirmation(thread, {"action": "replace_hotel", "hotel_name": "柏悦"}) is None


class TestChatTurnWiring:
    def test_confirm_requires_thread_and_is_skipped_without_one(self, monkeypatch):
        """无 thread 的直调保持旧行为（不进图）；带 thread 的确认型提案走 interrupt。"""
        proposal = _proposal()
        monkeypatch.setattr(decide, "_chat_turn_response", lambda req: proposal)
        request = ChatTurnRequest(city="杭州", days=2, message="换酒店")
        assert decide.run_chat_turn(request) is proposal  # 未挂 thread：原样返回，不进图

        thread = _thread()
        replied = decide.run_chat_turn(request, confirmation_thread=thread)
        assert [o.hotel_name for o in replied.hotel_options] == ["柏悦", "文华东方"]
        verdict = cg.resume_confirmation(thread, {"action": "replace_hotel", "hotel_name": "柏悦"})
        assert verdict is not None and verdict.confirmed

    def test_non_confirming_reply_skips_graph(self, monkeypatch):
        plain = ChatTurnResponse(reply="好的", plans=[], changed=False)
        monkeypatch.setattr(decide, "_chat_turn_response", lambda req: plain)
        assert (
            decide.run_chat_turn(ChatTurnRequest(city="杭州", days=2, message="hi"), confirmation_thread="chat-x")
            is plain
        )
