"""断点恢复集成测试（PR-3）：图跑到一半进程死亡 → 重启从 checkpoint 续跑到完成。

PLAN PR-3 验收口径：
1. 图跑到一半模拟进程死亡 → **重启**（全新 compile 的图接同一份检查点存储，进程死亡
   的等价物）从 checkpoint 续跑到完成，且**已完成的超步一个都不重跑**（节点调用
   计数为证——续跑 ≠ 整段重来）；
2. thread_id = 生成任务标识（截断 ≤255）；无检查点时 resume 明确返回 None，不冒充续跑。

"进程死亡"模拟：节点里抛 `KeyboardInterrupt` 子类——它穿过图节点的 `except Exception`
（INV-4 的降级兜底只接 Exception），效果等价进程被杀：没落盘的超步作废，已落盘的还在。

恢复保真（审查 P0-3，R10-Q3"一道测试救最多"）：续跑测试的断言必须钉在**重建输出的
可观察不变量**上，而不是钉在重建机制被 mock 掉之后的下游行为上——后者与遗漏共谋。
`test_recovery_rebuilds_command_without_losing_user_params` 与
`test_stream_crash_recovery_preserves_quotes_labels_and_draft_status` 钉四不变量：
命令字段保真（含 origin_city/intent）/ 报价不被 NULL 覆盖 / 落库项标签完整 /
纯草案详情 destinationStatus=draft_only。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.agent.generation.orchestration import trip_graph, workflow
from app.agent.runtime import checkpoint
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.schemas.trip import DailyPlan, GenerateRequest, GenerateResponse, TripItem
from app.schemas.trip_requirements import TripRequirements
from app.services import (
    generation_gate,
    generation_recovery,
    itinerary_generation,
    state_and_sessions,
    user_service,
)

PASSWORD = "example123"
CITY = "杭州"


class _ProcessDeath(KeyboardInterrupt):
    """模拟进程死亡（BaseException：穿透节点的 except Exception 兜底）。"""


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'resume.db'}")
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


def _wire_plan(day_no: int, names: list[str]) -> dict:
    """流式 wire 形状的合规天（景点 + 餐饮、无重叠）：终检口径与 test_generation_migration 一致。"""
    return {
        "dayNo": day_no,
        "note": f"第 {day_no} 天",
        "theme": "湖山线",
        "tripTheme": None,
        "items": [
            {"itemType": "attraction", "poiName": name, "cost": 45, "startTime": "09:00", "endTime": "11:30"}
            for name in names
        ]
        + [
            {"itemType": "food", "poiName": f"知味观{day_no}", "cost": 40, "startTime": "11:30", "endTime": "13:00"},
        ],
    }


def _daily_plan(day_no: int, name: str) -> DailyPlan:
    """逐日兜底链的替身产出：与流式草案同形状（source=None 的纯生成项）。"""
    return DailyPlan(
        day_no=day_no,
        note=f"第 {day_no} 天",
        theme="湖山线",
        items=[
            TripItem(poi_name=name, item_type="attraction", cost=45, start_time="09:00", end_time="11:30"),
            TripItem(poi_name=f"知味观{day_no}", item_type="food", cost=40, start_time="11:30", end_time="13:00"),
        ],
    )


def _rows(trip_id: int) -> tuple[ItineraryMain, list[ItineraryDay]]:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None, "trip 不存在"
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        session.expunge(main)
        for day in days:
            session.expunge(day)
        return main, list(days)


def _items(trip_id: int) -> list[ItineraryItem]:
    with db_session.session_scope() as session:
        rows = (
            session.execute(
                select(ItineraryItem)
                .where(ItineraryItem.itinerary_id == trip_id)
                .order_by(ItineraryItem.day_id, ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return list(rows)


def _zombie(trip_id: int) -> None:
    """把活跃心跳拨老（天 + 主表），恢复任务才会接（同 test_generation_migration 口径）。

    显式列值 UPDATE：onupdate=func.now() 只在该列不在 SET 里时触发，显式赋值
    不会被"刚刚"覆盖（SQLite 下 func.now() 是 UTC，本地 +08 会看似未来）。
    """
    stale = datetime.now() - timedelta(minutes=30)
    with db_session.session_scope() as session:
        session.execute(update(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).values(updated_at=stale))
        session.execute(update(ItineraryMain).where(ItineraryMain.id == trip_id).values(updated_at=stale))


def test_trip_graph_resumes_from_checkpoint_after_process_death(monkeypatch) -> None:
    calls = {"parse": 0, "research": 0, "generate": 0, "reflect": 0, "format": 0}
    deaths = {"left": 1}

    def parse_requirements(state):
        calls["parse"] += 1
        return {"requirements": {"city": "杭州"}}

    def research_pois(state):
        calls["research"] += 1
        return {"candidates": [{"name": "西湖"}], "foods": [], "hotels": [], "consumption": {}, "research_report": {}}

    def generate_itinerary(state):
        calls["generate"] += 1
        return {"daily_plans": [{"day_no": 1, "items": []}]}

    def reflect(state):
        calls["reflect"] += 1
        if deaths["left"]:
            deaths["left"] -= 1
            raise _ProcessDeath()
        return {"validation_issues": [], "validation_log": []}

    def format_output(state):
        calls["format"] += 1
        return {"result": GenerateResponse(city="杭州", days=1, title="t", daily_plans=[], budget_estimate={})}

    monkeypatch.setattr(workflow, "parse_requirements", parse_requirements)
    monkeypatch.setattr(workflow, "research_pois", research_pois)
    monkeypatch.setattr(workflow, "generate_itinerary", generate_itinerary)
    monkeypatch.setattr(workflow, "reflect", reflect)
    monkeypatch.setattr(workflow, "needs_fix", lambda state: "pass")
    monkeypatch.setattr(workflow, "format_output", format_output)

    thread = f"test-resume-{uuid4().hex}"
    with pytest.raises(_ProcessDeath):
        trip_graph.run_trip(GenerateRequest(city="杭州", days=1), thread_id=thread)
    assert calls == {"parse": 1, "research": 1, "generate": 1, "reflect": 1, "format": 0}, (
        "死在 reflect：parse/research/generate 三个超步已落检查点"
    )

    # 重启 = 全新 compile 的图对象接同一份检查点存储（进程死亡的等价物）
    revived = trip_graph.build_unified_graph().compile(checkpointer=checkpoint.get_checkpointer())
    result = revived.invoke(None, checkpoint.run_config(thread))
    assert calls == {"parse": 1, "research": 1, "generate": 1, "reflect": 2, "format": 1}, (
        "续跑只补没落盘的部分：已完成超步一个都不重跑（整段重跑 = 计数翻倍）"
    )
    assert result["result"].city == "杭州", "续跑到完成，产出照常交付"


def test_resume_returns_none_without_checkpoint_and_thread_id_is_truncated() -> None:
    """无检查点不冒充续跑；thread_id 按 checkpoints 列宽截断 ≤255（官方列宽坑）。"""
    assert trip_graph.resume_run(f"missing-{uuid4().hex}") is None
    assert trip_graph.resume_day(f"missing-{uuid4().hex}") is None
    long_thread = checkpoint.checkpoint_thread_id("x" * 400)
    assert len(long_thread) == checkpoint.THREAD_ID_MAX == 255
    assert checkpoint.run_config("day-1-2") == {"configurable": {"thread_id": "day-1-2"}}


def _reusable_plan(note: str) -> DailyPlan:
    """线程复用测试的最小合法载荷：一次过 validate_plans。

    旧预期为什么是错的（P2 空天修复 2026-10-05）：本测试原用空 plan 当最小
    载荷——彼时空 plan 在 day_reflect 免检直通，一轮恰好一次调用。修复后空
    plan 是重采样 issue（空天必须进重试环，见 trip_graph.day_reflect），载荷
    改为能过校验的最小行程；测试意图（thread 状态全量重置）不变。
    """
    return DailyPlan(
        day_no=1,
        note=note,
        items=[
            TripItem(item_type="attraction", poi_name="景点", start_time="09:00", end_time="13:00", duration_min=240),
            TripItem(item_type="food", poi_name="餐厅", start_time="13:10", end_time="14:10", duration_min=60, cost=50),
        ],
    )


def test_thread_reuse_starts_fresh_run(monkeypatch) -> None:
    """同一 thread 再跑 = 新 run（day 状态全量重置）：重复恢复/重试不与旧历史串味。"""
    plans = [_reusable_plan("第一轮"), _reusable_plan("第二轮")]
    attempts = {"n": 0}

    def fake_once(req, *, force_fallback=False):
        plan = plans[attempts["n"]]
        attempts["n"] += 1
        return plan, "llm"

    from app.agent.generation.orchestration import day_workflow

    monkeypatch.setattr(day_workflow, "generate_day_once", fake_once)
    thread = f"test-reuse-{uuid4().hex}"
    request_kwargs = {"city": "杭州", "day_no": 1, "action_id": thread}
    first = trip_graph.run_day(trip_graph.GenerateDayRequest(**request_kwargs), thread_id=thread)
    second = trip_graph.run_day(trip_graph.GenerateDayRequest(**request_kwargs), thread_id=thread)
    assert first.note == "第一轮" and second.note == "第二轮", "重放同一 thread 不吃旧状态（attempts/plan 全量重置）"
    assert attempts["n"] == 2


def test_cleanup_old_threads_removes_expired_history(monkeypatch) -> None:
    """保留期清理（LangGraph 官方告警：checkpoints 无限增长）：过期 thread 整条删除。"""
    from app.agent.generation.orchestration import day_workflow

    monkeypatch.setattr(
        day_workflow,
        "generate_day_once",
        lambda req, *, force_fallback=False: (
            DailyPlan(day_no=1, note="待清理", items=[TripItem(poi_name="测试景点", item_type="attraction")]),
            "llm",
        ),
    )
    thread = f"test-cleanup-{uuid4().hex}"
    trip_graph.run_day(trip_graph.GenerateDayRequest(city="杭州", day_no=1, action_id=thread), thread_id=thread)
    assert trip_graph.resume_day(thread) is not None, "先确认这条 thread 有检查点"
    assert checkpoint.cleanup_old_threads(0) >= 1, "retention=0：全部过期"
    assert trip_graph.resume_day(thread) is None, "过期 thread 整条删掉，续跑入口回到 None"


# ---------- 恢复保真（P0-3：四不变量） ----------

_QUOTE_SENTINEL = [{"provider": "observed-test", "price": 1234, "airline": "测试航空"}]


def _stub_orchestration(monkeypatch, *, deaths: dict | None = None) -> dict:
    """打桩编排层三入口 + 池同步内联；`deaths` 非 None 时整段流式在第 2 天落地后死。"""
    calls: dict = {"context": [], "day": []}

    def fake_context(city, prefs, *, itinerary_id=None, start_date=None, days=None, origin_city=None, **kwargs):
        calls["context"].append({"origin_city": origin_city, "city": city})
        return {
            "candidates": [{"name": "西湖"}],
            "foods": [],
            "hotels": [],
            "consumption": {},
            "flight_quotes": _QUOTE_SENTINEL,
            "research_report": {},
        }

    def fake_day(request):
        calls["day"].append(request.day_no)
        return _daily_plan(request.day_no, f"点{request.day_no}")

    monkeypatch.setattr(itinerary_generation, "run_plan_context", fake_context)
    monkeypatch.setattr(itinerary_generation, "run_generate_day", fake_day)
    monkeypatch.setattr(itinerary_generation, "_submit_budget_recalculate", lambda itinerary_id: None)
    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", lambda task, *args: None)

    def inline(task, *args):
        task(*args)

    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", inline)

    if deaths is not None:
        real_persist = itinerary_generation._persist_stream_day

        def die_after_two_days(user_id, itinerary_id, command, context, fingerprint):
            for day_no in (1, 2):
                real_persist(
                    user_id,
                    itinerary_id,
                    command,
                    day_no,
                    _wire_plan(day_no, [f"流点{day_no}"]),
                    fingerprint,
                    overwrite=False,
                )
            deaths["died"] += 1
            raise _ProcessDeath("stream connection reset mid-trip")

        monkeypatch.setattr(itinerary_generation, "_plan_whole_trip", die_after_two_days)
    return calls


def test_recovery_rebuilds_command_without_losing_user_params(client, monkeypatch) -> None:
    """rebuild_request 字段等价（含 origin_city/intent/requirements）；recover_one 不碰报价。"""
    _stub_orchestration(monkeypatch)
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: None)
    detail = client.post(
        "/api/itinerary/generate",
        json={
            "city": CITY,
            "days": 2,
            "persons": 2,
            "stayNights": 1,
            "budget": 2000,
            "preferences": ["亲子"],
            "hotelTier": "comfort",
            "intent": "带娃慢游西湖",
            "requirements": "不赶路，午休",
            "originCity": "上海",
        },
    ).json()["data"]
    trip_id = detail["id"]
    # 提交被 patch 成不执行 → 行程还挂在注册表里；本用例模拟"提交它的进程已死"，
    # 清掉注册表让 recovery 可接（P1-1 存活探测的另一面）
    itinerary_generation.reset_active_planning_for_tests()
    # 模拟上一轮真实观测过报价：flight_quotes 非 NULL（save_flight_quotes 的空→NULL 语义
    # 不许把这份观测在恢复路径上无声抹掉）。直接赋 list——JSON 列由 SQLAlchemy 序列化。
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.flight_quotes = _QUOTE_SENTINEL
    _zombie(trip_id)

    _main, _days = _rows(trip_id)
    command = generation_recovery.rebuild_request(_main)
    assert (command.city, command.days, command.persons, command.stay_nights) == (CITY, 2, 2, 1)
    assert command.budget == 2000 and command.preferences == ["亲子"] and command.hotel_tier == "comfort"
    assert command.origin_city == "上海", "origin_city 是'一行修复躺在那没拿'——rebuild 必须读回"
    assert command.intent == "带娃慢游西湖" and command.requirements == "不赶路，午休", (
        "V10 落库的用户原话参数必须读回，否则续跑天静默丢意图"
    )
    # 指纹与首次生成等价：续跑才不会被幂等门 409 拒在门外。
    # budget 必须用 Decimal（pydantic 对 JSON 数字的实际产出形状）——
    # 2000（标度 0）与 DB 回读的 2000.00（标度 2）此前会算出不同指纹，
    # 带预算的行程一进恢复就被 verify_action 409 拒掉（request_fingerprint 已归一标度）。
    original = itinerary_generation.GenerateCommand(
        city=CITY,
        days=2,
        persons=2,
        stay_nights=1,
        budget=Decimal("2000"),
        preferences=["亲子"],
        hotel_tier="comfort",
        intent="带娃慢游西湖",
        requirements="不赶路，午休",
        origin_city="上海",
    )
    assert generation_gate.request_fingerprint(command) == generation_gate.request_fingerprint(original)

    submitted: list[itinerary_generation.GenerateCommand] = []
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *args, **kw: submitted.append(args[2]))
    assert generation_recovery.recover_one(trip_id, failed_resume=False) is False
    assert submitted and submitted[0].origin_city == "上海" and submitted[0].intent == "带娃慢游西湖"
    quotes = _rows(trip_id)[0].flight_quotes
    assert quotes is not None and _first_provider(quotes) == "observed-test", (
        "recover_one 全程不写报价列：上一轮观测价保持原样"
    )


def test_recovery_rebuilds_structured_requirements(client, monkeypatch) -> None:
    """M1a 验收：requirementsStruct 建壳随 V13 落库，rebuild 读回同一结构且与存量指纹一致。"""
    _stub_orchestration(monkeypatch)
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: None)
    struct = {
        "schemaVersion": 1,
        "dayWindows": [{"dayNo": 1, "kind": "arrival", "notBefore": "14:00", "finishBy": "20:00"}],
        "requiredPlaces": [{"constraintId": "place-1", "name": "灵隐寺", "dayNo": 2}],
        "excludedCategories": ["museum"],
        "excludedPlaces": ["宋城"],
        "pace": "relaxed",
        "transportPreference": "mixed",
        "maxWalkMinutesPerLeg": 20,
        "budgetPolicy": {"mode": "hard_cap", "includeIntercityTransport": False},
        "lodging": {"rooms": 2, "stayNightsExplicit": True},
    }
    detail = client.post(
        "/api/itinerary/generate",
        json={
            "city": CITY,
            "days": 2,
            "persons": 2,
            "stayNights": 1,
            "requirementsStruct": struct,
        },
    ).json()["data"]
    trip_id = detail["id"]
    itinerary_generation.reset_active_planning_for_tests()

    main, _days = _rows(trip_id)
    assert main.requirements_json is not None, "新行程建壳一律写需求快照（这是新旧指纹口径的判定信号）"
    command = generation_recovery.rebuild_request(main)
    assert command.legacy_fingerprint is False
    assert command.requirements_struct == TripRequirements.model_validate(struct), (
        "恢复读回的结构化需求与提交的逐字段一致（恢复不丢失）"
    )
    # 与建壳提交时（submit_planning 对同一 command 算的）指纹一致：恢复/重生成不会被 409
    original = itinerary_generation.GenerateCommand(
        city=CITY,
        days=2,
        persons=2,
        stay_nights=1,
        requirements_struct=TripRequirements.model_validate(struct),
    )
    assert generation_gate.request_fingerprint(command) == generation_gate.request_fingerprint(original)

    # 旧行程（requirements_json NULL，V13 前创建）按旧口径恢复：不反推约束、指纹走 v1
    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, trip_id)
        assert row is not None
        row.requirements_json = None
    legacy_main, _legacy_days = _rows(trip_id)
    legacy_command = generation_recovery.rebuild_request(legacy_main)
    assert legacy_command.legacy_fingerprint is True and legacy_command.requirements_struct is None
    # 同一输入去掉结构化需求后按 v1 口径重算，与 legacy 重算逐字节一致（口径切换不产生伪 409）
    plain = itinerary_generation.GenerateCommand(city=CITY, days=2, persons=2, stay_nights=1, legacy_fingerprint=True)
    assert generation_gate.request_fingerprint(legacy_command) == generation_gate.request_fingerprint(plain), (
        "旧行程按 v1 口径重算指纹必须与旧口径逐字节一致，恢复不被 409"
    )


def _first_provider(raw: object) -> str:
    """flight_quotes 列（str 或已反序列化 list，随方言）→ 首行 provider。"""
    value = json.loads(raw) if isinstance(raw, str) else raw
    assert isinstance(value, list) and value and isinstance(value[0], dict)
    return str(value[0].get("provider"))


def test_stream_crash_recovery_preserves_quotes_labels_and_draft_status(client, monkeypatch) -> None:
    """stream 中途崩溃 → recover() 端到端：命令保真 / 报价保留 / 标签完整 / draft_only。"""
    deaths = {"died": 0}
    calls = _stub_orchestration(monkeypatch, deaths=deaths)
    with pytest.raises(_ProcessDeath):
        client.post(
            "/api/itinerary/generate",
            json={
                "city": CITY,
                "days": 4,
                "intent": "四天深游",
                "originCity": "上海",
            },
        )
    assert deaths["died"] == 1, "进程死亡发生在第 2 天落地之后"
    with db_session.session_scope() as session:
        trip_id = session.execute(select(ItineraryMain.id)).scalar_one()

    _zombie(trip_id)
    generation_recovery.recover()  # 僵尸重拉分支按 Java 口径不改变更计数，断言钉在终态
    assert calls["context"], "恢复轮重跑了研究"

    # 不变量①：命令字段保真——恢复后的研究带着原 origin_city（rebuild 不再丢）
    assert [c["origin_city"] for c in calls["context"]] == ["上海", "上海"], "首次与恢复各研究一次，两次都带出发地"
    # 不变量②：报价保留——恢复轮 save_flight_quotes 写回的还是观测哨兵，不是 NULL
    _main, days = _rows(trip_id)
    assert _main.flight_quotes and _first_provider(_main.flight_quotes) == "observed-test"
    assert _main.gen_state == "COMPLETED" and _main.status == 2
    assert [day.generation_status for day in days] == ["SUCCEEDED"] * 4, "缺口天全部补齐，不丢天"
    # 不变量③：落库项标签完整——source 可空（纯草案），verification/value 标签必有默认值
    items = _items(trip_id)
    assert len(items) == 8
    assert all((item.verification_status or "").strip() for item in items), "verification_status 不许空"
    assert all((item.value_kind or "").strip() for item in items), "value_kind 不许空"
    assert [item.poi_name for item in items if item.poi_name == "流点1"], "已落库天不被恢复重写"
    # 不变量④：纯 LLM 草案（无权威来源行）→ 详情 destinationStatus=draft_only，不再虚标
    detail = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    assert detail["destinationStatus"] == "draft_only"


def test_checkpoint_whitelist_roundtrips_chat_reply(caplog) -> None:
    """反序列化白名单必须覆盖 UnifiedAgentState.chat_reply 的 ChatTurnResponse（R2-F4）。

    白名单缺项时 jsonplus 不炸而是**降级**：打 "Blocked deserialization" 告警并把
    对象解成裸 dict——confirm_graph 写入的 chat_reply 在恢复读出后类型静默变味。
    夜审在 backend-stderr.log 实拍到该告警。用生产白名单断言 roundtrip 保型且零告警。
    """
    import logging

    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from app.schemas.trip import ChatTurnResponse

    serde = JsonPlusSerializer(allowed_msgpack_modules=checkpoint._ALLOWED_MSGPACK_MODULES)
    payload = ChatTurnResponse(reply="好的，已把第二天换成博物馆路线", changed=True)
    with caplog.at_level(logging.WARNING, logger="langgraph.checkpoint.serde.jsonplus"):
        restored = serde.loads_typed(serde.dumps_typed(payload))
    assert isinstance(restored, ChatTurnResponse), "chat_reply 反序列化必须保型"
    assert restored.reply == payload.reply
    assert "Blocked deserialization" not in caplog.text, "白名单缺项会刷 Blocked 告警并静默降级成 dict"
