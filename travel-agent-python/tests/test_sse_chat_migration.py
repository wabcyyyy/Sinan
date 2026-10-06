"""M7-a 迁移验证：SSE 进程内化 + 对话改行程（阻塞与流式两条路径）。

Java 侧这套东西分在三处：`ItinerarySseGateway`（Redis 订阅 → emitter 广播）、
`ItinerarySseController`（chat-edit 的流式变体，reply 按 40 字符切片）、
`ItineraryChatService`（上下文构建与落库收尾）。迁到同进程后前两者的中间环节消失，
最容易丢的是这些细节：

1. 信封五键与 seq/ts 口径（心跳/超限帧固定 seq=0、ts 带 +08:00）；
2. 连接数上限**不抛 HTTP 错**，只给被拒的那条连接发 error 后收尾；
3. Redis 不可用时本地订阅者**仍要收到事件**（seq 退进程内自增）——这条是真踩过的：
   熔断打开时 `_get_client()` 抛的 ConnectionError 逃出了兜底分支；
4. 流式与阻塞共用 `build_chat_turn_context` + `finalize_chat_turn`：发给 agent 的入参
   与落库的对话记忆必须逐字段一致，否则两条路径的历史与 pendingAction 语义会分叉。
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
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.api.deps import AuthUser as _AuthUser
from app.common import cache_store, event_hub, event_publisher
from app.common.config import settings
from app.common.envelope import ApiError, install_exception_handlers
from app.common.task_pool import TaskRejected
from app.db import session as db_session
from app.db.models import (
    Base,
    BudgetDetail,
    ItineraryChatMessage,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.schemas.business.itinerary import ChatEditBody
from app.schemas.trip import ChatTurnResponse, HotelOption
from app.services import itinerary_chat, state_and_sessions, user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"
PASSWORD = "example123"
OWNER = 1
TRIP = 10


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm7a.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    # Redis 指向不可达端口：进程内投递与 seq 兜底必须在"没有 Redis"时也成立
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(event_hub, "HEARTBEAT_SECONDS", 0.05)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    event_publisher.reset_event_publisher()
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()
    event_publisher.reset_event_publisher()


def _seed() -> None:
    hashed = user_service.hash_password(PASSWORD)
    with db_session.session_scope() as session:
        session.add_all(
            [
                SysUser(id=OWNER, username="alice", password=hashed, status=1, role="user"),
                SysUser(id=2, username="mallory", password=hashed, status=1, role="user"),
            ]
        )
        session.add(
            ItineraryMain(
                id=TRIP,
                user_id=OWNER,
                title="杭州2日游",
                city="杭州",
                days=2,
                persons=2,
                budget=Decimal("3000.00"),
                status=2,
                start_date=date(2026, 4, 20),
                end_date=date(2026, 4, 21),
                preferences="亲子,美食",
                hotel_tier="comfort",
            )
        )
        day = ItineraryDay(itinerary_id=TRIP, day_no=1, note="湖山线", generation_status="SUCCEEDED")
        session.add(day)
        session.flush()
        session.add_all(
            [
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=TRIP,
                    item_type="attraction",
                    poi_name="西湖",
                    cost=Decimal("45.00"),
                    start_time=time(9, 30),
                    sort_no=0,
                ),
                BudgetDetail(itinerary_id=TRIP, category="门票", amount=Decimal("45.00"), item_count=1),
                BudgetDetail(itinerary_id=TRIP, category="酒店", amount=Decimal("600.00"), item_count=2),
            ]
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    test = TestClient(app, follow_redirects=False)
    test.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    return test


def _drain(agen) -> list[str]:
    """在临时事件循环里收干一个异步信封生成器。"""

    async def scenario():
        return [frame async for frame in agen]

    return asyncio.run(scenario())


def _envelopes(frames: list[str]) -> list[dict]:
    return [json.loads(frame) for frame in frames]


def _stub_turn(monkeypatch, response: ChatTurnResponse) -> list[dict]:
    """把 agent 入口打桩并记录它收到的请求体（形状必须与阻塞/流式两条路径一致）。"""
    seen: list[dict] = []

    def fake_run(request, **kwargs):
        # PR-6 接线契约：服务层必须把确认流 thread 传进来（提案要落 checkpoint）
        thread = kwargs.get("confirmation_thread")
        assert isinstance(thread, str) and thread.startswith("chat-"), thread
        seen.append(request.model_dump(by_alias=True))
        return response

    monkeypatch.setattr(itinerary_chat, "run_chat_turn", fake_run)
    return seen


def _inline_pool(monkeypatch) -> None:
    """对话池改同步执行：测的是帧序与落库，不是线程调度。"""
    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", lambda task, *args: task(*args))


# ---------- 进程内事件总线 ----------


def test_local_subscribers_receive_events_without_redis() -> None:
    """Redis 不可用时本地流必须照收：seq 退进程内自增，广播失败不影响投递。"""

    async def scenario():
        subscription, rejected = event_hub.subscribe(TRIP, event_publisher.too_many_connections_envelope(TRIP))
        assert rejected is False
        event_publisher.publish_event(TRIP, "day_start", {"dayNo": 1})
        frame = await subscription.take(timeout=1)
        event_hub.unsubscribe(subscription)
        return frame

    frame = asyncio.run(scenario())
    assert frame is not None and frame is not event_hub.CLOSED
    envelope = json.loads(frame)
    assert (envelope["type"], envelope["itineraryId"], envelope["data"]) == ("day_start", TRIP, {"dayNo": 1})
    assert set(envelope) == {"type", "itineraryId", "seq", "ts", "data"}
    assert envelope["seq"] >= 1 and envelope["ts"].endswith("+08:00")
    assert event_hub.subscriber_count(TRIP) == 0


def test_publish_local_is_point_to_point() -> None:
    """点对点直发只给本行程连接：对话草稿广播出去会让其它标签页互相覆盖。"""

    async def scenario():
        mine, _ = event_hub.subscribe(TRIP, "{}")
        other, _ = event_hub.subscribe(99, "{}")
        event_publisher.publish_local(TRIP, "chat_token", {"messageId": "ab12cd34", "delta": "好"})
        mine_frame = await mine.take(timeout=1)
        other_frame = await other.take(timeout=0.1)
        event_hub.unsubscribe(mine)
        event_hub.unsubscribe(other)
        return mine_frame, other_frame

    mine_frame, other_frame = asyncio.run(scenario())
    assert json.loads(mine_frame)["type"] == "chat_token"
    assert other_frame is None  # 另一条流没收到


def test_connection_limit_rejects_with_an_error_frame_not_an_http_error() -> None:
    """同一行程第 6 条连接：先发可读的 error 再收尾（抛 HTTP 错会引发前端重连风暴）。"""

    async def scenario():
        kept = [event_hub.subscribe(TRIP, "{}")[0] for _ in range(event_hub.MAX_SUBSCRIBERS_PER_ITINERARY)]
        limit = event_publisher.too_many_connections_envelope(TRIP)
        rejected, was_rejected = event_hub.subscribe(TRIP, limit)
        frame = await rejected.take(timeout=1)
        # 队列排空且已关闭 → 立即 CLOSED，被拒的连接不该再等一个心跳周期
        tail = await rejected.take()
        for subscription in kept:
            event_hub.unsubscribe(subscription)
        return len(kept), was_rejected, frame, tail

    kept_count, was_rejected, frame, tail = asyncio.run(scenario())
    assert kept_count == 5 and was_rejected is True
    payload = json.loads(frame)
    assert payload["type"] == "error" and payload["seq"] == 0
    assert payload["data"] == {
        "code": "TOO_MANY_CONNECTIONS",
        "message": "该行程的实时连接数已达上限，请关闭多余页面后重试",
        "retryable": False,
    }
    assert tail is event_hub.CLOSED


def test_saturated_subscriber_is_dropped_rather_than_blocking_publisher() -> None:
    """慢消费者写满缓冲后即被摘除：生产端（生成线程）绝不能因为前端卡住而被拖慢。"""

    async def scenario():
        subscription, _ = event_hub.subscribe(TRIP, "{}")
        for index in range(event_hub.SINK_BUFFER + 20):
            event_publisher.publish_local(TRIP, "degraded", {"scope": f"x{index}"})
        await asyncio.sleep(0)  # 让排队的 call_soon_threadsafe 回调全部落地
        dropped = event_hub.broadcast(TRIP, event_publisher.heartbeat_envelope(TRIP))
        return subscription.open, dropped, event_hub.subscriber_count(TRIP)

    still_open, delivered, remaining = asyncio.run(scenario())
    assert still_open is False
    assert delivered == 0 and remaining == 0, "满了就该被广播循环摘掉，而不是堆积或抛错"


# ---------- /events 端点 ----------
# TestClient 会把响应整体缓冲完才返回，无限 SSE 流在它下面永远"回不来"；
# 因此这两个用例直接调用路由协程并消费 body_iterator，再 aclose 验证订阅被摘除。


def test_events_endpoint_streams_in_process_frames() -> None:
    async def scenario():
        response = await itinerary_routes.events(TRIP, user=_AuthUser(id=OWNER, username="alice", role="user"))
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        assert event_hub.subscriber_count(TRIP) == 1
        event_publisher.publish_event(TRIP, "complete", {"status": 2})
        frames = response.body_iterator.__aiter__()
        first = json.loads((await frames.__anext__()).removeprefix("data:").strip())
        # 终态帧后服务端收尾流（R3-F4）。旧预期"终态后下一个周期补心跳"就是缺陷本身：
        # 终态后连接的唯一产出是无限心跳，把 MAX_SUBSCRIBERS_PER_ITINERARY 的槽占到
        # 客户端自己断开——改为断言迭代器在终态帧后排空收尾。
        with pytest.raises(StopAsyncIteration):
            await frames.__anext__()
        await response.body_iterator.aclose()
        return first

    first = asyncio.run(scenario())
    assert (first["type"], first["itineraryId"], first["seq"] >= 1) == ("complete", TRIP, True)
    assert first["data"] == {"status": 2}
    assert event_hub.subscriber_count(TRIP) == 0, "终态收尾后必须摘除订阅"


def test_chat_edit_stream_endpoint_sends_event_frames(monkeypatch) -> None:
    _inline_pool(monkeypatch)
    _stub_turn(monkeypatch, ChatTurnResponse(reply="两天足够", changed=True, plans=[{"day_no": 1, "items": []}]))

    async def scenario():
        response = await itinerary_routes.chat_edit_stream(
            TRIP, ChatEditBody(message="再加一天", history=[]), user=_AuthUser(id=OWNER, username="alice", role="user")
        )
        kinds = []
        async for chunk in response.body_iterator:
            kinds.append(json.loads(chunk.removeprefix("data:").strip())["type"])
        return response, kinds

    response, kinds = asyncio.run(scenario())
    assert response.media_type == "text/event-stream"
    assert kinds[0] == "chat_token" and kinds[-2:] == ["chat_draft", "chat_done"]


def test_events_endpoint_checks_ownership_before_the_stream(client: TestClient) -> None:
    """非本人必须是 HTTP 404，而不是流建立后再发 error（与 Java findOwnedMain 同序）。"""
    with db_session.session_scope() as session:
        session.add(ItineraryMain(id=77, user_id=2, title="别人的行程", city="北京", days=1, persons=1, status=2))
    missing = client.get("/api/itinerary/77/events")
    assert missing.status_code == 404 and missing.json()["message"] == "行程不存在"
    assert client.get("/api/itinerary/999999/events").status_code == 404


def test_events_endpoint_requires_login() -> None:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(itinerary_router)
    anon = TestClient(app, follow_redirects=False)
    assert anon.get(f"/api/itinerary/{TRIP}/events").status_code == 401


# ---------- 对话改行程：阻塞版 ----------


def test_chat_edit_builds_the_same_agent_body_the_java_gateway_sent(monkeypatch) -> None:
    turn = ChatTurnResponse(reply="已把西湖挪到下午", plans=[{"day_no": 1, "items": []}], changed=True)
    seen = _stub_turn(monkeypatch, turn)
    out = itinerary_chat.chat_edit(OWNER, TRIP, "把西湖改到下午", [{"role": "user", "content": "旧"}])

    assert len(seen) == 1
    body = seen[0]
    assert body["city"] == "杭州" and body["persons"] == 2 and body["budget"] == 3000.0
    assert body["currentTotal"] == 645.0 and body["currentHotelTotal"] == 600.0
    assert body["startDate"] == "2026-04-20" and body["endDate"] == "2026-04-21"
    assert body["preferences"] == ["亲子", "美食"] and body["hotelTier"] == "comfort"
    assert body["message"] == "把西湖改到下午"
    assert body["days"] == len(body["plans"]) == 1
    # 同进程直调必须复刻 HTTP JSON 的类型收敛：cost 是 float 而不是 Decimal，
    # 否则 agent 里的算术会 TypeError（Java 侧从来不会）
    assert body["plans"][0]["items"][0]["cost"] == 45.0
    assert isinstance(body["plans"][0]["items"][0]["cost"], float)
    assert body["history"] == [{"role": "user", "content": "旧"}], "库里无记忆时才用客户端 history"
    assert out["reply"] == "已把西湖挪到下午" and out["changed"] is True
    assert out["plans"][0]["_baseRevision"] == out["baseRevision"]
    assert out["messageId"] and set(out) == {
        "reply",
        "changed",
        "plans",
        "hotelOptions",
        "baseRevision",
        "requiresConfirmation",
        "planDocument",
        "operations",
        "pendingAction",
        "messageId",
    }


def test_chat_edit_persists_both_rows_and_invalidates_older_drafts(monkeypatch) -> None:
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=TRIP,
                user_id=OWNER,
                role="ai",
                content="上一版建议",
                plans_json='[{"day_no": 1, "items": [], "_baseRevision": "deadbeef"}]',
                hotel_options_json="[]",
                changed=1,
            )
        )
        session.flush()
        older_id = session.execute(select(ItineraryChatMessage).limit(1)).scalar_one().id

    _stub_turn(
        monkeypatch,
        ChatTurnResponse(
            reply="好",
            plans=[{"day_no": 1, "items": []}],
            changed=True,
            hotel_options=[
                HotelOption(
                    id="h1",
                    hotel_name="西子宾馆",
                    tier="comfort",
                    base_price=600.0,
                    season_factor=1.0,
                    season_label="平季",
                    nightly_price=600.0,
                    nights=1,
                    rooms=1,
                    total_price=600.0,
                    within_budget=True,
                    reason="近湖且含早",
                )
            ],
        ),
    )
    itinerary_chat.chat_edit(OWNER, TRIP, "换个酒店", [])

    with db_session.session_scope() as session:
        rows = session.execute(select(ItineraryChatMessage).order_by(ItineraryChatMessage.id)).scalars().all()
        assert [row.role for row in rows] == ["ai", "user", "ai"]
        older = next(row for row in rows if row.id == older_id)
        # changed=True 或带酒店提案 → 旧草稿一律消费掉（同 Java 的失效时机）
        assert (older.plans_json, older.hotel_options_json, older.changed) == ("[]", "[]", 0)
        newest = rows[-1]
        assert json.loads(newest.plans_json)[0]["_baseRevision"]
        assert json.loads(newest.hotel_options_json)[0]["baseRevision"]


def test_chat_edit_keeps_older_draft_when_nothing_changed(monkeypatch) -> None:
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=TRIP,
                user_id=OWNER,
                role="ai",
                content="待应用",
                plans_json='[{"day_no": 1, "items": [], "_baseRevision": "x"}]',
                hotel_options_json="[]",
                changed=1,
            )
        )
    _stub_turn(monkeypatch, ChatTurnResponse(reply="只是问一句", changed=False))
    itinerary_chat.chat_edit(OWNER, TRIP, "西湖几点开", [])
    with db_session.session_scope() as session:
        kept = session.execute(
            select(ItineraryChatMessage).where(ItineraryChatMessage.content == "待应用")
        ).scalar_one()
        assert kept.plans_json != "[]", "没改动也没酒店提案时不该把用户的待应用草稿清掉"


def test_chat_edit_prefers_the_pending_draft_over_database_days(monkeypatch) -> None:
    """有未应用的天数草稿时，后续对话继续基于草稿天数，而不是数据库旧值。"""
    pending = [{"day_no": 1, "items": []}, {"day_no": 2, "items": []}]
    revision = itinerary_chat.plan_revision(itinerary_chat.current_plans(TRIP))
    pending_with_rev = [dict(plan, _baseRevision=revision) for plan in pending]
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=TRIP,
                user_id=OWNER,
                role="ai",
                content="改成两天",
                plans_json=json.dumps(pending_with_rev),
                hotel_options_json="[]",
                changed=1,
            )
        )
    seen = _stub_turn(monkeypatch, ChatTurnResponse(reply="好", changed=False))
    itinerary_chat.chat_edit(OWNER, TRIP, "第二天去灵隐", [])
    assert seen[0]["days"] == 2, "DB 里只有 1 天，草稿是 2 天"


def test_chat_edit_maps_agent_failure_to_the_gateway_502(monkeypatch) -> None:
    def boom(request):
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(itinerary_chat, "run_chat_turn", boom)
    with pytest.raises(ApiError) as exc:
        itinerary_chat.chat_edit(OWNER, TRIP, "随便改改", [])
    assert (exc.value.status, exc.value.message) == (502, "行程助手暂不可用")
    with db_session.session_scope() as session:
        assert session.execute(select(ItineraryChatMessage)).scalars().all() == [], (
            "agent 失败时两条记忆都不该落库（Java 同：先调用后落库）"
        )


def test_chat_edit_rejects_foreign_itinerary_before_calling_agent(monkeypatch) -> None:
    seen = _stub_turn(monkeypatch, ChatTurnResponse(reply="不该被调用"))
    with pytest.raises(ApiError) as exc:
        itinerary_chat.chat_edit(2, TRIP, "改一下", [])
    assert (exc.value.status, exc.value.message) == (404, "行程不存在")
    assert seen == []


def test_chat_edit_endpoint_wraps_the_result_in_the_result_envelope(client: TestClient, monkeypatch) -> None:
    _stub_turn(monkeypatch, ChatTurnResponse(reply="好", changed=False))
    body = client.post(f"/api/itinerary/{TRIP}/chat-edit", json={"message": "西湖几点开", "history": []}).json()
    assert body["code"] == 200 and body["data"]["reply"] == "好"
    # 空消息保持 Java 的宽松口径：这层不拦，交给 agent 校验后映射成 502
    empty = client.post(f"/api/itinerary/{TRIP}/chat-edit", json={"message": ""})
    assert empty.status_code == 502 and empty.json()["message"] == "行程助手暂不可用"


# ---------- 对话改行程：流式版 ----------


def test_stream_frames_match_the_blocking_response_and_rejoin_into_reply(monkeypatch) -> None:
    _inline_pool(monkeypatch)
    reply = "字" * 85
    seen = _stub_turn(
        monkeypatch,
        ChatTurnResponse(reply=reply, plans=[{"day_no": 1, "items": []}], changed=True, operations=[{"kind": "move"}]),
    )
    frames = _envelopes(_drain(itinerary_chat.chat_edit_stream(OWNER, TRIP, "把西湖改到下午", [])))

    assert [frame["type"] for frame in frames[:3]] == ["chat_token"] * 3
    assert frames[-2]["type"] == "chat_draft" and frames[-1]["type"] == "chat_done"
    assert "".join(frame["data"]["delta"] for frame in frames[:-2]) == reply
    assert [len(frame["data"]["delta"]) for frame in frames[:-2]] == [40, 40, 5]
    stream_id = frames[0]["data"]["messageId"]
    assert len(stream_id) == 8
    assert all(frame["data"]["messageId"] == stream_id for frame in frames[:-2])
    assert frames[-1]["data"]["messageId"] == stream_id
    draft = frames[-2]["data"]
    assert "reply" not in draft
    assert set(draft) == {
        "changed",
        "plans",
        "hotelOptions",
        "baseRevision",
        "requiresConfirmation",
        "planDocument",
        "operations",
        "pendingAction",
        "messageId",
    }
    assert draft["operations"] == [{"kind": "move"}]
    # 与阻塞版同一条上下文构造：agent 收到的入参形状一致
    assert seen[0]["message"] == "把西湖改到下午" and seen[0]["plans"]
    assert all(
        set(frame) == {"type", "itineraryId", "seq", "ts", "data"} and frame["ts"].endswith("+08:00")
        for frame in frames
    )
    with db_session.session_scope() as session:
        roles = [
            row.role
            for row in session.execute(select(ItineraryChatMessage).order_by(ItineraryChatMessage.id)).scalars().all()
        ]
        assert roles == ["user", "ai"], "流式路径同样要落库，否则历史与 pendingAction 失效"


def test_stream_heartbeats_while_the_model_is_running(monkeypatch) -> None:
    """模型没跑完之前连接不能静默：空闲周期要补心跳帧（Java 那段窗口是完全静默的）。"""
    _stub_turn(monkeypatch, ChatTurnResponse(reply="好", changed=False))
    submitted: list = []
    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", lambda task, *args: submitted.append(args))
    frames = _envelopes(_drain_first(itinerary_chat.chat_edit_stream(OWNER, TRIP, "改一下", []), 2))
    assert [frame["type"] for frame in frames] == ["heartbeat", "heartbeat"]
    assert len(submitted) == 1, "任务已提交，只是还没跑完"


def _drain_first(agen, count: int) -> list[str]:
    async def scenario():
        frames = []
        async for frame in agen:
            frames.append(frame)
            if len(frames) >= count:
                break
        return frames

    return asyncio.run(scenario())


def test_stream_error_frames_for_busy_and_failed_turns(monkeypatch) -> None:
    _stub_turn(monkeypatch, ChatTurnResponse(reply="好", changed=False))

    def reject(task, *args):
        raise TaskRejected("chat pool saturated")

    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", reject)
    busy = _envelopes(_drain(itinerary_chat.chat_edit_stream(OWNER, TRIP, "改一下", [])))
    assert len(busy) == 1 and busy[0]["type"] == "error"
    # 与生成任务的 429 同语义，但 SSE 下只能变成事件（HTTP 已经是 200）
    assert busy[0]["data"] == {"code": "AGENT_BUSY", "message": "行程助手繁忙，请稍后重试", "retryable": True}

    def boom(request):
        raise RuntimeError("模型炸了")

    monkeypatch.setattr(itinerary_chat, "run_chat_turn", boom)
    _inline_pool(monkeypatch)
    failed = _envelopes(_drain(itinerary_chat.chat_edit_stream(OWNER, TRIP, "改一下", [])))
    assert len(failed) == 1 and failed[0]["data"]["code"] == "AGENT_ERROR"
    assert failed[0]["data"]["message"] == "行程助手暂不可用"


def test_stream_context_failure_is_reported_as_an_event(monkeypatch) -> None:
    def reject(task, *args):
        raise AssertionError("上下文都没建成，不该占用工作线程")

    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", reject)
    frames = _envelopes(_drain(itinerary_chat.chat_edit_stream(999, TRIP, "改一下", [])))
    assert len(frames) == 1 and frames[0]["type"] == "error"
    assert frames[0]["data"]["code"] == "AGENT_ERROR" and "行程不存在" in frames[0]["data"]["message"]


def test_chunk_reply_parity_with_java() -> None:
    assert itinerary_chat.chunk_reply("") == []
    assert itinerary_chat.chunk_reply("abc") == ["abc"]
    assert itinerary_chat.chunk_reply("x" * 41) == ["x" * 40, "x"]
    assert itinerary_chat.chunk_reply("x" * 8, size=0) == []


def test_history_window_applies_when_the_db_is_empty(monkeypatch) -> None:
    long_history = [{"role": "user", "content": f"m{i}"} for i in range(30)]
    seen = _stub_turn(monkeypatch, ChatTurnResponse(reply="好", changed=False))
    itinerary_chat.chat_edit(OWNER, TRIP, "继续", long_history)
    assert len(seen[0]["history"]) == 20 and seen[0]["history"][-1]["content"] == "m29"


def test_persisted_memory_replaces_client_history(monkeypatch) -> None:
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=TRIP,
                user_id=OWNER,
                role="ai",
                content="库里的记忆",
                plans_json="[]",
                hotel_options_json="[]",
                changed=0,
            )
        )
    seen = _stub_turn(monkeypatch, ChatTurnResponse(reply="好", changed=False))
    itinerary_chat.chat_edit(OWNER, TRIP, "继续", [{"role": "user", "content": f"m{i}"} for i in range(30)])
    # 一旦库里有记忆就以它为准，并压成 {role, content} 两键（客户端传的整段被忽略）
    assert seen[0]["history"] == [{"role": "ai", "content": "库里的记忆"}]
