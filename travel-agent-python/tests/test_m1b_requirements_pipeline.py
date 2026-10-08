"""M1b 验收（spec §6）：研究、生成、终检、编辑上下文贯通结构化需求。

业务服务入口注入桩抓取研究与生成参数，再检查最终库内需求——不只断言
prompt 包含一句文本。杭州场景：persons=3、budget=4500、hotel_tier=舒适型、
必去灵隐寺（第 2 天）、排除博物馆与宋城、第 1 天 14:00 后到达 20:00 前结束、
慢游、预算硬上限（不含大交通）、2 间房。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryMain, SysUser
from app.schemas.trip import DailyPlan, GenerateRequest, TripItem
from app.schemas.trip_requirements import TripRequirements
from app.services import (
    generation_gate,
    itinerary_chat,
    itinerary_generation,
    state_and_sessions,
    user_service,
)

PASSWORD = "example123"
CITY = "杭州"

STRUCT: dict[str, Any] = {
    "schemaVersion": 1,
    "dayWindows": [{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "20:00"}],
    "requiredPlaces": [{"constraintId": "place-1", "name": "灵隐寺", "dayNo": 2}],
    "excludedPlaces": ["宋城"],
    "excludedCategories": ["museum"],
    "pace": "relaxed",
    "transportPreference": "mixed",
    "budgetPolicy": {"mode": "hard_cap", "includeIntercityTransport": False},
    "lodging": {"rooms": 2, "stayNightsExplicit": True},
}

GENERATE_BODY: dict[str, Any] = {
    "city": CITY,
    "days": 2,
    "persons": 3,
    "budget": 4500,
    "hotelTier": "舒适型",
    "intent": "带爸妈慢游杭州",
    "requirementsStruct": STRUCT,
}


def _expected_struct() -> TripRequirements:
    return TripRequirements.model_validate(STRUCT)


def _daily_plan(day_no: int, name: str) -> DailyPlan:
    """逐日兜底链的替身产出：与 test_checkpoint_resume 同形状。"""
    return DailyPlan(
        day_no=day_no,
        note=f"第 {day_no} 天",
        theme="湖山线",
        items=[
            TripItem(poi_name=name, item_type="attraction", cost=45, start_time="09:00", end_time="11:30"),
            TripItem(poi_name=f"知味观{day_no}", item_type="food", cost=40, start_time="11:30", end_time="13:00"),
        ],
    )


def _stub_orchestration(monkeypatch, captured: dict) -> None:
    """桩研究/逐日/整段三个 agent 入口，抓取贯通参数（不执行真实 LLM/研究）。"""

    def fake_context(request, *, itinerary_id=None, **kwargs):
        captured["research_request"] = request
        # M1b：plan_context 投影保留 research_report（降级证据不丢）
        return {"candidates": [], "foods": [], "hotels": [], "consumption": None, "research_report": {}}

    def fake_day(request):
        captured["day_requests"].append(request)
        return _daily_plan(request.day_no, f"点{request.day_no}")

    def fake_stream(request, cancel=None):
        captured["stream_request"] = request
        # 空流：整段不产出 → 走逐日兜底（本文件关注参数贯通，不关注流式装配）
        yield from ()

    monkeypatch.setattr(itinerary_generation, "run_plan_context", fake_context)
    monkeypatch.setattr(itinerary_generation, "run_generate_day", fake_day)
    monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", fake_stream)
    monkeypatch.setattr(itinerary_generation, "_submit_budget_recalculate", lambda itinerary_id: None)
    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", lambda task, *args: None)
    # 池内联执行：generate 请求返回前 plan_days 已同步跑完（本文件要抓贯通参数）
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm1b.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", "example-only-hs256-test-signing-material")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    itinerary_generation.reset_active_planning_for_tests()
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


@pytest.fixture
def client(env) -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    test = TestClient(app, follow_redirects=False)
    test.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    return test


def _submit_trip(client: TestClient, captured: dict, monkeypatch) -> int:
    _stub_orchestration(monkeypatch, captured)
    detail = client.post("/api/itinerary/generate", json=GENERATE_BODY).json()["data"]
    trip_id = detail["id"]
    # 提交被桩成不执行 → 清注册表让 plan_days 线程跑完（同 test_checkpoint_resume 口径）
    itinerary_generation.reset_active_planning_for_tests()
    return trip_id


def test_research_and_generation_carry_requirements(client, monkeypatch) -> None:
    """研究请求与逐日生成请求都带完整需求：persons=3/budget=4500/舒适型/结构化需求逐字段一致。"""
    captured: dict = {"day_requests": []}
    _submit_trip(client, captured, monkeypatch)

    research: GenerateRequest | None = captured.get("research_request")
    assert research is not None, "研究层必须被调用（业务入口桩抓取）"
    assert (research.persons, research.budget, research.hotel_tier) == (3, 4500.0, "舒适型"), (
        "研究层此前永远看见 persons=1（plan_context 内部重建），M1b 必须是真实人数/预算/档次"
    )
    assert research.requirements_struct == _expected_struct(), "研究层收到与提交逐字段一致的结构化需求"
    assert research.intent == "带爸妈慢游杭州"

    day_requests = captured["day_requests"]
    assert len(day_requests) == 2, "两天都走逐日兜底（空整段流）"
    for request in day_requests:
        assert (request.persons, request.budget, request.hotel_tier) == (3, 4500.0, "舒适型")
        assert request.requirements_struct == _expected_struct(), "逐日带整趟需求（同引用语义，不改写）"
    assert day_requests[1].day_no == 2


def test_whole_trip_stream_request_carries_requirements(client, monkeypatch) -> None:
    """整段流式请求同样携带结构化需求（stream_branch 反向重建不再丢字段）。"""
    captured: dict = {"day_requests": []}
    _submit_trip(client, captured, monkeypatch)
    stream_request = captured.get("stream_request")
    assert stream_request is not None, "fresh_trip 应先走整段流式"
    assert stream_request.requirements_struct == _expected_struct()
    assert (stream_request.persons, stream_request.days) == (3, 2)


def test_shell_persists_and_chat_context_reads_requirements(client, monkeypatch) -> None:
    """建壳落库 requirements_json；编辑上下文从主表读回同一需求（已确认需求优先）。"""
    captured: dict = {"day_requests": []}
    trip_id = _submit_trip(client, captured, monkeypatch)

    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None and main.requirements_json is not None, "新行程建壳一律写需求快照"
        assert TripRequirements.model_validate(main.requirements_json) == _expected_struct()
        days = (
            session.query(ItineraryDay).filter(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no).all()
        )
        assert all(day.generation_status == "SUCCEEDED" for day in days), "桩链路两天都成功落库"

    ctx = itinerary_chat.build_chat_turn_context(1, trip_id, "帮我改一下第二天", None)
    assert ctx.chat_body["requirements_struct"] == _expected_struct(), (
        "编辑上下文读主表需求快照（已确认需求优先于聊天历史再猜测）"
    )
    assert ctx.chat_body["persons"] == 3 and ctx.chat_body["budget"] == 4500.0


def test_stale_research_context_discarded_on_requirement_change(client, monkeypatch) -> None:
    """恢复复用研究上下文前校验需求指纹（spec §6.5）：变更后不相干的 context 不复用。

    plan_days 是所有 context 进入生成链的唯一收口（恢复腿 submit_planning 的
    context 也从这里过），直接对它断言：指纹不匹配 → 弃用并重新研究；匹配 → 复用。
    """
    captured: dict = {"day_requests": []}
    trip_id = _submit_trip(client, captured, monkeypatch)

    fresh_research: list[GenerateRequest] = []

    def counting_context(request, *, itinerary_id=None, **kwargs):
        fresh_research.append(request)
        return {"candidates": [], "foods": [], "hotels": [], "consumption": None, "research_report": {}}

    def inline(task, *args):
        task(*args)

    monkeypatch.setattr(itinerary_generation, "run_plan_context", counting_context)
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", inline)

    command = itinerary_generation.GenerateCommand(city=CITY, days=2, persons=3, stay_nights=2)
    fingerprint = generation_gate.request_fingerprint(command)

    # ① 指纹不匹配的旧 context（需求已变）→ 弃用，重新研究
    stale_context = {"candidates": [{"name": "旧证据"}], "_requirements_fp": "not-the-current-fingerprint"}
    itinerary_generation.submit_planning(1, trip_id, command, context=stale_context)
    assert len(fresh_research) == 1, "指纹不匹配的 context 必须被弃用并重新研究"
    assert fresh_research[0].persons == 3

    # ② 指纹匹配的 context → 复用，不重研究（僵尸续跑的成本主体就此消失）
    fresh_research.clear()
    captured["day_requests"].clear()
    matched_context = {
        "candidates": [],
        "foods": [],
        "hotels": [],
        "consumption": None,
        "_requirements_fp": fingerprint,
    }
    itinerary_generation.submit_planning(1, trip_id, command, context=matched_context)
    assert fresh_research == [], "指纹匹配的恢复 context 应被复用，不重研究"
