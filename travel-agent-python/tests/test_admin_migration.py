"""M6-b 后台管理迁移验证（Java `AdminController` + `AdminServiceImpl`，8 个端点）。

盯住四类会被"顺手改写"的口径：

1. `totalUsers` 是自增 ID 最大值而不是行数（含被物理删除的账号）；
2. 分页形状 `{records,total,size,current,pages}` 与 `pages` 的取整式；`page=0` 在 Java
   里会算出负 offset 并让数据库报错，这里不加钳制；
3. 管理动作的三道拒绝：非法状态值 / 用户不存在 / 操作或删除自己；
4. Agent 指标与 LLM 用量从"跨进程 HTTP + 兜底 map"变成同进程取数，
   兜底形状（全 0 + `agentAvailable:false`）必须逐项保留，前端靠它渲染降级横幅。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent.runtime.usage_store import UsageStore
from app.api.business.admin import router as admin_router
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    BudgetDetail,
    ItineraryChatMessage,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    ItineraryVersion,
    SysUser,
)
from app.services import admin_service, itinerary_query, state_and_sessions, user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"
PASSWORD = "example123"
ADMIN_ID = 1
ALICE_ID = 2
BOB_ID = 3
GHOST_ID = 9  # 自增位跳到 9：行数仍是 4，totalUsers 必须是 9
TRIP_ID = 10
DRAFT_ID = 11
GONE_ID = 12
DAY_ID = 20


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm6b.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    # 用量仪表盘指向临时 SQLite：不能读用户真实的 data/llm_usage.db
    store = UsageStore(tmp_path / "usage.db")
    monkeypatch.setattr(admin_service, "usage_store", store)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    _seed()
    yield
    store.close()
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    hashed = user_service.hash_password(PASSWORD)
    with db_session.session_scope() as session:
        session.add_all(
            [
                # created_at 各不相同：列表按 created_at 倒序，同一秒内的行两侧都是不稳定序
                SysUser(
                    id=ADMIN_ID,
                    username="root",
                    password=hashed,
                    status=1,
                    role="admin",
                    created_at=datetime(2026, 4, 1, 9, 0),
                ),
                SysUser(
                    id=ALICE_ID,
                    username="alice",
                    nickname="小爱",
                    password=hashed,
                    status=1,
                    role="user",
                    created_at=datetime(2026, 4, 2, 9, 0),
                ),
                SysUser(
                    id=BOB_ID,
                    username="bob",
                    password=hashed,
                    status=0,
                    role="user",
                    phone="13800000000",
                    created_at=datetime(2026, 4, 3, 9, 0),
                ),
                SysUser(
                    id=GHOST_ID,
                    username="ghost",
                    password=hashed,
                    status=1,
                    role="user",
                    created_at=datetime(2026, 4, 4, 9, 0),
                ),
            ]
        )
        trip = ItineraryMain(
            id=TRIP_ID,
            user_id=ALICE_ID,
            title="杭州2日游",
            city="杭州",
            days=2,
            persons=2,
            status=2,
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 21),
            budget=Decimal("3000.00"),
            created_at=datetime(2026, 4, 20, 8, 0),
        )
        draft = ItineraryMain(
            id=DRAFT_ID,
            user_id=BOB_ID,
            title="北京1日游",
            city="北京",
            days=1,
            persons=1,
            status=1,
            created_at=datetime(2026, 9, 14, 8, 0),
        )
        gone = ItineraryMain(
            id=GONE_ID,
            user_id=ALICE_ID,
            title="已删除",
            city="杭州",
            days=1,
            persons=1,
            status=2,
            deleted=1,
            created_at=datetime(2026, 4, 21, 8, 0),
        )
        session.add_all([trip, draft, gone])
        day = ItineraryDay(id=DAY_ID, itinerary_id=trip.id, day_no=1, generation_status="SUCCEEDED")
        session.add(day)
        session.flush()
        session.add_all(
            [
                ItineraryItem(
                    day_id=DAY_ID,
                    itinerary_id=TRIP_ID,
                    item_type="attraction",
                    poi_name="西湖",
                    cost=Decimal("0.00"),
                    sort_no=0,
                ),
                BudgetDetail(itinerary_id=TRIP_ID, category="门票", amount=Decimal("45.00"), item_count=1),
                ItineraryChatMessage(itinerary_id=TRIP_ID, user_id=ALICE_ID, role="ai", content="管家说"),
                ItineraryVersion(
                    itinerary_id=TRIP_ID,
                    user_id=ALICE_ID,
                    version_no=1,
                    operation="generate",
                    summary="生成",
                    snapshot_json='{"dayList":[]}',
                ),
            ]
        )


def _login(username: str) -> TestClient:
    client = TestClient(_app(), follow_redirects=False)
    client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    return client


@pytest.fixture
def admin() -> TestClient:
    return _login("root")


@pytest.fixture
def alice() -> TestClient:
    return _login("alice")


def _app():
    from fastapi import FastAPI

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(itinerary_router)  # 只为管理员删除后校验属主侧不可见
    return app


# ---------- 管理面鉴权方向 ----------


def test_admin_paths_require_admin_role(admin: TestClient, alice: TestClient) -> None:
    anon = TestClient(_app(), follow_redirects=False)
    # 匿名 401、登录但非管理员 403：与 Java SecurityConfig 的 /api/admin/** hasRole(ADMIN) 同
    assert anon.get("/api/admin/stats").status_code == 401
    assert alice.get("/api/admin/stats").status_code == 403
    assert alice.put("/api/admin/users/3/status/1").status_code == 403
    assert alice.delete("/api/admin/itineraries/10").status_code == 403
    assert admin.get("/api/admin/stats").status_code == 200


# ---------- 概览 ----------


def test_stats_counts_are_the_java_calibration(admin: TestClient) -> None:
    data = admin.get("/api/admin/stats").json()["data"]
    assert set(data) == {
        "totalUsers",
        "activeUsers",
        "disabledUsers",
        "totalItineraries",
        "todayNewUsers",
        "todayNewItineraries",
        "generatingItineraries",
    }
    # totalUsers = MAX(id) = 9 而不是 4 行：Java 有意反映"注册过的最大自增位"
    assert data["totalUsers"] == GHOST_ID
    assert data["activeUsers"] == 3 and data["disabledUsers"] == 1
    assert data["totalItineraries"] == 2, "软删行程不计数（@TableLogic 同样追加 deleted=0）"
    assert data["generatingItineraries"] == 1, "status=1 的草稿行程"
    assert data["todayNewUsers"] == 0, "种子的 created_at 都是历史时间"
    assert data["todayNewItineraries"] == 0


def test_stats_today_boundary_is_local_midnight(admin: TestClient) -> None:
    """今日口径 = 应用本地时区零点起（Java 用 LocalDate.now().atStartOfDay()）。"""
    midnight = datetime.combine(date.today(), time.min)
    with db_session.session_scope() as session:
        session.add(
            SysUser(
                username="fresh",
                password=user_service.hash_password(PASSWORD),
                status=1,
                role="user",
                created_at=midnight,
            )
        )
        session.add(
            SysUser(
                username="late-yesterday",
                password=user_service.hash_password(PASSWORD),
                status=1,
                role="user",
                created_at=midnight - timedelta(microseconds=1),
            )
        )
    data = admin.get("/api/admin/stats").json()["data"]
    assert data["todayNewUsers"] == 1, "零点整计入、差 1 微秒不计入"
    assert data["activeUsers"] == 5


# ---------- 用户分页 ----------


def test_user_page_shape_order_and_itinerary_counts(admin: TestClient) -> None:
    data = admin.get("/api/admin/users", params={"page": 1, "size": 2}).json()["data"]
    assert set(data) == {"records", "total", "size", "current", "pages"}
    assert (data["total"], data["size"], data["current"], data["pages"]) == (4, 2, 1, 2)
    # created_at 倒序：ghost(4/4) > bob(4/3) > alice(4/2) > root(4/1)
    assert [row["username"] for row in data["records"]] == ["ghost", "bob"]
    assert set(data["records"][0]) == {
        "id",
        "username",
        "nickname",
        "phone",
        "status",
        "role",
        "itineraryCount",
        "createdAt",
    }
    by_name = {row["username"]: row for row in data["records"]}
    # status 是 1/0 而不是 true/false：Java 侧字段是 Integer
    assert by_name["bob"]["status"] == 0 and by_name["ghost"]["status"] == 1
    assert by_name["bob"]["nickname"] is None and by_name["bob"]["phone"] == "13800000000"
    assert by_name["bob"]["createdAt"] == "2026-04-03T09:00"
    # 行程数含各状态、不含软删：alice 有 2 条但 1 条已删
    assert by_name["bob"]["itineraryCount"] == 1

    second = admin.get("/api/admin/users", params={"page": 2, "size": 2}).json()["data"]
    assert [row["username"] for row in second["records"]] == ["alice", "root"]
    assert second["pages"] == 2
    empty = admin.get("/api/admin/users", params={"page": 3, "size": 2}).json()["data"]
    assert empty["records"] == [] and empty["total"] == 4
    # 整除时不额外加一页（MyBatis-Plus getPages 同式）
    assert admin.get("/api/admin/users", params={"page": 1, "size": 4}).json()["data"]["pages"] == 1


def test_user_itinerary_count_is_batched(admin: TestClient, monkeypatch) -> None:
    """一页用户只能多一次 group by：逐行 count 会在管理页面上放大成 N+1。"""
    seen: list[list[int]] = []
    real = admin_service._itinerary_counts

    def spy(session, user_ids):
        seen.append(list(user_ids))
        return real(session, user_ids)

    monkeypatch.setattr(admin_service, "_itinerary_counts", spy)
    page = admin.get("/api/admin/users", params={"page": 1, "size": 3}).json()["data"]
    assert seen == [[GHOST_ID, BOB_ID, ALICE_ID]]
    counts = {row["username"]: row["itineraryCount"] for row in page["records"]}
    assert counts == {"ghost": 0, "bob": 1, "alice": 1}


def test_user_keyword_matches_username_or_nickname(admin: TestClient) -> None:
    by_nickname = admin.get("/api/admin/users", params={"keyword": "小爱"}).json()["data"]
    assert [row["username"] for row in by_nickname["records"]] == ["alice"]
    # LIKE '%o%'：bob/ghost/root 命中，alice 不命中（昵称也参与匹配）
    by_letter = admin.get("/api/admin/users", params={"keyword": "o"}).json()["data"]
    assert sorted(row["username"] for row in by_letter["records"]) == ["bob", "ghost", "root"]
    assert admin.get("/api/admin/users", params={"keyword": "  "}).json()["data"]["total"] == 4, (
        "空白关键词等价不过滤（Java 判 isBlank）"
    )


# ---------- 用户状态与删除 ----------


def test_update_status_rejects_illegal_value_missing_user_and_self(admin: TestClient) -> None:
    illegal = admin.put("/api/admin/users/3/status/2")
    assert illegal.status_code == 400 and illegal.json()["message"] == "非法的状态值"
    non_numeric = admin.put("/api/admin/users/3/status/on")
    assert non_numeric.status_code == 400, "业务路径的类型错误走 400 信封，不是 FastAPI 422"
    missing = admin.put("/api/admin/users/999/status/1")
    assert missing.status_code == 404 and missing.json()["message"] == "用户不存在"
    self_op = admin.put("/api/admin/users/1/status/0")
    assert self_op.status_code == 400 and self_op.json()["message"] == "不能操作自己的账号"


def test_update_status_flips_the_status_only(admin: TestClient) -> None:
    resp = admin.put("/api/admin/users/3/status/1")
    assert resp.status_code == 200 and resp.json() == {"code": 200, "message": "success", "data": None}
    with db_session.session_scope() as session:
        bob = session.get(SysUser, BOB_ID)
        assert bob is not None
        assert int(bob.status) == 1
        assert bob.username == "bob" and bob.role == "user" and bob.phone == "13800000000"
    assert admin.get("/api/admin/stats").json()["data"]["disabledUsers"] == 0


def test_delete_user_is_soft_and_refuses_self(admin: TestClient) -> None:
    refused = admin.delete("/api/admin/users/1")
    assert refused.status_code == 400 and refused.json()["message"] == "不能删除自己的账号"
    assert admin.delete("/api/admin/users/999").json()["message"] == "用户不存在"

    assert admin.delete("/api/admin/users/3").json()["code"] == 200
    with db_session.session_scope() as session:
        row = session.get(SysUser, BOB_ID, execution_options={"include_deleted": True})
        assert row is not None
        assert row.deleted == 1, "@TableLogic 语义：deleteById 是 UPDATE deleted=1"
    # 软删后从列表、计数与登录侧一并消失
    assert admin.get("/api/admin/users").json()["data"]["total"] == 3
    stats = admin.get("/api/admin/stats").json()["data"]
    assert (stats["activeUsers"], stats["disabledUsers"]) == (3, 0)
    assert _login("bob").get("/api/admin/stats").status_code == 401, "已删账号的会话不再被认成登录态"


# ---------- 行程分页与删除 ----------


def test_itinerary_page_filters_combine(admin: TestClient) -> None:
    all_rows = admin.get("/api/admin/itineraries").json()["data"]
    assert [row["title"] for row in all_rows["records"]] == ["北京1日游", "杭州2日游"]
    assert set(all_rows["records"][0]) == {
        "id",
        "userId",
        "title",
        "city",
        "startDate",
        "endDate",
        "days",
        "persons",
        "budget",
        "status",
        "createdAt",
    }
    by_city = admin.get("/api/admin/itineraries", params={"keyword": "杭州"}).json()["data"]
    assert [row["title"] for row in by_city["records"]] == ["杭州2日游"], "关键词命中 title 或 city"
    by_owner = admin.get("/api/admin/itineraries", params={"userId": ALICE_ID}).json()["data"]
    assert [row["title"] for row in by_owner["records"]] == ["杭州2日游"]
    both = admin.get("/api/admin/itineraries", params={"userId": ALICE_ID, "status": 1}).json()["data"]
    assert both["total"] == 0, "多条件之间是 AND"
    generated = admin.get("/api/admin/itineraries", params={"status": 2}).json()["data"]
    row = generated["records"][0]
    assert row["startDate"] == "2026-04-20" and row["endDate"] == "2026-04-21"
    assert row["budget"] == 3000.0 and row["createdAt"] == "2026-04-20T08:00"


def test_admin_delete_itinerary_cascades_without_a_snapshot(admin: TestClient, alice: TestClient) -> None:
    # 先把属主的详情写进缓存，验证管理员删除会失效它
    itinerary_query.detail(ALICE_ID, TRIP_ID)
    cache_key = f"{ALICE_ID}:{TRIP_ID}"
    assert cache_store.get_json(itinerary_query.DETAIL_CACHE_NAMESPACE, cache_key) is not None

    assert admin.delete(f"/api/admin/itineraries/{TRIP_ID}").json()["code"] == 200
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, TRIP_ID, execution_options={"include_deleted": True})
        assert main is not None
        assert main.deleted == 1, "主表软删"
        for model in (ItineraryDay, ItineraryItem, BudgetDetail):
            rows = (
                session.execute(
                    select(model).execution_options(include_deleted=True).where(model.itinerary_id == TRIP_ID)
                )
                .scalars()
                .all()
            )
            assert rows and all(row.deleted == 1 for row in rows), f"{model.__tablename__} 必须一并软删，否则留孤儿"
        assert session.execute(select(ItineraryChatMessage)).scalars().all() == [], (
            "chat 表无 deleted 列：Java 同样是物理删"
        )
        versions = session.execute(select(ItineraryVersion)).scalars().all()
        assert [v.operation for v in versions] == ["generate"], "管理员删除不做版本快照（归属校验在快照侧）"
    assert cache_store.get_json(itinerary_query.DETAIL_CACHE_NAMESPACE, cache_key) is None
    again = admin.delete(f"/api/admin/itineraries/{TRIP_ID}")
    assert again.status_code == 404 and again.json()["message"] == "行程不存在"
    assert alice.get("/api/itinerary").json()["data"] == []


# ---------- Agent 指标与 LLM 用量 ----------

METRICS_UNAVAILABLE = {
    "agentAvailable": False,
    "runs": 0,
    "successes": 0,
    "failures": 0,
    "degraded_runs": 0,
    "llm_calls": 0,
    "tool_calls": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "success_rate": 0.0,
    "failure_rate": 0.0,
    "degraded_rate": 0.0,
    "avg_event_latency_ms": 0.0,
    "recent_failures": [],
}


def test_agent_metrics_reports_in_process_snapshot(admin: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(
        admin_service.metrics, "snapshot", lambda: {"runs": 5, "successes": 4, "failures": 1, "recent_failures": []}
    )
    data = admin.get("/api/admin/agent-metrics").json()["data"]
    assert data["agentAvailable"] is True and data["runs"] == 5

    def boom() -> dict:
        raise RuntimeError("observability exploded")

    monkeypatch.setattr(admin_service.metrics, "snapshot", boom)
    downgraded = admin.get("/api/admin/agent-metrics").json()["data"]
    # 兜底 map 逐项对齐 Java：前端读 agentAvailable 显示降级横幅，其余格子要有 0 可渲染
    assert downgraded == METRICS_UNAVAILABLE
    assert "circuitBreaker" not in downgraded, "Java→Python 那一跳没了，熔断快照刻意不移植"


def test_llm_usage_matches_the_agent_endpoint_payload(admin: TestClient) -> None:
    from app.api import agent as agent_api

    store = admin_service.usage_store
    store.record("generate", "qwen-plus", 100, 20, 500, True)
    store.record("clarify", "qwen-turbo", 10, 2, 100, False, "timeout")

    data = admin.get("/api/admin/llm-usage", params={"range": "1h"}).json()["data"]
    assert set(data) == {"range", "bucket", "summary", "by_scene", "by_model", "timeline", "calls", "agentAvailable"}
    assert data["agentAvailable"] is True
    assert data["range"] == "1h" and data["bucket"] == 60, "1h 按分钟分桶"
    assert data["summary"]["calls"] == 2 and data["summary"]["failures"] == 1
    assert [row["scene"] for row in data["by_scene"]] == ["generate", "clarify"]
    assert data["calls"]["total"] == 2
    assert {row["scene"]: row["success"] for row in data["calls"]["records"]} == {"generate": True, "clarify": False}

    # 同一份取数：内部 agent 端点也必须给归一化后的窗口（M6 把这段逻辑收敛进 usage_store）
    payload = agent_api.agent_usage(range="7d").data
    assert payload is not None
    assert payload["range"] == "7d" and payload["bucket"] == 86400
    unknown = admin.get("/api/admin/llm-usage", params={"range": "yesterday"}).json()["data"]
    assert unknown["range"] == "24h" and unknown["bucket"] == 3600, "未知 range 回落 24h"


def test_llm_usage_limits_and_degraded_shape(admin: TestClient, monkeypatch) -> None:
    seen: list[tuple[int, int]] = []
    real_calls = admin_service.usage_store.calls

    def spy(start_ts, end_ts, limit=100, offset=0):
        seen.append((limit, offset))
        return real_calls(start_ts, end_ts, limit, offset)

    monkeypatch.setattr(admin_service.usage_store, "calls", spy)
    admin_service.llm_usage("24h", 99999, -5)
    assert seen == [(500, 0)], "limit 夹到 500、offset 不为负：两条端点共用的收敛规则"

    def boom(range_key, limit, offset):
        raise RuntimeError("usage db locked")

    monkeypatch.setattr(admin_service.usage_store, "report", boom)
    downgraded = admin.get("/api/admin/llm-usage", params={"range": "7d"}).json()["data"]
    # 兜底里 range 原样回显入参、bucket 恒为 3600（Java 既有形状，前端只读 agentAvailable）
    assert downgraded == {
        "agentAvailable": False,
        "range": "7d",
        "bucket": 3600,
        "summary": {
            "calls": 0,
            "successes": 0,
            "failures": 0,
            "success_rate": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "avg_duration_ms": 0.0,
        },
        "by_scene": [],
        "by_model": [],
        "timeline": [],
        "calls": {"total": 0, "records": []},
    }
