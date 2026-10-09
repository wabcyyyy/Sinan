"""M5b 核心内容与备选富化分离（spec §11）的行为钉。

验收口径逐条落成用例：
1. 全天 SUCCEEDED 且终检过 → core_ready 业务事件发布一次，data.revision == DB
   planning_revision、daysEmitted 来自 DB（SUCCEEDED 天数）；发布那一刻
   gen_state 已是 CORE_READY（DB 权威投影，非只存在于一次 SSE 帧）；随后
   _finish 照常收 COMPLETED——complete 的收尾含义不变，CORE_READY 是过程态。
2. 有 PENDING/FAILED 天（终检不过/交付不齐）→ 不发 core_ready，走既有
   PARTIAL/FAILED 终态路径。
3. 整趟主输出缺 suggestions → 不报错：装配层如实返回空数组（既有行为钉住），
   备选池由研究候选池确定性构建（floor_suggestions 从空 raw 构建），open_trip
   输出 schema 不再含 suggestions 键、prompt 不再要求输出（版本已 bump）。
4. 富化三段写（A plan_note / B items.intro / C suggestions_json）绑定
   planning_revision：修订号移动 → 放弃写并记 WARNING；匹配 → 正常写。
5. core_ready 只发一次：GENERATING→CORE_READY 的条件更新即发布闸，非
   GENERATING 状态（已发/已终态）一律跳过。
6. stage_timing 阶段计时事件存在、elapsedMs 为非负数值（只记录不设阈值）。

全离线：agent 入口与富化的模型调用都在编排模块名字上打桩，线程池同步执行；
外部源由 tests/conftest.py 的 autouse 夹具钉死。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

import app.agent  # noqa: F401  先完成 agent 门面导入（存量导入环，同 test_llm_route.py 口径）
from app.agent.generation.content.day_prompts import open_day_output_schema, open_trip_output_schema
from app.agent.generation.content.narrative import assemble_trip_output
from app.agent.generation.content.suggestions import floor_suggestions
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.prompts.open_generation import open_trip_system_prompt
from app.schemas.trip import DailyPlan, TripItem
from app.services import (
    generation_events,
    generation_recovery,
    itinerary_enricher,
    itinerary_generation,
    state_and_sessions,
    user_service,
)

JWT_MATERIAL = "example-only-hs256-test-signing-material"
PASSWORD = "example123"
CITY = "杭州"
_REQUEST = SimpleNamespace(intent="亲子游", requirements="别太赶", region_hint="浙江")


# ---------------------------------------------------------------- 编排层夹具 ----


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm5b.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
    itinerary_generation.reset_active_planning_for_tests()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _make_client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    test = TestClient(app, follow_redirects=False)
    test.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    return test


@pytest.fixture
def client(env) -> TestClient:
    return _make_client()


@pytest.fixture
def captured_events(monkeypatch) -> list[tuple[int, str, dict]]:
    """业务事件捕获：generation_events 全部 emitter 都经 publish_event 出口。"""
    seen: list[tuple[int, str, dict]] = []
    monkeypatch.setattr(
        generation_events,
        "publish_event",
        lambda itinerary_id, event_type, data, run_id=None: seen.append((itinerary_id, event_type, data)),
    )
    return seen


def _plan(day_no: int, names: list[str], *, theme: str = "湖山线") -> DailyPlan:
    return DailyPlan(
        day_no=day_no,
        note=f"第 {day_no} 天",
        theme=theme,
        items=[
            TripItem(
                poi_name=name,
                item_type="hotel" if name.startswith("酒店") else "attraction",
                cost=300 if name.startswith("酒店") else 45,
                start_time="24:00" if name.startswith("酒店") else "09:30",
                end_time=None if name.startswith("酒店") else "13:30",
            )
            for name in names
        ],
        suggestions=[],
    )


def _wire_plan(day_no: int, names: list[str]) -> dict:
    """整段流式事件里的 plan 是 camel 化 wire dict，且必须过整段终检（≥1 景点 + 餐饮、
    有效活动 ≥240 分钟、无重叠）——形状与 test_generation_migration 同基线。"""
    return {
        "dayNo": day_no,
        "note": f"第 {day_no} 天",
        "theme": "湖山线",
        "tripTheme": None,
        "items": [
            {"itemType": "attraction", "poiName": name, "cost": 45, "startTime": "09:00", "endTime": "11:30"}
            for name in names
        ]
        + [{"itemType": "food", "poiName": f"知味观{day_no}", "cost": 40, "startTime": "11:30", "endTime": "13:00"}],
    }


def _fake_agents(monkeypatch, per_day: list[DailyPlan], stream_events: list[dict] | None = None) -> dict:
    """打桩三个 agent 入口；`stream_events=None` 表示整段流式直接抛错（走逐日兜底）。"""
    calls: dict = {"day": [], "stream": 0, "context": 0, "enrich": []}

    monkeypatch.setattr(
        itinerary_generation,
        "run_plan_context",
        lambda request, itinerary_id=None: (
            calls.__setitem__("context", calls["context"] + 1),
            {"candidates": [], "foods": [], "hotels": [], "consumption": None},
        )[1],
    )

    def fake_day(request):
        calls["day"].append(request)
        return per_day[min(request.day_no, len(per_day)) - 1]

    def fake_stream(request, cancel=None, **kwargs):
        calls["stream"] += 1
        if stream_events is None:
            raise RuntimeError("整段流式炸了")
        yield from stream_events

    monkeypatch.setattr(itinerary_generation, "run_generate_day", fake_day)
    monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", fake_stream)
    monkeypatch.setattr(itinerary_generation, "_submit_budget_recalculate", lambda itinerary_id: None)

    def fake_enrich(task, *args):
        calls["enrich"].append(args)

    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", fake_enrich)
    return calls


def _run_inline(monkeypatch) -> None:
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))


def _trip(client: TestClient, body: dict) -> dict:
    response = client.post("/api/itinerary/generate", json=body)
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def _get_main(trip_id: int) -> ItineraryMain:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        session.expunge(main)
        return main


def _events_of(captured: list[tuple[int, str, dict]], event_type: str) -> list[dict]:
    return [data for _tid, etype, data in captured if etype == event_type]


# ------------------------------------------------------- 1. core_ready 里程碑 ----


def test_core_ready_published_when_all_days_succeed(client, monkeypatch, captured_events) -> None:
    """全天 SUCCEEDED：core_ready 发布一次，revision/daysEmitted 来自 DB，
    发布时 gen_state 已是 CORE_READY；随后 _finish 照常收 COMPLETED。"""
    events = [
        {"type": "day", "plan": _wire_plan(1, ["西湖"])},
        {"type": "day", "plan": _wire_plan(2, ["灵隐寺"])},
        {
            "type": "done",
            "daysExpected": 2,
            "daysEmitted": [1, 2],
            "tripTheme": "西子湖畔慢行",
            "complete": True,
            "message": None,
        },
    ]
    calls = _fake_agents(monkeypatch, [_plan(1, ["不该被用到"]), _plan(2, ["同样不该"])], stream_events=events)
    _run_inline(monkeypatch)

    # 发布闸观察点：core_ready 事件出站那一刻读 DB——事件由条件更新成功后的同一
    # 事务提交触发，此刻状态投影必须是 CORE_READY（不是只存在于一次 SSE 帧）。
    # 注意 captured_events 夹具已先接管 publish_event，这里在其外再包一层观察者。
    states_at_publish: list[str] = []
    capture_stub = generation_events.publish_event

    def publishing_recorder(itinerary_id, event_type, data, run_id=None):
        if event_type == "core_ready":
            states_at_publish.append(str(_get_main(itinerary_id).gen_state))
        return capture_stub(itinerary_id, event_type, data, run_id)

    monkeypatch.setattr(generation_events, "publish_event", publishing_recorder)

    detail = _trip(client, {"city": CITY, "days": 2, "stayNights": 1})
    main = _get_main(detail["id"])

    assert calls["day"] == [], "整段流式已覆盖全部天，逐日循环不重跑"
    ready = _events_of(captured_events, "core_ready")
    assert len(ready) == 1, "core_ready 在一次生成任务里只发布一次"
    assert states_at_publish == ["CORE_READY"], "DB 权威状态投影与事件同源：发布时 gen_state 已迁移"
    assert ready[0]["revision"] == main.planning_revision, "revision 读自 DB planning_revision 现值"
    assert ready[0]["daysEmitted"] == 2, "daysEmitted 来自 DB SUCCEEDED 天数，不从事件流推导"
    assert main.gen_state == "COMPLETED" and main.status == 2, "complete 收尾含义不变，CORE_READY 是过程态"
    assert _events_of(captured_events, "complete")[0]["status"] == "COMPLETED"
    # 富化任务提交时捕获提交时的规划修订（M5b 绑定富化）
    assert calls["enrich"] and calls["enrich"][0][-1] == main.planning_revision


def test_pending_day_suppresses_core_ready(client, monkeypatch, captured_events) -> None:
    """日锁占用 → 第 2 天 PENDING（交付不齐）→ 无 core_ready，走既有 PARTIAL。"""
    _fake_agents(monkeypatch, [_plan(1, ["西湖"]), _plan(2, ["灵隐寺"])], stream_events=None)
    _run_inline(monkeypatch)
    monkeypatch.setattr(itinerary_generation.generation_gate, "try_day_lock", lambda itinerary_id, day_no: day_no != 2)
    detail = _trip(client, {"city": CITY, "days": 2})
    main = _get_main(detail["id"])
    assert _events_of(captured_events, "core_ready") == [], "有 PENDING 天不发 core_ready"
    assert main.gen_state == "PARTIAL", "交付不齐走既有部分交付终态"


def test_failed_day_suppresses_core_ready(client, monkeypatch, captured_events) -> None:
    """逐日生成失败 → FAILED，无 core_ready（失败路径既有语义不变）。"""

    def boom(request):
        raise ValueError("模型返回畸形 JSON")

    _fake_agents(monkeypatch, [_plan(1, ["西湖"])])
    _run_inline(monkeypatch)
    monkeypatch.setattr(itinerary_generation, "run_generate_day", boom)
    detail = _trip(client, {"city": CITY, "days": 2})
    main = _get_main(detail["id"])
    assert _events_of(captured_events, "core_ready") == []
    assert main.gen_state == "FAILED" and main.status == 3


def test_core_ready_gate_skips_when_not_generating(env, monkeypatch, captured_events) -> None:
    """发布闸语义：gen_state 非 GENERATING（已 CORE_READY / 已终态）一律不再发布。"""
    with db_session.session_scope() as session:
        main = ItineraryMain(user_id=7, title="闸门", city=CITY, days=2, persons=1, status=1, gen_state="GENERATING")
        session.add(main)
        session.flush()
        trip_id = main.id
    timing = itinerary_generation._StageClock(trip_id)
    itinerary_generation._publish_core_ready(trip_id, timing)
    itinerary_generation._publish_core_ready(trip_id, timing)
    ready = _events_of(captured_events, "core_ready")
    assert len(ready) == 1, "第二次调用被条件更新挡下（只能发布一次）"
    assert ready[0]["revision"] == 0 and ready[0]["daysEmitted"] == 0, "数值读自 DB 现值"

    with db_session.session_scope() as session:
        session.execute(
            update(ItineraryMain).where(ItineraryMain.id == trip_id).values(gen_state="COMPLETED", status=2)
        )
    captured_events.clear()
    itinerary_generation._publish_core_ready(trip_id, timing)
    assert _events_of(captured_events, "core_ready") == [], "终态后不再补发里程碑"


def test_recovery_treats_core_ready_as_in_progress(client, monkeypatch, captured_events) -> None:
    """崩溃窗口：core_ready 已写、终态未写（CORE_READY + status=1）→ 恢复扫描必须接手，
    数据齐时按「数据其实齐了」补终态，不重跑生成。"""
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: None)
    _fake_agents(monkeypatch, [_plan(1, ["西湖"]), _plan(2, ["灵隐寺"])])
    detail = _trip(client, {"city": CITY, "days": 2})
    itinerary_generation.reset_active_planning_for_tests()
    trip_id = detail["id"]
    with db_session.session_scope() as session:
        for day in session.execute(select(ItineraryDay)).scalars().all():
            day.generation_status = "SUCCEEDED"
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.status, main.gen_state = 1, "CORE_READY"
        main.updated_at = datetime.now() - timedelta(minutes=30)

    assert generation_recovery.recover() == 1, "CORE_READY 是生成中过程态，恢复扫描不能漏"
    main = _get_main(trip_id)
    assert main.status == 2 and main.gen_state == "COMPLETED", "数据齐了补终态，不重花钱"
    assert _events_of(captured_events, "core_ready") == [], "恢复补终态不重复发布里程碑"


# ------------------------------------------------------------ 5. 阶段计时 ----


def test_stage_timing_records_milestones(client, monkeypatch, captured_events) -> None:
    """submit→研究完成→首个 day 落库→core_ready→终态的分段耗时事件存在且非负数值型。"""
    events = [
        {"type": "day", "plan": _wire_plan(1, ["西湖"])},
        {"type": "day", "plan": _wire_plan(2, ["灵隐寺"])},
        {
            "type": "done",
            "daysExpected": 2,
            "daysEmitted": [1, 2],
            "tripTheme": None,
            "complete": True,
            "message": None,
        },
    ]
    _fake_agents(monkeypatch, [_plan(1, ["x"]), _plan(2, ["y"])], stream_events=events)
    _run_inline(monkeypatch)
    _trip(client, {"city": CITY, "days": 2, "stayNights": 1})

    timings = _events_of(captured_events, "stage_timing")
    stages = [row["stage"] for row in timings]
    assert {"research", "first_day", "core_ready", "terminal"} <= set(stages)
    for row in timings:
        assert isinstance(row["elapsedMs"], int) and row["elapsedMs"] >= 0, "只记录不设阈值，数值型非负"
    marks = [stages.index(stage) for stage in ("research", "first_day", "core_ready", "terminal")]
    assert marks == sorted(marks), "里程碑按时间序发布"


# ------------------------------------------------------ 3. 主输出缺 suggestions ----


def test_open_trip_output_schema_drops_suggestions() -> None:
    """整趟输出 schema 去 suggestions；day 级（逐日兜底链）口径不变。"""
    schema = open_trip_output_schema()
    assert "suggestions" not in schema["properties"]
    assert "suggestions" not in schema["required"]
    day_schema = open_day_output_schema(1)
    assert "suggestions" in day_schema["properties"], "逐日链的建议契约不动"


def test_open_trip_prompt_no_longer_requires_suggestions() -> None:
    prompt = open_trip_system_prompt(days=3, hotel_clause="", min_active_minutes=240, max_daily_minutes=480)
    assert "24-40" not in prompt, "长备选数量契约已从整趟 prompt 移除"
    assert '"suggestions":[' not in prompt and "另必须输出" not in prompt
    assert "不要输出 suggestions" in prompt, "明确告知模型备选由系统构建，避免白烧 token"


def test_assemble_trip_output_missing_suggestions_is_empty_array() -> None:
    data = {
        "trip_theme": "湖光山色",
        "daily_plans": [
            {
                "day_no": 1,
                "theme": "d1",
                "note": "n1",
                "items": [{"item_type": "attraction", "poi_name": "西湖", "start_time": "09:00", "end_time": "11:00"}],
            }
        ],
    }
    plans, suggestions = assemble_trip_output(data, 1)
    assert len(plans) == 1 and suggestions == [], "缺失如实返回空数组，不报错"
    # 模型仍输出（json_object 降级档不强制 schema）时照常收入，不丢既有能力
    _plans2, suggestions2 = assemble_trip_output(
        {**data, "suggestions": [{"poi_name": "断桥", "city": "杭州", "category": "attraction"}]}, 1
    )
    assert suggestions2 == [{"poi_name": "断桥", "city": "杭州", "category": "attraction"}]


def test_floor_suggestions_builds_from_research_pool_when_model_gives_none() -> None:
    """缺口补全路径对「全空」同样成立：floor_suggestions 从研究候选池直接构建。"""
    pool = (
        [{"name": f"景点{i}", "category": "attraction"} for i in range(6)]
        + [{"name": f"美食{i}", "category": "food"} for i in range(6)]
        + [{"name": f"酒店{i}", "category": "hotel"} for i in range(5)]
    )
    rows = floor_suggestions([], pool)
    cats = {row["category"] for row in rows}
    assert {"attraction", "food", "hotel"} <= cats
    assert len(rows) >= 12, "主类 ≥4 的地板从池中构建（不只是补不足）"


def test_generate_open_plans_builds_suggestions_without_model_suggestions(monkeypatch) -> None:
    """open_plans 层：模型整趟输出零 suggestions → 备选池仍从研究候选池构建。"""
    from app.agent.generation.content import landing
    from app.agent.generation.orchestration import open_plans
    from app.schemas.trip import GenerateRequest

    candidates = [{"name": f"景点{i}", "category": "attraction"} for i in range(6)]
    foods = [{"name": f"美食{i}", "category": "food"} for i in range(6)]
    hotels = [{"name": f"酒店{i}", "category": "hotel"} for i in range(5)]
    plans = [
        {
            "day_no": day_no,
            "note": f"第 {day_no} 天",
            "items": [
                {"item_type": "attraction", "poi_name": f"景点{day_no}", "start_time": "09:00", "end_time": "11:00"},
                {"item_type": "food", "poi_name": f"美食{day_no}", "start_time": "11:30", "end_time": "13:00"},
            ],
        }
        for day_no in (1, 2)
    ]
    monkeypatch.setattr(open_plans, "llm_open_trip", lambda req: (plans, []))
    monkeypatch.setattr(landing, "local_ground", lambda item, city: None)
    monkeypatch.setattr(open_plans, "activity_floor", lambda city: [])  # 体验类联网补池离线钉空

    increment, errors = open_plans.generate_open_plans(
        GenerateRequest(city="杭州", days=2, persons=2),
        "",
        context_hotels=hotels,
        candidates=candidates,
        foods=foods,
    )
    assert increment is not None and errors == []
    raw = increment["raw_suggestions"]
    assert raw, "模型零建议时备选池由研究池构建，不为空"
    cats = {row["category"] for row in raw}
    assert {"attraction", "food", "hotel"} <= cats


# ------------------------------------------------------ 4. 富化绑定 revision ----


@pytest.fixture
def sqlite_env(monkeypatch, tmp_path):
    from app.services import state_and_sessions

    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    engine = create_engine(f"sqlite:///{tmp_path / 'm5b-enricher.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()


def _seed_main(**overrides) -> int:
    with db_session.session_scope() as session:
        main = ItineraryMain(
            user_id=7,
            title="杭州 2 日游",
            city="杭州",
            days=2,
            persons=2,
            status=2,
            **overrides,
        )
        session.add(main)
        session.flush()
        return main.id


def _set_suggestions(itinerary_id: int, rows: list[dict]) -> None:
    with db_session.session_scope() as session:
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(suggestions_json=json.dumps(rows, ensure_ascii=False))
        )


def _stored_rows(itinerary_id: int) -> list[dict]:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        raw = main.suggestions_json if main is not None else None
    if not raw:
        return []
    return json.loads(raw)


def _bump_revision(itinerary_id: int) -> None:
    """带外推进修订号 = 模拟 core_ready 后用户编辑（M4 内容写路径同语义）。"""
    with db_session.session_scope() as session:
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(planning_revision=ItineraryMain.planning_revision + 1)
        )


def test_store_suggestion_rows_writes_when_revision_matches(sqlite_env) -> None:
    itinerary_id = _seed_main(planning_revision=3)
    itinerary_enricher._store_suggestion_rows(7, itinerary_id, [{"name": "河坊街", "category": "attraction"}], 3)
    assert _stored_rows(itinerary_id)[0]["name"] == "河坊街"


def test_store_suggestion_rows_abandons_when_revision_moved(sqlite_env, caplog) -> None:
    itinerary_id = _seed_main(planning_revision=4)
    original = [{"name": "新行程的备选"}]
    _set_suggestions(itinerary_id, original)

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        itinerary_enricher._store_suggestion_rows(7, itinerary_id, [{"name": "旧富化"}], 3)

    assert _stored_rows(itinerary_id) == original, "修订号已移动：旧富化不得覆盖新行程"
    assert any("stale enrichment abandoned" in record.message for record in caplog.records)

    # legacy 口径（不传 revision：独立调用/既有测试）保持无条件写，行为不变
    itinerary_enricher._store_suggestion_rows(7, itinerary_id, [{"name": "无防护写"}])
    assert _stored_rows(itinerary_id)[0]["name"] == "无防护写"


def test_enrich_itinerary_binds_revision_end_to_end(sqlite_env, monkeypatch) -> None:
    """enrich_itinerary 全链：expected_revision 不匹配 → suggestions_json 分文不动；
    以现值重新提交 → 坐标回填正常落库。"""
    itinerary_id = _seed_main(planning_revision=3)
    _set_suggestions(
        itinerary_id,
        [
            {"name": "雷峰塔", "category": "attraction", "latitude": None, "longitude": None},
            {"name": "河坊街", "category": "attraction", "latitude": None, "longitude": None},
        ],
    )
    monkeypatch.setattr(itinerary_enricher, "run_butler_note", lambda payload: "")
    monkeypatch.setattr(itinerary_enricher, "run_poi_intros", lambda city, names, intent=None: {})
    evict_calls: list[tuple] = []
    monkeypatch.setattr(
        itinerary_enricher.itinerary_query,
        "evict_detail",
        lambda user_id, itinerary: evict_calls.append((user_id, itinerary)),
    )

    def _verify(rows, city):
        return [dict(row, latitude=30.25, longitude=120.16) for row in rows], {
            "filled": len(rows),
            "dropped": 0,
            "unresolved": 0,
            "skipped": 0,
        }

    monkeypatch.setattr(itinerary_enricher, "verify_suggestion_rows", _verify)

    _bump_revision(itinerary_id)  # core_ready 后用户编辑过行程
    itinerary_enricher.enrich_itinerary(7, itinerary_id, _REQUEST, 3)
    rows = _stored_rows(itinerary_id)
    assert all(row["latitude"] is None for row in rows), "旧 revision 的富化不落库"

    itinerary_enricher.enrich_itinerary(7, itinerary_id, _REQUEST, 4)
    rows = _stored_rows(itinerary_id)
    assert all(row["latitude"] == 30.25 for row in rows), "绑定现 revision 的富化正常写入"
    assert (7, itinerary_id) in evict_calls, "写回后仍精确失效详情缓存"


def _seed_day_with_item(itinerary_id: int, poi_name: str = "灵隐寺") -> int:
    with db_session.session_scope() as session:
        day = ItineraryDay(itinerary_id=itinerary_id, day_no=1, city="杭州")
        session.add(day)
        session.flush()
        item = ItineraryItem(day_id=day.id, itinerary_id=itinerary_id, item_type="attraction", poi_name=poi_name)
        session.add(item)
        session.flush()
        return item.id


def _item_intro(item_id: int) -> str | None:
    with db_session.session_scope() as session:
        item = session.get(ItineraryItem, item_id)
        return item.intro if item is not None else None


def test_butler_note_binds_revision(sqlite_env, monkeypatch, caplog) -> None:
    """A 段（plan_note）绑修订：修订号已移动 → 不写不发事件并记 WARNING；匹配 → 写+事件。"""
    itinerary_id = _seed_main(planning_revision=3)
    with db_session.session_scope() as session:
        main_row = session.get(ItineraryMain, itinerary_id)
    assert main_row is not None
    monkeypatch.setattr(itinerary_enricher, "_plans_for_butler", lambda iid: [{"day_no": 1, "items": ["灵隐寺"]}])
    monkeypatch.setattr(itinerary_enricher, "run_butler_note", lambda payload: "这是一段管家讲解，交代行程取舍。")
    events: list[int] = []
    monkeypatch.setattr(generation_events, "butler_note", lambda iid, length, preview: events.append(iid))

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        itinerary_enricher._write_butler_note(7, main_row, _REQUEST, itinerary_id, 9)
    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, itinerary_id)
        assert row is not None
        assert row.plan_note is None, "修订号已移动：旧富化讲解不落库"
    assert events == [], "未落库就不得发 butler_note 事件"
    assert any("stale butler note abandoned" in record.message for record in caplog.records)

    itinerary_enricher._write_butler_note(7, main_row, _REQUEST, itinerary_id, 3)
    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, itinerary_id)
        assert row is not None
        assert "管家讲解" in (row.plan_note or "")
    assert events == [itinerary_id], "匹配现修订：正常落库并发事件"


def test_fill_item_intros_binds_revision(sqlite_env, monkeypatch, caplog) -> None:
    """B 段（items.intro）绑修订：修订号已移动 → intro 分文不动；匹配 → 写入；
    legacy 口径（不传 revision）无条件写，行为不变。"""
    itinerary_id = _seed_main(planning_revision=3)
    item_id = _seed_day_with_item(itinerary_id, "灵隐寺")
    monkeypatch.setattr(
        itinerary_enricher, "run_poi_intros", lambda city, names, intent=None: {"灵隐寺": "千年古刹，邻飞来峰。"}
    )
    with db_session.session_scope() as session:
        main_row = session.get(ItineraryMain, itinerary_id)
    assert main_row is not None

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        itinerary_enricher._fill_item_intros(7, main_row, _REQUEST, itinerary_id, 9)
    assert _item_intro(item_id) is None, "修订号已移动：旧富化不给新行程条目写介绍"
    assert any("stale item intros abandoned" in record.message for record in caplog.records)

    itinerary_enricher._fill_item_intros(7, main_row, _REQUEST, itinerary_id, 3)
    assert _item_intro(item_id) == "千年古刹，邻飞来峰。", "匹配现修订：介绍正常写入"

    _bump_revision(itinerary_id)
    itinerary_enricher._fill_item_intros(7, main_row, _REQUEST, itinerary_id)
    assert _item_intro(item_id) == "千年古刹，邻飞来峰。", "legacy 口径（无 revision）无条件写"


def test_fill_item_intros_truncates_to_column_width(sqlite_env, monkeypatch) -> None:
    """超长介绍截断到列宽（String(600)）再写——活栈实测 DataError 会让整批介绍降级。"""
    itinerary_id = _seed_main(planning_revision=3)
    item_id = _seed_day_with_item(itinerary_id, "灵隐寺")
    long_intro = "飞" * 1000
    monkeypatch.setattr(itinerary_enricher, "run_poi_intros", lambda city, names, intent=None: {"灵隐寺": long_intro})
    with db_session.session_scope() as session:
        main_row = session.get(ItineraryMain, itinerary_id)
    assert main_row is not None

    itinerary_enricher._fill_item_intros(7, main_row, _REQUEST, itinerary_id, 3)
    stored = _item_intro(item_id)
    assert stored is not None and len(stored) == itinerary_enricher.MAX_INTRO_CHARS, "超长介绍截断到列宽，不整批丢弃"


def test_finish_submits_enrichment_with_current_revision(client, monkeypatch, captured_events) -> None:
    """_finish 捕获提交时 main.planning_revision 随富化任务传入（提交时捕获语义）。"""
    _fake_agents(monkeypatch, [_plan(1, ["西湖", "酒店A"])])
    _run_inline(monkeypatch)
    enrich_submits: list[tuple] = []

    def fake_enrich_submit(task, *args):
        enrich_submits.append((task, args))

    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", fake_enrich_submit)
    detail = _trip(client, {"city": CITY, "days": 1, "stayNights": 1})
    main = _get_main(detail["id"])
    assert enrich_submits, "富化任务已提交"
    task, args = enrich_submits[0]
    _user_id, itinerary_id, _command, revision = args
    assert task is itinerary_enricher.enrich_itinerary
    assert itinerary_id == detail["id"]
    assert revision == main.planning_revision, "绑定的是提交时刻的修订号"
    assert _events_of(captured_events, "complete")[0]["status"] == "COMPLETED"
