"""「重生成第 N 天」端点与 PARTIAL 恢复扫描的集成测试（P0-3 部分交付闭环）。

背景（审查报告 §3.2.1/§3.8.3/P0-3，复核附注已读）：部分交付（终态 PARTIAL、
空 PENDING 天）此前一键自救断裂——详情页按钮实调只重排不重生成的 optimize_day、
对空天 400；恢复扫描只扫 GENERATING/FAILED，终态 PARTIAL 零次自动恢复。

本文件钉住新语义：
1. 端点鉴权（匿名 401）/归属（他人行程 404）；
2. 只接 PENDING/FAILED/空天：已成功天与「有内容的迁移前老天」400，生成中 409；
3. 日锁幂等（双击第二发 409）+ 指纹复用（与首次生成同一 action_id/fingerprint）；
4. 配额硬线：只记分钟窗、日窗键全程不被触碰（不为补齐从未交付的天再付配额）；
5. 失败时天标 FAILED、行程终态不被拖回 FAILED（其余天不受牵连）；
6. 恢复扫描纳入终态 PARTIAL：空天自动补齐一次（gen_resumed 封顶 + complete_trip
   不清旗防无限补齐），有可见内容的天不自动碰（防卷进用户编辑）。

全离线：agent 入口在编排模块名字上打桩（同 test_generation_migration 的纪律），
断言的是编排与恢复语义，不是模型输出质量。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain, SysUser
from app.schemas.trip import DailyPlan, TripItem
from app.services import (
    day_persistence,
    generation_gate,
    generation_recovery,
    itinerary_generation,
    state_and_sessions,
    user_service,
)

PASSWORD = "example123"
CITY = "杭州"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'regen.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", "example-only-hs256-test-signing-material")
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    itinerary_generation.reset_active_planning_for_tests()
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
        session.add(SysUser(username="bob", password=user_service.hash_password(PASSWORD), status=1, role="user"))
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


@pytest.fixture
def clients(env):
    """alice（行程主人）/ bob（他人）/ anon（匿名）三只客户端共用同一 app。"""
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    alice = TestClient(app, follow_redirects=False)
    alice.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    bob = TestClient(app, follow_redirects=False)
    bob.post("/api/auth/login", json={"username": "bob", "password": PASSWORD})
    anon = TestClient(app, follow_redirects=False)
    return {"alice": alice, "bob": bob, "anon": anon}


def _plan(day_no: int, names: list[str]) -> DailyPlan:
    """可交付形状的替身产出（≥1 景点 + 餐饮、有效活动 ≥240 分钟，同 _wire_plan 基线）。"""
    return DailyPlan(
        day_no=day_no,
        note=f"第 {day_no} 天",
        theme="湖山线",
        items=[
            TripItem(poi_name=name, item_type="attraction", cost=45, start_time="09:00", end_time="11:30")
            for name in names
        ]
        + [TripItem(poi_name=f"知味观{day_no}", item_type="food", cost=40, start_time="11:30", end_time="13:00")],
    )


def _fake_agents(monkeypatch, per_day: list[DailyPlan] | None = None) -> dict:
    """打桩研究/逐日两个 agent 入口（记录调用）；富化池空转（富化与本文件断言无关）。"""
    calls: dict = {"context": 0, "day": []}

    def fake_context(city, prefs, *, itinerary_id=None, **kwargs):
        calls["context"] += 1
        return {"candidates": [{"name": "西湖"}], "foods": [], "hotels": [], "consumption": None}

    def fake_day(request):
        calls["day"].append(request.day_no)
        if per_day is None:
            raise RuntimeError("LLM 上游炸了")
        return per_day[min(request.day_no, len(per_day)) - 1]

    monkeypatch.setattr(itinerary_generation, "run_plan_context", fake_context)
    monkeypatch.setattr(itinerary_generation, "run_generate_day", fake_day)
    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", lambda task, *args: None)
    return calls


@pytest.fixture
def partial_trip(clients, monkeypatch) -> int:
    """终态 PARTIAL：day1 SUCCEEDED（带一条 item），day2 空 PENDING——行程 189 的真实形状。

    建壳后 submit 被空转（不跑编排），直接手写部分交付终态；注册表清空模拟
    「当年提交它的进程已经死了」（P1-1 存活探测的另一面，同 broken_trip 口径）。
    """
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: None)
    _fake_agents(monkeypatch)
    response = clients["alice"].post("/api/itinerary/generate", json={"city": CITY, "days": 2})
    assert response.status_code == 200, response.json()
    trip_id = response.json()["data"]["id"]
    itinerary_generation.reset_active_planning_for_tests()
    stale = datetime.now() - timedelta(minutes=30)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None, "trip 不存在"
        main.status, main.gen_state = 2, "PARTIAL"
        main.updated_at = stale
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        days[0].generation_status, days[0].updated_at = "SUCCEEDED", stale
        days[1].generation_status, days[1].updated_at = "PENDING", stale
        session.add(
            ItineraryItem(itinerary_id=trip_id, day_id=days[0].id, item_type="attraction", poi_name="西湖", deleted=0)
        )
    return trip_id


def _days(trip_id: int) -> list[ItineraryDay]:
    with db_session.session_scope() as session:
        rows = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return list(rows)


def _main(trip_id: int) -> ItineraryMain:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None, "trip 不存在"
        session.expunge(main)
        return main


def _items(day_id: int) -> list[ItineraryItem]:
    with db_session.session_scope() as session:
        rows = (
            session.execute(select(ItineraryItem).where(ItineraryItem.day_id == day_id).order_by(ItineraryItem.sort_no))
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return list(rows)


# ---------- 鉴权与归属 ----------


def test_regenerate_day_requires_auth(clients, partial_trip) -> None:
    response = clients["anon"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
    assert response.status_code == 401, "匿名不可达（默认拒绝清单只准变短）"


def test_regenerate_day_rejects_other_users_trip(clients, partial_trip, monkeypatch) -> None:
    _fake_agents(monkeypatch)
    response = clients["bob"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
    assert response.json()["code"] == 404, "他人行程不暴露存在性（require_owned_main 同款 404）"


# ---------- 端点语义 ----------


def test_regenerate_day_fills_pending_day_and_completes_trip(clients, partial_trip, monkeypatch) -> None:
    calls = _fake_agents(monkeypatch, per_day=[_plan(1, ["保俶塔"]), _plan(2, ["新西溪"])])
    response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    day2 = next(day for day in data["dayList"] if day["dayNo"] == 2)
    assert [item["poiName"] for item in day2["items"]][:1] == ["新西溪"], "空 PENDING 天被真实重生成补齐"

    days = _days(partial_trip)
    assert days[1].generation_status == "SUCCEEDED"
    # 幂等语义：与逐日主路径同一 action_id 与指纹（rebuild_request 同源重建），
    # 后续恢复/重试对同一天的写入不会撞 409。
    assert days[1].generation_action_id == f"day-{partial_trip}-2"
    command = generation_recovery.rebuild_request(_main(partial_trip))
    assert days[1].generation_fingerprint == generation_gate.request_fingerprint(command)
    # day1 不被重生成牵连：内容原样、状态保持成功
    assert days[0].generation_status == "SUCCEEDED"
    assert [item.poi_name for item in _items(days[0].id)] == ["西湖"]
    # 补齐最后缺口天后行程升级 COMPLETED（终态重算 + 版本快照 + SSE 终帧走 _finish）
    main = _main(partial_trip)
    assert main.gen_state == "COMPLETED" and main.status == 2
    assert calls["context"] == 1 and calls["day"] == [2], "研究重跑一次、只生成目标天"


def test_regenerate_day_rejects_succeeded_day(clients, partial_trip, monkeypatch) -> None:
    calls = _fake_agents(monkeypatch, per_day=[_plan(1, ["保俶塔"]), _plan(2, ["新西溪"])])
    response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/1/regenerate")
    payload = response.json()
    assert payload["code"] == 400 and "已生成成功" in payload["message"], (
        "已成功天是已交付结果，重生成不是它的改写入口（调整走对话/优化）"
    )
    assert calls["day"] == [], "拒绝路径不许烧 LLM"


def test_regenerate_day_rejects_generating_trip(clients, partial_trip, monkeypatch) -> None:
    calls = _fake_agents(monkeypatch, per_day=[_plan(2, ["新西溪"])])
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, partial_trip)
        assert main is not None
        main.status, main.gen_state = 1, "GENERATING"
    response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
    payload = response.json()
    assert payload["code"] == 409 and "正在生成中" in payload["message"]
    assert calls["day"] == [], "在跑的编排拥有这趟行程，端点不许抢跑同一天"


def test_regenerate_day_double_click_hits_day_lock(clients, partial_trip, monkeypatch) -> None:
    _fake_agents(monkeypatch, per_day=[_plan(2, ["新西溪"])])
    assert generation_gate.try_day_lock(partial_trip, 2), "测试预占日锁（模拟第一发在跑）"
    try:
        response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
        payload = response.json()
        assert payload["code"] == 409 and "正在生成中" in payload["message"], "日锁幂等：并发/双击的第二发不进 LLM"
    finally:
        generation_gate.release_day_lock(partial_trip, 2)


def test_regenerate_day_accepts_failed_day(clients, partial_trip, monkeypatch) -> None:
    """FAILED 天与 PENDING 天同属「从未交付」：都是本端点要接住的自救对象。"""
    calls = _fake_agents(monkeypatch, per_day=[_plan(1, ["保俶塔"]), _plan(2, ["新西溪"])])
    days = _days(partial_trip)
    with db_session.session_scope() as session:
        row = session.get(ItineraryDay, days[1].id)
        assert row is not None
        row.generation_status, row.generation_error = "FAILED", "模型超时"
    response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
    assert response.status_code == 200, response.json()
    assert calls["day"] == [2], "FAILED 天不设点位数前件（那是 optimize_day 的规则）"
    assert _days(partial_trip)[1].generation_status == "SUCCEEDED"


def test_regenerate_day_failure_marks_day_failed_without_failing_trip(clients, partial_trip, monkeypatch) -> None:
    _fake_agents(monkeypatch, per_day=None)  # run_generate_day 抛 RuntimeError
    response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
    payload = response.json()
    assert payload["code"] == 502 and "错误码 RuntimeError" in payload["message"], (
        "通用失败给用户可读文案 + 稳定错误码（R2-F3/API-1 口径）"
    )
    days = _days(partial_trip)
    assert days[1].generation_status == "FAILED" and days[1].generation_error
    main = _main(partial_trip)
    assert main.status == 2 and main.gen_state == "PARTIAL", "单天自救失败不许把整趟行程拖回 FAILED——其余天不受牵连"


# ---------- 配额硬线（P0-3：不为补齐从未交付的天再付一次配额） ----------


def test_regenerate_day_exempts_daily_quota_but_keeps_minute_window(clients, partial_trip, monkeypatch) -> None:
    calls = _fake_agents(monkeypatch, per_day=[_plan(1, ["保俶塔"]), _plan(2, ["新西溪"])])
    keys: list[str] = []

    def spy(key: str, window: int) -> int:
        keys.append(key)
        return 1  # 低于任一上限，隔离「记不记」与「拦不拦」

    monkeypatch.setattr(state_and_sessions, "sliding_hit", spy)
    response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/2/regenerate")
    assert response.status_code == 200, response.json()
    assert keys == ["quota:llm:min:1"], "只记分钟窗；日窗键全程不被触碰（免扣拍板的机检）"
    assert calls["day"] == [2]

    keys.clear()

    def minute_flood(key: str, window: int) -> int:
        keys.append(key)
        return settings.user_llm_runs_per_minute + 1  # 分钟窗超限

    monkeypatch.setattr(state_and_sessions, "sliding_hit", minute_flood)
    response = clients["alice"].post(f"/api/itinerary/{partial_trip}/days/1/regenerate")
    payload = response.json()
    assert payload["code"] == 429 and "过于频繁" in payload["message"], "脚本式连点仍被分钟窗拦下"


# ---------- 恢复扫描纳入终态 PARTIAL ----------


def test_recovery_scans_partial_trip_and_resumes_once(clients, partial_trip, monkeypatch) -> None:
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    assert generation_recovery.recover() >= 1, "终态 PARTIAL 进入扫描（此前零次自动恢复）"
    assert submitted == [partial_trip]
    main = _main(partial_trip)
    assert main.gen_resumed is True and main.gen_state == "GENERATING" and main.status == 1

    # 自动补齐后仍是 PARTIAL（部分天没救回来）：complete_trip 不清 gen_resumed，
    # 下一轮扫描被一次性旗封顶——防「每轮重拉同一趟半成品行程」的无限补齐。
    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, partial_trip)
        assert row is not None
        row.status, row.gen_state, row.updated_at = 2, "PARTIAL", datetime.now() - timedelta(minutes=30)
    assert _main(partial_trip).gen_resumed is True, "PARTIAL 终态不清一次性旗"
    submitted.clear()
    generation_recovery.recover()
    assert submitted == [], "gen_resumed=1 封顶：自动补齐只有一次，再失败留给用户显式重生成"


def test_complete_trip_keeps_gen_resumed_on_partial_and_resets_on_completed(partial_trip) -> None:
    """直接钉住 complete_trip 的旗语义（PARTIAL 保留 / COMPLETED 归零）。"""
    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, partial_trip)
        assert row is not None
        row.gen_resumed = True
    day_persistence.complete_trip(partial_trip, all_succeeded=False)
    main = _main(partial_trip)
    assert main.gen_state == "PARTIAL" and main.gen_resumed is True
    day_persistence.complete_trip(partial_trip, all_succeeded=True)
    main = _main(partial_trip)
    assert main.gen_state == "COMPLETED" and main.gen_resumed is False


def test_recovery_skips_partial_trip_with_visible_day_content(clients, partial_trip, monkeypatch) -> None:
    """交接陷阱护栏：未成功天上有可见内容（用户手补/终检草稿，DB 无法区分）就不自动碰。"""
    days = _days(partial_trip)
    with db_session.session_scope() as session:
        session.add(
            ItineraryItem(
                itinerary_id=partial_trip, day_id=days[1].id, item_type="food", poi_name="用户手补的餐厅", deleted=0
            )
        )
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    assert generation_recovery.recover() == 0
    assert submitted == [], "有可见内容的天只能由用户显式一键重生成，恢复轮不许卷进重生成"
