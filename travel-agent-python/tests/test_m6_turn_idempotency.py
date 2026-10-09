"""M6 turn 级幂等测试（spec §12 编辑入口）。

覆盖：
- claim 原语：同 turnId 并发（先占坑模拟 running）、done 重放、同 turnId 不同请求 409、
  失败后重试放行（error 短窗）；
- 旧路径钉住：无 X-Turn-Id 的请求两次同文都完整执行（模型两次、消息四条）——
  缺省行为零破坏；
- LLM mock 断言重放零调用、零落库；
- 流式路径：路由前置 prepare 409（建流前）、done 重放的帧形（不占池不落库）、
  正常回合完成后记录转 done（阻塞重试拿到重放）。

基建：临时 SQLite + 禁用 Redis（cache_store 走进程内降级，与 test_generate_idempotency
同口径）；agent 入口打桩计数。
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, time
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business import itinerary as itinerary_routes
from app.common import cache_store
from app.common.envelope import ApiError, install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryChatMessage, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.schemas.trip import ChatTurnResponse
from app.services import itinerary_chat, state_and_sessions

JWT_MATERIAL = "example-only-hs256-test-signing-material"
OWNER = 1
TRIP = 10
TURN = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm6.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(cache_store, "_get_client", _redis_unavailable)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _redis_unavailable():
    raise ConnectionError("Redis disabled for offline turn idempotency tests")


def _seed() -> None:
    with db_session.session_scope() as session:
        session.add(SysUser(id=OWNER, username="alice", password="x", status=1, role="user"))
        session.add(
            ItineraryMain(
                id=TRIP,
                user_id=OWNER,
                title="杭州2日游",
                city="杭州",
                days=1,
                persons=2,
                budget=Decimal("3000.00"),
                status=2,
                start_date=date(2026, 4, 20),
                end_date=date(2026, 4, 20),
            )
        )
        day = ItineraryDay(itinerary_id=TRIP, day_no=1, note="湖山线", generation_status="SUCCEEDED")
        session.add(day)
        session.flush()
        session.add(
            ItineraryItem(
                day_id=day.id,
                itinerary_id=TRIP,
                item_type="attraction",
                poi_name="西湖",
                start_time=time(9, 30),
                sort_no=0,
            )
        )


def _stub_turn(monkeypatch, calls: list[int], reply: str = "已调整") -> None:
    def fake_run(request, **kwargs):
        calls.append(1)
        return ChatTurnResponse(reply=reply, changed=False)

    monkeypatch.setattr(itinerary_chat, "run_chat_turn", fake_run)


def _messages() -> list[ItineraryChatMessage]:
    with db_session.session_scope() as session:
        rows = session.execute(select(ItineraryChatMessage).order_by(ItineraryChatMessage.id)).scalars().all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _turn_record(user_id: int, itinerary_id: int, turn_id: str) -> dict | None:
    return cache_store.get_json(itinerary_chat.TURN_IDEM_NAMESPACE, f"{user_id}:{itinerary_id}:{turn_id}")


# ---------- 阻塞路径：claim 语义 ----------


def test_running_record_returns_409_and_zero_llm_calls(monkeypatch) -> None:
    """同 turnId 并发：先占坑模拟「第一份正在处理」，第二份 409 且不进模型不落库。"""
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    # 先成功跑一轮拿到真实 request_hash（占坑必须同指纹才能命中 running 分支）
    itinerary_chat.chat_edit(OWNER, TRIP, "把节奏放慢", [], turn_id=TURN)
    assert len(calls) == 1
    done = _turn_record(OWNER, TRIP, TURN)
    assert done is not None and done["status"] == "done"
    # 手工回拨成 running，模拟并发窗口里的第一份仍在处理
    cache_store.set_json(
        itinerary_chat.TURN_IDEM_NAMESPACE,
        f"{OWNER}:{TRIP}:{TURN}",
        {"status": "running", "request_hash": done["request_hash"]},
        600,
    )
    with pytest.raises(ApiError) as exc:
        itinerary_chat.chat_edit(OWNER, TRIP, "把节奏放慢", [], turn_id=TURN)
    assert exc.value.status == 409 and "仍在处理中" in exc.value.message
    assert len(calls) == 1, "running 期间的重试绝不执行第二份模型调用"
    assert len(_messages()) == 2, "重试不新增消息"


def test_done_record_replays_without_llm_or_persistence(monkeypatch) -> None:
    """done 记录：重放固定空变更响应——零模型调用、零新增消息/草稿。"""
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    first = itinerary_chat.chat_edit(OWNER, TRIP, "换个酒店", [], turn_id=TURN)
    assert first["changed"] is False and first["messageId"] is not None
    second = itinerary_chat.chat_edit(OWNER, TRIP, "换个酒店", [], turn_id=TURN)
    assert len(calls) == 1, "重放不再调模型"
    assert second["reply"] == itinerary_chat.TURN_REPLAY_REPLY
    assert second["changed"] is False
    assert second["plans"] == [] and second["hotelOptions"] == []
    assert second["messageId"] is None, "重放不是一条新消息"
    assert len(_messages()) == 2, "重放不新增消息"


def test_same_turn_id_with_different_request_is_409(monkeypatch) -> None:
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    itinerary_chat.chat_edit(OWNER, TRIP, "第一句话", [], turn_id=TURN)
    with pytest.raises(ApiError) as exc:
        itinerary_chat.chat_edit(OWNER, TRIP, "第二句话", [], turn_id=TURN)
    assert exc.value.status == 409 and "不同请求" in exc.value.message
    assert len(calls) == 1


def test_failed_turn_releases_and_allows_retry(monkeypatch) -> None:
    """失败 ≠ 终态闸：error 记录释放后，同 turnId 重试可以真正执行。"""
    calls: list[int] = []

    def boom(request, **kwargs):
        calls.append(1)
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(itinerary_chat, "run_chat_turn", boom)
    with pytest.raises(ApiError) as exc:
        itinerary_chat.chat_edit(OWNER, TRIP, "改一下", [], turn_id=TURN)
    assert exc.value.status == 502
    record = _turn_record(OWNER, TRIP, TURN)
    assert record is not None and record["status"] == "error"

    _stub_turn(monkeypatch, calls)
    out = itinerary_chat.chat_edit(OWNER, TRIP, "改一下", [], turn_id=TURN)
    assert out["reply"] == "已调整" and len(calls) == 2
    settled = _turn_record(OWNER, TRIP, TURN)
    assert settled is not None and settled["status"] == "done"


def test_without_turn_id_keeps_legacy_behavior(monkeypatch) -> None:
    """无 X-Turn-Id：旧行为零破坏——两次同文请求各自完整执行并落库。"""
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    first = itinerary_chat.chat_edit(OWNER, TRIP, "把节奏放慢", [])
    second = itinerary_chat.chat_edit(OWNER, TRIP, "把节奏放慢", [])
    assert len(calls) == 2, "无 turnId 不做幂等"
    assert first["messageId"] != second["messageId"]
    assert len(_messages()) == 4
    assert _turn_record(OWNER, TRIP, TURN) is None


def test_request_hash_is_sensitive_to_message_plans_and_requirements() -> None:
    ctx = itinerary_chat.build_chat_turn_context(OWNER, TRIP, "改一下", [])
    base = itinerary_chat.turn_request_hash("改一下", ctx)
    assert base == itinerary_chat.turn_request_hash("改一下", ctx), "同输入同指纹"
    assert base != itinerary_chat.turn_request_hash("改成别的", ctx), "message 变 → 指纹变"
    assert len(base) == 64


def test_normalize_turn_id() -> None:
    assert itinerary_chat.normalize_turn_id(None) is None
    assert itinerary_chat.normalize_turn_id("   ") is None
    assert itinerary_chat.normalize_turn_id(f" {TURN} ") == TURN
    assert itinerary_chat.normalize_turn_id("x" * 500) == "x" * itinerary_chat.MAX_TURN_ID_LEN


# ---------- 流式路径 ----------


def _drain(agen) -> list[dict]:
    async def scenario():
        return [json.loads(frame.removeprefix("data:").strip()) async for frame in agen]

    return asyncio.run(scenario())


def _inline_pool(monkeypatch) -> None:
    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", lambda task, *args: task(*args))


def test_stream_route_preparation_409s_before_the_stream(monkeypatch) -> None:
    """stream 路由：turn 冲突在**建流之前**抛 409（HTTP 状态，不是流中间 error 事件）。"""
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    itinerary_chat.chat_edit(OWNER, TRIP, "第一句话", [], turn_id=TURN)
    with pytest.raises(ApiError) as exc:
        itinerary_chat.prepare_chat_turn(OWNER, TRIP, "第二句话", [], TURN)
    assert exc.value.status == 409


def test_stream_replay_emits_empty_change_frames_without_pool_or_persistence(monkeypatch) -> None:
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    ctx = itinerary_chat.build_chat_turn_context(OWNER, TRIP, "换个酒店", [])
    claim = itinerary_chat.TurnClaim(
        turn_id=TURN,
        request_hash="x",
        replay=itinerary_chat._replay_response(ctx),
    )
    submitted: list = []
    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", lambda task, *args: submitted.append(args))
    frames = _drain(itinerary_chat.chat_edit_stream(OWNER, TRIP, "换个酒店", [], prepared=(ctx, claim)))
    assert [f["type"] for f in frames] == ["chat_token", "chat_draft", "chat_done"]
    assert all(f["data"]["messageId"] == TURN[:8] for f in frames if f["type"] != "chat_draft")
    assert frames[1]["data"]["messageId"] is None, "重放不指向任何新落库消息"
    assert frames[1]["data"]["changed"] is False and frames[1]["data"]["plans"] == []
    assert submitted == [], "重放不占对话池"
    assert len(calls) == 0 and len(_messages()) == 0, "重放零模型调用、零落库"


def test_stream_turn_settles_record_done_and_blocking_retry_replays(monkeypatch) -> None:
    """流式正常回合：工作线程收尾回写 done；随后同 turnId 阻塞重试拿到重放。"""
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    _inline_pool(monkeypatch)
    ctx, claim = itinerary_chat.prepare_chat_turn(OWNER, TRIP, "改一下", [], TURN)
    assert claim is not None and claim.replay is None
    frames = _drain(itinerary_chat.chat_edit_stream(OWNER, TRIP, "改一下", [], prepared=(ctx, claim)))
    assert frames[-1]["type"] == "chat_done"
    settled = _turn_record(OWNER, TRIP, TURN)
    assert settled is not None and settled["status"] == "done"
    replay = itinerary_chat.chat_edit(OWNER, TRIP, "改一下", [], turn_id=TURN)
    assert len(calls) == 1, "同 turnId 的第二次请求不再进模型"
    assert replay["reply"] == itinerary_chat.TURN_REPLAY_REPLY


def test_stream_failure_settles_error_and_retry_executes(monkeypatch) -> None:
    """流式失败：error 记录落库（短窗），同 turnId 重试放行执行。"""
    calls: list[int] = []

    def boom(request, **kwargs):
        calls.append(1)
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(itinerary_chat, "run_chat_turn", boom)
    _inline_pool(monkeypatch)
    ctx, claim = itinerary_chat.prepare_chat_turn(OWNER, TRIP, "改一下", [], TURN)
    frames = _drain(itinerary_chat.chat_edit_stream(OWNER, TRIP, "改一下", [], prepared=(ctx, claim)))
    assert len(frames) == 1 and frames[0]["type"] == "error"
    failed = _turn_record(OWNER, TRIP, TURN)
    assert failed is not None and failed["status"] == "error"

    _stub_turn(monkeypatch, calls)
    out = itinerary_chat.chat_edit(OWNER, TRIP, "改一下", [], turn_id=TURN)
    assert out["reply"] == "已调整" and len(calls) == 2


# ---------- API 层：头透传 ----------


@pytest.fixture
def client(monkeypatch) -> TestClient:
    from app.api import deps
    from app.common import token_revocation

    monkeypatch.setattr(deps.settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": OWNER, "username": "alice", "role": "user", "status": 1},
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(itinerary_routes.router)
    return TestClient(app)


def _auth_header() -> dict[str, str]:
    from app.common.jwt_compat import encode_token

    return {"Authorization": f"Bearer {encode_token('alice', JWT_MATERIAL, 3600)}"}


def test_api_replays_done_turn_via_header(client: TestClient, monkeypatch) -> None:
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    auth = _auth_header()
    body = {"message": "换个酒店", "history": []}
    first = client.post(f"/api/itinerary/{TRIP}/chat-edit", json=body, headers={**auth, "X-Turn-Id": TURN})
    assert first.status_code == 200 and first.json()["data"]["messageId"] is not None
    second = client.post(f"/api/itinerary/{TRIP}/chat-edit", json=body, headers={**auth, "X-Turn-Id": TURN})
    assert second.status_code == 200
    assert second.json()["data"]["reply"] == itinerary_chat.TURN_REPLAY_REPLY
    assert len(calls) == 1, "HTTP 层同键重放零模型调用"


def test_api_running_turn_conflicts_with_409(client: TestClient, monkeypatch) -> None:
    calls: list[int] = []
    _stub_turn(monkeypatch, calls)
    auth = _auth_header()
    body = {"message": "改一下", "history": []}
    assert (
        client.post(f"/api/itinerary/{TRIP}/chat-edit", json=body, headers={**auth, "X-Turn-Id": TURN}).status_code
        == 200
    )
    record = _turn_record(OWNER, TRIP, TURN)
    assert record is not None
    cache_store.set_json(
        itinerary_chat.TURN_IDEM_NAMESPACE,
        f"{OWNER}:{TRIP}:{TURN}",
        {"status": "running", "request_hash": record["request_hash"]},
        600,
    )
    conflict = client.post(f"/api/itinerary/{TRIP}/chat-edit", json=body, headers={**auth, "X-Turn-Id": TURN})
    assert conflict.status_code == 409 and "仍在处理中" in conflict.json()["message"]
    # 缺省（无头）请求完全不受影响
    assert client.post(f"/api/itinerary/{TRIP}/chat-edit", json=body, headers=auth).status_code == 200
    assert len(calls) == 2, "无头请求走旧行为"
