"""行程照看 S1+S2+S3：持久状态、归属、统一检查执行与后台建议发布（规格 §4.1–§4.5）。

隔离测试库 + TestClient（装配同 tests/test_itinerary_read_migration.py 的轻量模式）。
S1 覆盖：隔离库可创建/读取/暂停；跨用户拒绝；开启资格与监控范围边界。
S2 覆盖（规格 §4.4 步骤 1–4、9）：无影响/不可用/日期太远/已出发零模型零消息且状态
如实；概率漂移不重复通知；cron 与立即检查并发只赢一次；过期 token 可重领；重启可读；
暂停/禁用/删除/归档停止外呼不发布。
S3 覆盖（规格 §4.4 步骤 5–9、§4.5）：后台费用/身份上下文绑定（owner/scene/90s/4 次）、
BYOK 路由与 byok 账本归属、暂停后已结算费用保留、发布完整可确认草稿（完整天数、
未受影响日原样、_confirmationThread）、生成期间出现
用户草稿/行程变化即丢弃、失败不留键不发布、care 确认线程只应用自己草稿且不碰用户
checkpoint、当日尝试与去重键只随发布走、GET 按消息现状派生 pending/applied/stale、
忽略后同风险不重复通知。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import date, datetime, timedelta
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent.editing.weather_care import (
    CareAdjustInput,
    CareAdjustResult,
    CareAffectedDay,
    CareWatchedItem,
)
from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common.cache_store import reset_for_tests as reset_cache
from app.common.clock import shanghai_today
from app.common.config import settings
from app.common.envelope import ApiError, install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    ItineraryCare,
    ItineraryChatMessage,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.schemas.trip import ChatTurnRequest, ChatTurnResponse
from app.services import (
    itinerary_care_check,
    itinerary_care_publish,
    itinerary_care_risk,
    itinerary_chat,
    itinerary_plan_apply,
)

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture
def care_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'care.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "care_enabled", True)
    monkeypatch.setattr(settings, "weather_enabled", True)
    reset_cache()
    _seed()
    yield
    db_session.init_engine(None, None)
    reset_cache()


def _seed() -> None:
    from app.services import user_service

    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        session.add_all(
            [
                SysUser(username="alice", password=hashed, status=1, role="user"),
                SysUser(username="mallory", password=hashed, status=1, role="user"),
            ]
        )


def _make_trip(
    user_id: int = 1,
    *,
    start_in_days: int = 3,
    trip_days: int = 2,
    gen_state: str | None = "COMPLETED",
    archived: bool = False,
) -> tuple[int, list[int]]:
    """造一趟完整交付、未出发的行程；返回 (trip_id, attraction_item_ids)。"""
    today = shanghai_today()
    with db_session.session_scope() as session:
        start = today + timedelta(days=start_in_days)
        trip = ItineraryMain(
            user_id=user_id,
            title=f"照看-{start.isoformat()}",
            city="杭州",
            start_date=start,
            end_date=start + timedelta(days=trip_days - 1),
            days=trip_days,
            persons=1,
            status=2,
            gen_state=gen_state,
            archived=archived,
            stay_nights=0,
        )
        session.add(trip)
        session.flush()
        ids: list[int] = []
        for day_no in range(1, trip_days + 1):
            day = ItineraryDay(
                itinerary_id=trip.id,
                day_no=day_no,
                travel_date=start + timedelta(days=day_no - 1),
                generation_status="SUCCEEDED",
            )
            session.add(day)
            session.flush()
            for name in (f"景点{day_no}a", f"景点{day_no}b"):
                item = ItineraryItem(
                    day_id=day.id,
                    itinerary_id=trip.id,
                    item_type="attraction",
                    poi_name=name,
                    sort_no=len(ids),
                )
                session.add(item)
                session.flush()
                ids.append(item.id)
        return trip.id, ids


def _client(username: str = "alice") -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": username, "password": "x"}).status_code == 200
    return client


def _enable(client: TestClient, trip_id: int, watched: list[int] | None = None) -> httpx.Response:
    body: dict = {"enabled": True, "allowBackgroundAi": True}
    if watched is not None:
        body["watchedItemIds"] = watched
    return client.put(f"/api/itinerary/{trip_id}/care", json=body)


# ---------- 创建 / 读取 / 暂停 ----------


def test_care_create_read_pause_roundtrip(care_db):
    trip_id, item_ids = _make_trip()
    client = _client()
    assert client.get(f"/api/itinerary/{trip_id}/care").json()["data"] == {
        "supported": True,
        "enabled": False,
        "watchedItemIds": [],
        "status": None,
        "nextCheckAt": None,
        "lastCheckedAt": None,
        "lastResult": None,
        "needsReselect": False,
    }

    data = _enable(client, trip_id, item_ids).json()["data"]
    assert data["enabled"] is True and data["supported"] is True
    assert data["watchedItemIds"] == item_ids
    assert data["status"] is None and data["nextCheckAt"] is not None, "开启即置 next_check_at（规格 §3.2）"

    # 暂停：不要求再次授权；保留监控范围，停止扫描
    data = client.put(f"/api/itinerary/{trip_id}/care", json={"enabled": False}).json()["data"]
    assert data["enabled"] is False and data["status"] == "disabled"
    assert data["watchedItemIds"] == item_ids and data["nextCheckAt"] is None
    # 重新开启复用既有监控范围，无需重选
    data = _enable(client, trip_id).json()["data"]
    assert data["enabled"] is True and data["watchedItemIds"] == item_ids


def test_care_enable_requires_explicit_background_ai_consent(care_db):
    trip_id, item_ids = _make_trip()
    client = _client()
    for body in ({"enabled": True, "watchedItemIds": item_ids}, {"enabled": True, "allowBackgroundAi": False}):
        response = client.put(f"/api/itinerary/{trip_id}/care", json=body)
        assert response.status_code == 400, "后台 AI 费用授权必须显式 true（规格 §3.2）"
    with db_session.session_scope() as session:
        assert session.scalars(select(ItineraryCare)).all() == [], "失败的开启不得留下半行状态"


def test_care_cross_user_is_404_not_403(care_db):
    trip_id, item_ids = _make_trip(user_id=1)
    mallory = _client("mallory")
    assert mallory.get(f"/api/itinerary/{trip_id}/care").status_code == 404
    put = mallory.put(
        f"/api/itinerary/{trip_id}/care", json={"enabled": True, "watchedItemIds": item_ids, "allowBackgroundAi": True}
    )
    assert put.status_code == 404, "非所有者 404，不暴露资源存在性（规格 §4.3）"
    alice = _client()
    assert alice.get(f"/api/itinerary/{trip_id}/care").json()["data"]["enabled"] is False, "跨用户写不得落库"


# ---------- 开启资格（规格 §3.2） ----------


def test_care_enable_blocked_for_departed_trip(care_db):
    trip_id, item_ids = _make_trip(start_in_days=0)
    client = _client()
    response = _enable(client, trip_id, item_ids)
    assert response.status_code == 400 and "出发" in response.json()["message"]


def test_care_enable_blocked_for_partial_delivery(care_db):
    trip_id, item_ids = _make_trip(gen_state="PARTIAL")
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 400, "PARTIAL 是部分交付，不算完整天交付"


def test_care_enable_blocked_for_archived_trip(care_db):
    trip_id, item_ids = _make_trip(archived=True)
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 400


def test_care_supported_false_for_legacy_or_incomplete(care_db):
    trip_id, _ids = _make_trip(gen_state=None)
    client = _client()
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["supported"] is False and data["enabled"] is False


# ---------- 监控范围 ----------


def test_care_watched_items_must_be_current_attractions(care_db):
    trip_id, item_ids = _make_trip()
    _other_trip, other_ids = _make_trip()
    client = _client()
    with db_session.session_scope() as session:
        day_id = session.scalars(select(ItineraryDay.id).where(ItineraryDay.itinerary_id == trip_id)).first()
        food = ItineraryItem(day_id=day_id, itinerary_id=trip_id, item_type="food", poi_name="楼外楼", sort_no=9)
        session.add(food)
        session.flush()
        food_id = food.id
    for watched in ([999999], other_ids, [item_ids[0], food_id]):
        response = _enable(client, trip_id, watched)
        assert response.status_code == 400, f"非本行程 attraction 的 ID 必须拒绝：{watched}"
    response = _enable(client, trip_id)
    assert response.status_code == 400, "新任务无既有范围时禁止空范围开启（规格 §3.2）"


def test_care_watched_upper_bound_is_20(care_db):
    trip_id, item_ids = _make_trip()
    client = _client()
    response = _enable(client, trip_id, item_ids + [999999] * 19)
    assert response.status_code == 400


def test_care_needs_reselect_after_all_watch_ids_deleted(care_db):
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    with db_session.session_scope() as session:
        for item in session.query(ItineraryItem).filter(ItineraryItem.itinerary_id == trip_id).all():
            item.deleted = 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["enabled"] is True and data["needsReselect"] is True, "全部失效时显示需重新选择（规格 §3.2）"
    assert data["supported"] is True, "行程本身仍合格，只是监控范围空了"


# ---------- 全局开关 ----------


def test_care_write_404_and_read_unsupported_when_globally_disabled(care_db, monkeypatch):
    monkeypatch.setattr(settings, "care_enabled", False)
    trip_id, item_ids = _make_trip()
    client = _client()
    assert client.get(f"/api/itinerary/{trip_id}/care").json()["data"]["supported"] is False
    assert _enable(client, trip_id, item_ids).status_code == 404, "全局关闭时写端点按功能不存在处理"


def test_care_supported_false_when_weather_disabled(care_db, monkeypatch):
    monkeypatch.setattr(settings, "weather_enabled", False)
    trip_id, item_ids = _make_trip()
    client = _client()
    assert client.get(f"/api/itinerary/{trip_id}/care").json()["data"]["supported"] is False
    assert _enable(client, trip_id, item_ids).status_code == 400, "天气功能未开启不能开启照看（规格 §3.2）"


# ---------- S2：统一检查执行（规格 §4.4 步骤 1–4、9；模型流程与发布在 S3） ----------


class _WeatherFake:
    """天气适配器桩：记录调用，返回预设逐日行（rows）或动态结果（dynamic）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.rows: list[dict[str, object]] | None = None
        self.dynamic: Callable[[str | None, str | None], list[dict[str, object]] | None] | None = None

    def __call__(self, city: str, start_date: str | None, end_date: str | None) -> list[dict[str, object]] | None:
        self.calls.append((city, str(start_date), str(end_date)))
        if self.dynamic is not None:
            return self.dynamic(start_date, end_date)
        return self.rows


@pytest.fixture
def care_run(care_db, monkeypatch):
    """S2 执行面：天气桩 + 模型绊线——任何直接走门面的模型调用都让测试失败。"""
    fake = _WeatherFake()
    monkeypatch.setattr(itinerary_care_risk, "get_weather_forecast", fake)

    def _no_model(*args: object, **kwargs: object) -> object:
        raise AssertionError("本场景不允许任何模型调用")

    monkeypatch.setattr("app.agent.run_chat_turn", _no_model)
    return fake


class _CareModelStub:
    """S3 生成桩：替换 run_weather_care_adjustment；成功时按输入回一份通过复核的草稿
    （受影响日的监控项改时间，其余天逐字段原样），可编程失败与调用侧效应。"""

    def __init__(self) -> None:
        self.calls: list[CareAdjustInput] = []
        self.behavior = "succeed"  # succeed | fail | raise
        self.on_call: Callable[[CareAdjustInput], None] | None = None

    def __call__(self, data: CareAdjustInput) -> CareAdjustResult:
        self.calls.append(data)
        if self.on_call is not None:
            self.on_call(data)
        if self.behavior == "raise":
            raise RuntimeError("model exploded")
        if self.behavior == "fail":
            return CareAdjustResult(ok=False, reason="模型返回的方案没有通过复核")
        plans = deepcopy(data.plans)
        watched_ids = {item.id for affected in data.affected_days for item in affected.items}
        for affected in data.affected_days:
            plan = next(plan for plan in plans if plan["day_no"] == affected.day_no)
            for item in plan["items"]:
                if item.get("id") in watched_ids:
                    item["start_time"] = "10:30"
                    item["end_time"] = "11:30"
        response = ChatTurnResponse(reply="雨天建议调整时间", plans=plans, changed=True, requires_confirmation=True)
        return CareAdjustResult(ok=True, reply="雨天建议调整时间", plans=plans, response=response)


@pytest.fixture
def care_model(care_run, monkeypatch):
    """S3 模型流程桩：挂在 services 的调用点（itinerary_care_check）上。"""
    stub = _CareModelStub()
    monkeypatch.setattr(itinerary_care_publish, "run_weather_care_adjustment", stub)
    return stub


@pytest.fixture
def _sync_pool(monkeypatch):
    """把 chat_pool.submit 换成同步执行并记录提交；返回提交记录列表。"""
    seen: list[tuple[object, ...]] = []

    def _submit(task: Callable[..., object], *args: object) -> Future[object]:
        seen.append(args)
        return cast("Future[object]", task(*args))

    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", _submit)
    return seen


def _day_rows(day: date, *, code: int = 0, prob: int = 10) -> list[dict[str, object]]:
    """单日观测行：code=0 晴、61 雨；默认行程（start=今天+3）的窗口只有第 1 天。"""
    return [
        {
            "date": day.isoformat(),
            "code": code,
            "text": "晴" if code == 0 else "雨",
            "precip_prob": prob,
            "t_max": 25.0,
            "t_min": 18.0,
        }
    ]


def _chat_message_count() -> int:
    with db_session.session_scope() as session:
        return len(session.scalars(select(ItineraryChatMessage)).all())


def _care_row(trip_id: int) -> ItineraryCare:
    with db_session.session_scope() as session:
        row = session.get(ItineraryCare, trip_id)
        assert row is not None
        return row


def _become_due(trip_id: int) -> None:
    """推进扫描时钟：next_check_at 拨回过去，下一次 scan 必然拾取。"""
    with db_session.session_scope() as session:
        row = session.get(ItineraryCare, trip_id)
        assert row is not None
        row.next_check_at = datetime.now() - timedelta(seconds=1)


def test_care_scan_no_risk_is_honest_and_silent(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    window_day = shanghai_today() + timedelta(days=3)
    care_run.rows = _day_rows(window_day)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "no_risk"
    result = data["lastResult"]
    assert result["status"] == "no_risk" and result["reason"]
    assert result["days"] == [
        {"date": window_day.isoformat(), "precipProb": 10.0, "weatherCode": 0, "condition": "晴", "risk": False}
    ]
    assert result["affectedItemIds"] == [] and result["checkedAt"] is not None
    assert data["lastCheckedAt"] is not None and data["nextCheckAt"] is not None, "每 6 小时推进（规格 §3.2）"
    row = _care_row(trip_id)
    assert row.check_token is None and row.checking_until is None, "检查完释放争抢令牌（规格 §4.4 步骤 9）"
    assert (row.notified_risks_json or []) == [] and row.last_ai_attempt_on is None, "无影响不得占当日 AI 尝试"
    assert care_run.calls == [("杭州", window_day.isoformat(), window_day.isoformat())], "显式上海口径窗口"
    assert _chat_message_count() == 0, "无影响零新消息（规格 §3.3）"


def test_care_scan_weather_none_marks_unavailable_and_keeps_last_observation(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    window_day = shanghai_today() + timedelta(days=3)
    care_run.rows = _day_rows(window_day)
    assert itinerary_care_check.scan_due_checks() == 1
    care_run.rows = None
    _become_due(trip_id)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "unavailable"
    result = data["lastResult"]
    assert result["status"] == "unavailable" and result["reason"] == "暂未取得天气信息"
    assert len(result["days"]) == 1, "数据缺失不覆盖最后有效观测（规格 §3.3）"
    assert result["checkedAt"] is not None and result["affectedItemIds"] == []
    assert _chat_message_count() == 0 and _care_row(trip_id).last_ai_attempt_on is None


@pytest.mark.parametrize("weather", ["empty", "missing_day", "missing_fields", "missing_prob", "missing_code"])
def test_care_incomplete_weather_is_unavailable_without_model(care_model, care_run, weather):
    trip_id, item_ids = _make_trip(start_in_days=2)
    client = _client()
    _enable(client, trip_id, item_ids)
    first_day = shanghai_today() + timedelta(days=2)
    care_run.rows = _day_rows(first_day) + _day_rows(first_day + timedelta(days=1))
    if weather == "empty":
        care_run.rows = []
    elif weather == "missing_day":
        care_run.rows = care_run.rows[:1]
    else:
        if weather in ("missing_fields", "missing_code"):
            care_run.rows[1]["code"] = None
        if weather in ("missing_fields", "missing_prob"):
            care_run.rows[1]["precip_prob"] = None
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "unavailable"
    assert data["lastResult"]["reason"] == "暂未取得天气信息"
    assert care_model.calls == [] and _chat_message_count() == 0
    assert _care_row(trip_id).last_ai_attempt_on is None


def test_care_scan_far_future_waits_without_any_call(care_run):
    trip_id, item_ids = _make_trip(start_in_days=10)
    client = _client()
    _enable(client, trip_id, item_ids)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "waiting"
    assert care_run.calls == [], "窗口外零外呼（规格 §3.2 保留任务等待）"
    assert data["nextCheckAt"] is not None, "等待也要推进下一次检查时刻"
    assert data["lastResult"]["days"] == [] and _chat_message_count() == 0


def test_care_scan_departed_stops_and_leaves_scan(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.start_date = shanghai_today()  # 出发当天
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "finished" and data["nextCheckAt"] is None
    assert care_run.calls == [] and _chat_message_count() == 0
    assert itinerary_care_check.scan_due_checks() == 0, "终态离开扫描"


def test_care_scan_archived_or_deleted_trip_stops_without_calls(care_run):
    trip_a, ids_a = _make_trip()
    trip_b, ids_b = _make_trip()
    client = _client()
    _enable(client, trip_a, ids_a)
    _enable(client, trip_b, ids_b)
    with db_session.session_scope() as session:
        archived = session.get(ItineraryMain, trip_a)
        assert archived is not None
        archived.archived = True
        deleted = session.get(ItineraryMain, trip_b)
        assert deleted is not None
        deleted.deleted = 1
    assert itinerary_care_check.scan_due_checks() == 2
    assert care_run.calls == [], "归档/删除后零外呼（规格 §3.2）"
    data_archived = client.get(f"/api/itinerary/{trip_a}/care").json()["data"]
    assert data_archived["status"] == "finished" and data_archived["nextCheckAt"] is None
    deleted_row = _care_row(trip_b)  # 删除后的行程 GET 404（归属闸门），状态读持久行
    assert deleted_row.last_status == "finished" and deleted_row.next_check_at is None
    assert _chat_message_count() == 0


def test_care_scan_disabled_account_stops_without_calls(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    with db_session.session_scope() as session:
        user = session.get(SysUser, 1)
        assert user is not None
        user.status = 0
    assert itinerary_care_check.scan_due_checks() == 1
    row = _care_row(trip_id)  # 禁用账号读 API 一并 401（authenticate 拒绝），状态读持久行
    assert row.last_status == "account_disabled" and row.next_check_at is None
    assert care_run.calls == [] and _chat_message_count() == 0


def test_care_scan_skips_paused_task(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    client.put(f"/api/itinerary/{trip_id}/care", json={"enabled": False})
    assert itinerary_care_check.scan_due_checks() == 0, "暂停后不参与扫描"
    assert care_run.calls == [] and _chat_message_count() == 0


def test_care_prob_drift_does_not_renotify(care_model, care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    window_day = shanghai_today() + timedelta(days=3)
    care_run.rows = _day_rows(window_day, code=3, prob=70)
    assert itinerary_care_check.scan_due_checks() == 1  # 第一次：发布成功、键随发布落库
    _become_due(trip_id)
    care_run.rows = _day_rows(window_day, code=3, prob=71)
    assert itinerary_care_check.scan_due_checks() == 1, "概率漂移仍继续天气观测"
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "pending", "不重复通知，但未处理的原草稿仍可确认/忽略"
    assert data["lastResult"]["pendingMessageId"] is not None
    assert data["lastResult"]["days"][0]["risk"] is True, "风险照实记录，只是不再打扰"
    row = _care_row(trip_id)
    assert row.notified_risks_json == [f"{window_day.isoformat()}:rain"], "已处理键不重复写入"
    assert row.last_ai_attempt_on == shanghai_today(), "当日 AI 尝试仍是第一次那份"
    assert len(care_model.calls) == 1, "第二次检查不再进入模型流程"


def test_care_risk_without_watched_items_is_no_risk(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids[2:])  # 只监控第 2 天（窗口外）
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=80)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "no_risk"
    assert data["lastResult"]["affectedItemIds"] == []
    assert (_care_row(trip_id).notified_risks_json or []) == [], "无影响不记去重键"
    assert _care_row(trip_id).last_ai_attempt_on is None and _chat_message_count() == 0


# ---------- S3：后台建议的生成、发布与确认接缝（规格 §4.4 步骤 5–9、§4.5） ----------


def _publish_draft(
    care_model, care_run, client: TestClient, trip_id: int, watched: list[int], *, start_in_days: int = 2
) -> tuple[dict, ItineraryChatMessage, date]:
    """共用链路：开启照看 → 窗口内下雨 → 扫描一次 → 返回 (GET 数据, 消息行)。"""
    assert _enable(client, trip_id, watched).status_code == 200
    rain_day = shanghai_today() + timedelta(days=3)
    care_run.rows = _day_rows(rain_day, code=61, prob=85)
    care_run.rows += _day_rows(rain_day - timedelta(days=1))
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    with db_session.session_scope() as session:
        message_id = data["lastResult"]["pendingMessageId"]
        message = session.get(ItineraryChatMessage, message_id) if message_id else None
    assert message is not None, "发布成功必有 [行程照看] 消息"
    return data, message, rain_day


def test_care_publishes_full_draft_and_keeps_other_day_intact(care_model, care_run):
    """2 日行程只调第 2 日（受影响日）：第 1 日逐字段原样、不是只回传第 2 日。"""
    trip_id, item_ids = _make_trip(start_in_days=2)  # 两天都在 3 日窗内；雨下在第 2 天
    client = _client()
    data, message, rain_day = _publish_draft(care_model, care_run, client, trip_id, item_ids[2:])
    assert data["status"] == "pending", "发布成功且消息可确认 → pending（§4.3 派生）"
    assert message is not None and message.role == "ai" and message.changed == 1
    assert message.content.startswith("[行程照看] "), "一条 [行程照看] AI 消息（§4.4 步骤 8）"
    plans = itinerary_chat.read_json_list(message.plans_json)
    assert len(plans) == 2, "草稿必须包含完整天数——只回传一天会误删其他天（§4.5）"
    assert [plan["day_no"] for plan in plans] == [1, 2]
    # 未受影响日逐字段原样（草稿是 float 形状、库里是 Decimal 形状，按值比对）
    draft_day1 = plans[0]
    current_day1 = itinerary_chat.current_plans(trip_id)[0]
    assert draft_day1["note"] == current_day1["note"]
    assert [item["id"] for item in draft_day1["items"]] == [item["id"] for item in current_day1["items"]]
    assert all(
        str(item[key]) == str(current.get(key))
        for item, current in zip(draft_day1["items"], current_day1["items"], strict=False)
        for key in ("item_type", "poi_name", "start_time")
    ) and all(
        (float(item["cost"]) if item["cost"] is not None else None)
        == (float(current["cost"]) if current.get("cost") is not None else None)
        for item, current in zip(draft_day1["items"], current_day1["items"], strict=False)
    ), "第 1 日（未受影响日）的条目身份、类型、名称、时间与费用全部原样"
    # 受影响日：监控项被改时间
    draft_day2 = plans[1]
    retimed = [item for item in draft_day2["items"] if item["id"] in set(item_ids[2:])]
    assert retimed and all(item["start_time"] == "10:30" for item in retimed)
    # 草稿元数据：基准版本 + 修订号 + care 确认线程（服务端写入）
    assert plans[0]["_baseRevision"] == itinerary_chat.plan_revision(itinerary_chat.current_plans(trip_id))
    assert plans[0]["_basePlanningRevision"] == 0
    assert plans[0]["_confirmationThread"] == f"care-{trip_id}"
    row = _care_row(trip_id)
    assert row.notified_risks_json == [f"{rain_day.isoformat()}:rain"], "去重键只随发布成功落库"
    assert row.last_ai_attempt_on == shanghai_today() and row.check_token is None
    assert row.next_check_at is not None
    result = data["lastResult"]
    assert result["status"] == "pending" and result["reason"] and result["checkedAt"] is not None
    assert result["affectedItemIds"] == sorted(item_ids[2:])
    assert len(care_model.calls) == 1
    assert care_run.calls == [("杭州", (shanghai_today() + timedelta(days=2)).isoformat(), rain_day.isoformat())], (
        "显式上海口径窗口：明天起 3 日 ∩ 行程日期"
    )
    # 模型输入：天气仅作数据 + 受影响日期/活动 + 正式 plans + 已确认需求
    request = care_model.calls[0]
    assert [day.day_no for day in request.affected_days] == [2]
    assert {item.id for item in request.affected_days[0].items} == set(item_ids[2:])
    assert len(request.plans) == 2 and request.requirements is None
    # care 没碰共享 chat 线程：其上没有待确认 checkpoint
    from app.agent import confirm_thread as chat_thread_of
    from app.agent.editing.chat_draft.confirm_graph import care_confirm_thread
    from app.agent.runtime import checkpoint

    assert checkpoint.get_checkpointer().get(checkpoint.run_config(care_confirm_thread(trip_id))) is not None, (
        "提案落在 care 专属线程"
    )
    assert checkpoint.get_checkpointer().get(checkpoint.run_config(chat_thread_of(trip_id))) is None, (
        "后台生成不绑定 chat-{id}（§4.5）"
    )


def test_care_apply_preserves_daily_metadata_including_unaffected_day(care_model, care_run):
    import json

    trip_id, item_ids = _make_trip(start_in_days=2)
    metadata = {
        "theme": "西湖慢游",
        "miniRoute": ["断桥", "西泠桥"],
        "backupPlan": "雨天博物馆",
        "photoSpots": ["湖边"],
        "practicalNotes": ["带伞"],
    }
    with db_session.session_scope() as session:
        for day in session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id)):
            day.metadata_json = json.dumps(metadata, ensure_ascii=False)
    client = _client()
    _, message, _ = _publish_draft(care_model, care_run, client, trip_id, item_ids[2:3])
    assert all(plan["theme"] == metadata["theme"] for plan in care_model.calls[0].plans)
    itinerary_plan_apply.apply_plans(1, trip_id, message.id, itinerary_chat.action_base_revision(message))
    with db_session.session_scope() as session:
        days = session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id)).all()
        assert len(days) == 2
        for day in days:
            assert day.metadata_json is not None
            assert json.loads(day.metadata_json) == metadata


@pytest.mark.parametrize("weather", ["same_risk", "clear", "unavailable"])
def test_care_pending_survives_next_check_and_remains_dismissible(care_model, care_run, weather):
    trip_id, item_ids = _make_trip()
    client = _client()
    _, message, rain_day = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    if weather == "unavailable":
        care_run.rows = None
    else:
        care_run.rows = _day_rows(rain_day, code=61 if weather == "same_risk" else 0)
    _become_due(trip_id)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "pending"
    assert data["lastResult"]["pendingMessageId"] == message.id
    assert data["lastResult"]["status"] == ("unavailable" if weather == "unavailable" else "no_risk")
    assert len(care_model.calls) == 1 and _chat_message_count() == 1
    dismissed = client.post(f"/api/itinerary/{trip_id}/care/dismiss")
    assert dismissed.status_code == 200 and dismissed.json()["data"]["status"] == "applied"
    _become_due(trip_id)
    assert itinerary_care_check.scan_due_checks() == 1
    assert client.get(f"/api/itinerary/{trip_id}/care").json()["data"]["lastResult"]["pendingMessageId"] is None


def test_care_generation_binds_owner_scene_and_run_limits(care_model, care_run):
    """费用/身份上下文只在执行线程内绑定：owner 的 RunLimits + chat 场景 + guard 探针。"""
    from app.agent import current_limits
    from app.agent.runtime.observability import current_scene

    trip_id, item_ids = _make_trip()
    client = _client()
    observed: dict[str, object] = {}

    def _observe(data: CareAdjustInput) -> None:
        limits = current_limits()
        assert limits is not None
        observed["user_id"] = limits.user_id
        observed["itinerary_id"] = limits.itinerary_id
        observed["deadline_seconds"] = limits.deadline_seconds
        observed["max_llm_calls"] = limits.max_llm_calls
        observed["max_tokens_default"] = limits.max_tokens
        observed["guard_wired"] = limits.guard is not None
        observed["scene"] = current_scene()

    care_model.on_call = _observe
    _publish_draft(care_model, care_run, client, trip_id, item_ids)
    assert observed == {
        "user_id": 1,
        "itinerary_id": trip_id,
        "deadline_seconds": itinerary_care_publish.CARE_DEADLINE_SECONDS,
        "max_llm_calls": itinerary_care_publish.CARE_MAX_LLM_CALLS,
        "max_tokens_default": settings.max_token_budget,
        "guard_wired": True,
        "scene": "chat",
    }, "begin_limits 继承的是带 owner 的 care RunLimits，scene=chat（§4.4 步骤 5）"


def test_care_cost_events_settle_to_owner_ledger(care_model, care_run):
    """费用归属：预留/结算经持久 backend 记到 owner 的平台窗口账本行。"""
    from app.common import cost_policy as policy
    from app.db.models import CostLedger
    from app.services import cost_ledger

    cost_ledger.install_backend()

    def _uninstall() -> None:
        from app.agent import set_cost_backend

        set_cost_backend(None)

    trip_id, item_ids = _make_trip()
    client = _client()

    def _spend(data: CareAdjustInput) -> None:
        from app.agent import current_limits

        limits = current_limits()
        assert limits is not None
        reservation = limits.reserve_llm(100, 50)
        limits.settle_llm(reservation, {"prompt_tokens": 100, "completion_tokens": 50})

    care_model.on_call = _spend
    try:
        _publish_draft(care_model, care_run, client, trip_id, item_ids)
    finally:
        _uninstall()
    scope = policy.user_window(1, datetime.now(), policy.ATTR_PLATFORM)
    with cost_ledger.own_session() as session:
        row = session.get(CostLedger, scope.key)
    assert row is not None, "预留/结算真实落账（§4.4 步骤 5）"
    assert row.charge_prompt_tokens == 100 and row.charge_completion_tokens == 50
    assert row.charged_calls == 1 and row.attribution == policy.ATTR_PLATFORM
    assert row.open_reservations == 0, "预留恰好结算一次，不留悬挂占格"


def test_care_generation_carries_owner_byok_route_and_ledger(care_model, care_run):
    """BYOK（§7「平台额度不足 / BYOK」行）：后台建议腿在执行线程内携带 owner 的自带
    网关，费用按 byok 归属结算——不冒平台账（§4.4 步骤 5 的路由解析）。"""
    from app.common import cost_policy as policy
    from app.common import gateway_crypto
    from app.common.llm_route import LLMRoute, current_route
    from app.db.models import CostLedger, UserLlmGateway
    from app.services import cost_ledger

    with db_session.session_scope() as session:
        session.add(
            UserLlmGateway(
                user_id=1,
                name="自带网关",
                base_url="https://byok.example/v1",
                api_key_cipher=gateway_crypto.encrypt_secret("sk-care-byok"),
                api_key_hint="byok",
                model="care-byok-model",
                enabled=True,
            )
        )
    cost_ledger.install_backend()
    trip_id, item_ids = _make_trip()
    client = _client()
    seen: dict[str, LLMRoute | None] = {}

    def _observe(data: CareAdjustInput) -> None:
        from app.agent import current_limits

        limits = current_limits()
        assert limits is not None
        seen["route"] = current_route()
        reservation = limits.reserve_llm(40, 20)
        limits.settle_llm(reservation, {"prompt_tokens": 40, "completion_tokens": 20})

    care_model.on_call = _observe
    try:
        _publish_draft(care_model, care_run, client, trip_id, item_ids)
    finally:
        from app.agent import set_cost_backend

        set_cost_backend(None)
    route = seen["route"]
    assert route is not None and route.base_url == "https://byok.example/v1" and route.model == "care-byok-model", (
        "后台生成线程必须携带 owner 的自带网关路由"
    )
    scope = policy.user_window(1, datetime.now(), policy.ATTR_BYOK)
    with cost_ledger.own_session() as session:
        row = session.get(CostLedger, scope.key)
    assert row is not None and row.attribution == policy.ATTR_BYOK, "BYOK 腿按 byok 归属落账，不冒平台账"
    assert row.charge_prompt_tokens == 40 and row.open_reservations == 0


def test_care_byok_broken_cipher_fails_honestly_without_publish(care_model, care_run):
    """BYOK 密文损坏：路由解析就地失败 → care 如实 failed（错误非成功，§7），零模型零消息。"""
    from app.db.models import UserLlmGateway

    with db_session.session_scope() as session:
        session.add(
            UserLlmGateway(
                user_id=1,
                name="坏密文网关",
                base_url="https://byok.example/v1",
                api_key_cipher="not-a-fernet-token",
                api_key_hint="bad",
                model="m",
                enabled=True,
            )
        )
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "failed"
    assert data["lastResult"]["reason"] == itinerary_care_publish.ROUTE_BROKEN_REASON, "错误非成功（§7）"
    assert len(care_model.calls) == 0 and _chat_message_count() == 0, "路由坏了不进模型流程、不发布"
    assert (_care_row(trip_id).notified_risks_json or []) == [], "失败不得留去重键"


def test_care_pause_midflight_keeps_settled_costs(care_model, care_run):
    """§7「执行中暂停」行的完整闭环：后续外呼停止 + 结果不发布 + 已发生费用仍结算。"""
    from app.agent import TaskStopped, current_limits, set_cost_backend
    from app.common import cost_policy as policy
    from app.db.models import CostLedger
    from app.services import cost_ledger

    cost_ledger.install_backend()
    trip_id, item_ids = _make_trip()
    client = _client()

    def _spend_then_pause(data: CareAdjustInput) -> None:
        limits = current_limits()
        assert limits is not None
        reservation = limits.reserve_llm(60, 30)
        limits.settle_llm(reservation, {"prompt_tokens": 60, "completion_tokens": 30})
        with db_session.session_scope() as session:
            row = session.get(ItineraryCare, trip_id)
            assert row is not None
            row.enabled = False
            row.check_token = None
            row.checking_until = None
            row.next_check_at = None
            row.last_status = "disabled"
        try:
            limits.reserve_llm(1, 1)
        except TaskStopped:
            return  # 暂停后的下一次外呼被 guard 硬断
        raise AssertionError("暂停后 guard 必须阻止后续外呼")

    care_model.on_call = _spend_then_pause
    try:
        assert _enable(client, trip_id, item_ids).status_code == 200
        care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)
        assert itinerary_care_check.scan_due_checks() == 1
    finally:
        set_cost_backend(None)
    row = _care_row(trip_id)
    assert row.last_status == "disabled" and row.last_result_json is None, "暂停后结果不得发布（§3.2）"
    assert _chat_message_count() == 0
    scope = policy.user_window(1, datetime.now(), policy.ATTR_PLATFORM)
    with cost_ledger.own_session() as session:
        settled = session.get(CostLedger, scope.key)
    assert settled is not None and settled.charge_prompt_tokens == 60, "暂停不回滚已结算的费用（§3.2）"
    assert settled.charged_calls == 1 and settled.open_reservations == 0


def test_care_model_failure_publishes_nothing_and_keeps_no_keys(care_model, care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)
    care_model.behavior = "fail"  # 生成结论：复核未通过
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "failed", "模型/复核失败如实标失败，不写成功（§4.4 步骤 7）"
    assert data["lastResult"]["reason"] == "模型返回的方案没有通过复核"
    assert data["lastResult"]["pendingMessageId"] is None and _chat_message_count() == 0
    row = _care_row(trip_id)
    assert (row.notified_risks_json or []) == [], "失败不得留去重键（§4.4 步骤 8）"
    assert row.last_ai_attempt_on == shanghai_today(), "已占用的当日尝试不回退（失败不重烧）"


def test_care_discards_result_when_user_draft_appears_midflight(care_model, care_run):
    """生成期间用户产生新草稿：后台结果丢弃，前台内容不被顶掉（§4.5 前台优先）。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)

    def _seed_user_draft(data: CareAdjustInput) -> None:
        with db_session.session_scope() as session:
            session.add(
                ItineraryChatMessage(
                    itinerary_id=trip_id, user_id=1, role="ai", content="用户自己的方案", plans_json='[{"day_no":1}]'
                )
            )

    care_model.on_call = _seed_user_draft
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "waiting_for_user", "锁内看到任何前台待确认草稿就丢弃（§4.5）"
    assert data["lastResult"]["reason"] == "先处理当前草稿，本次自动建议未保存"
    with db_session.session_scope() as session:
        messages = session.scalars(select(ItineraryChatMessage)).all()
    assert [m.content for m in messages] == ["用户自己的方案"], "只新增过前台那一条，care 结果未发布"
    assert (_care_row(trip_id).notified_risks_json or []) == [] and _care_row(trip_id).last_result_json is not None


def test_care_discards_result_when_official_plans_change_midflight(care_model, care_run):
    """生成期间用户编辑正式行程：版本复核失败，结果作废且不覆盖用户编辑。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)

    def _mutate(data: CareAdjustInput) -> None:
        with db_session.session_scope() as session:
            item = session.get(ItineraryItem, item_ids[0])
            assert item is not None
            item.poi_name = "用户改过的景点"

    care_model.on_call = _mutate
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "failed"
    assert data["lastResult"]["reason"] == "行程已更新，本次建议作废"
    assert _chat_message_count() == 0, "过期结果不发布（§7）"
    with db_session.session_scope() as session:
        item = session.get(ItineraryItem, item_ids[0])
        assert item is not None and item.poi_name == "用户改过的景点", "前台编辑不被后台覆盖"


def test_care_guard_stops_outbound_when_paused_midflight(care_model, care_run):
    """执行中暂停：guard 探针让后续外呼抛 TaskStopped，结果不发布。"""
    from app.agent import TaskStopped, current_limits

    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)

    def _pause_then_probe(data: CareAdjustInput) -> None:
        with db_session.session_scope() as session:
            row = session.get(ItineraryCare, trip_id)
            assert row is not None
            row.enabled = False
            row.check_token = None
            row.checking_until = None
            row.next_check_at = None
            row.last_status = "disabled"
        limits = current_limits()
        assert limits is not None and limits.guard is not None
        try:
            limits.reserve_llm(10, 10)
        except TaskStopped:
            return  # 探针失权：暂停后的下一次外呼被硬断
        raise AssertionError("暂停后 guard 必须阻止后续外呼")

    care_model.on_call = _pause_then_probe
    assert itinerary_care_check.scan_due_checks() == 1
    row = _care_row(trip_id)
    assert row.last_status == "disabled" and row.last_result_json is None, "暂停后结果不得发布（§3.2）"
    assert (row.notified_risks_json or []) == [] and _chat_message_count() == 0


def test_care_account_disabled_midflight_stops_and_discards(care_model, care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)

    def _disable(data: CareAdjustInput) -> None:
        with db_session.session_scope() as session:
            user = session.get(SysUser, 1)
            assert user is not None
            user.status = 0

    care_model.on_call = _disable
    assert itinerary_care_check.scan_due_checks() == 1
    row = _care_row(trip_id)
    assert row.last_status != "suggested" and _chat_message_count() == 0, "禁用后结果不发布（§3.2）"


def test_care_archived_or_deleted_midflight_discards_without_publish(care_model, care_run):
    """§7「行程删除或归档」行专项：生成期间归档/软删 → 发布闸拒绝，结果不发布、
    不留消息、不留去重键；guard 探针对归档/删除的硬断与暂停共用同一分支。"""

    def _archive(trip_id: int):
        def _mutate(data: CareAdjustInput) -> None:
            with db_session.session_scope() as session:
                main = session.get(ItineraryMain, trip_id)
                assert main is not None
                main.archived = True

        return _mutate

    def _delete(trip_id: int):
        def _mutate(data: CareAdjustInput) -> None:
            with db_session.session_scope() as session:
                main = session.get(ItineraryMain, trip_id)
                assert main is not None
                main.deleted = 1

        return _mutate

    trips: list[tuple[int, list[int], object]] = []
    trip_a, ids_a = _make_trip()
    trips.append((trip_a, ids_a, _archive(trip_a)))
    trip_b, ids_b = _make_trip()
    trips.append((trip_b, ids_b, _delete(trip_b)))

    for trip_id, item_ids, mutate in trips:
        client = _client()
        assert _enable(client, trip_id, item_ids).status_code == 200
        care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)
        care_model.on_call = mutate
        assert itinerary_care_check.scan_due_checks() == 1
        row = _care_row(trip_id)
        assert row.last_status != "suggested" and row.last_result_json is None, "删除/归档后结果不发布（§3.2）"
        assert (row.notified_risks_json or []) == [], "不发布就不得留去重键（§4.4 步骤 8）"
    assert _chat_message_count() == 0, "两条腿都不新增任何消息"


def test_care_discards_result_when_watched_scope_changes_midflight(care_model, care_run):
    """生成期间用户只改监控范围（行程版本未动）：按旧范围生成的建议作废（§4.4 步骤 8
    的范围复核），作废也照常收尾——不悬挂令牌、不留去重键，下一窗口按新范围重判。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)

    def _reselect(data: CareAdjustInput) -> None:
        response = client.put(
            f"/api/itinerary/{trip_id}/care",
            json={"enabled": None, "watchedItemIds": [item_ids[-1]], "allowBackgroundAi": None},
        )
        assert response.status_code == 200

    care_model.on_call = _reselect
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "superseded"
    assert data["lastResult"]["reason"] == "关注范围已更新，本次建议作废"
    assert data["lastResult"]["pendingMessageId"] is None and _chat_message_count() == 0
    row = _care_row(trip_id)
    assert (row.notified_risks_json or []) == [], "作废不留键，重选范围后风险可重新提醒"
    assert row.check_token is None and row.next_check_at is not None, "作废完成收尾（步骤 9）"


def test_care_quota_gate_in_generation_phase(care_model, care_run, monkeypatch):
    """模型阶段的额度准入（quota_service.enforce_llm_budget）真实生效。"""
    calls: list[int] = []

    def _blocked(user_id: int) -> None:
        calls.append(user_id)
        raise ApiError(429, "今日 AI 生成次数已用完，请明天再试")

    monkeypatch.setattr(itinerary_care_publish.quota_service, "enforce_llm_budget", _blocked)
    trip_id, item_ids = _make_trip()
    client = _client()
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=85)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "quota_exhausted" and data["lastResult"]["reason"] == "未生成调整建议：额度不足"
    assert calls == [1], "准入以 owner 身份执行"
    assert len(care_model.calls) == 0 and _chat_message_count() == 0, "额度不足不进模型流程、不发布"


def test_care_apply_uses_care_thread_and_user_checkpoint_survives(care_model, care_run):
    """用户已有酒店确认 checkpoint：care 不覆盖它；care 草稿只能经 care 线程应用。"""
    from app.agent import confirm_thread as chat_thread_of
    from app.agent import resume_confirmation, run_confirmation
    from app.agent.editing.chat_draft.confirm_graph import care_confirm_thread
    from app.schemas.trip import HotelOption

    trip_id, item_ids = _make_trip()
    client = _client()
    hotel_checkpoint = ChatTurnResponse(
        reply="请选择酒店",
        hotel_options=[
            HotelOption(
                id="opt-1",
                hotel_name="柏悦",
                tier="comfort",
                base_price=300,
                season_factor=1.0,
                season_label="平季",
                nightly_price=300,
                nights=1,
                rooms=1,
                total_price=300,
                within_budget=True,
                reason="近地铁",
            )
        ],
        requires_confirmation=True,
        pending_action={"type": "replace_hotel", "requires_confirmation": True},
    )
    run_confirmation(hotel_checkpoint, thread=chat_thread_of(trip_id))  # 用户此前的酒店确认提案
    data, message, _rain = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    assert data["status"] == "pending"
    # care 草稿应用走服务端消息里的 care 线程，不碰 chat 线程上的 checkpoint
    detail = itinerary_plan_apply.apply_plans(1, trip_id, message.id, itinerary_chat.action_base_revision(message))
    assert detail["days"] == 2
    with db_session.session_scope() as session:
        consumed = session.get(ItineraryChatMessage, message.id)
        assert consumed is not None and consumed.plans_json == "[]", "应用即消费草稿"
    verdict = resume_confirmation(chat_thread_of(trip_id), {"action": "replace_hotel", "hotel_name": "柏悦"})
    assert verdict is not None and verdict.confirmed, "用户的酒店确认 checkpoint 未被 care 覆盖"
    # care 线程的服务端复核只认自己的 apply_plans 提案：拿酒店选择的动作去另一趟
    # 行程的 care 线程确认，必须被拒（确认值必须落在当时提案内）
    trip_b, ids_b = _make_trip()
    _data_b, _message_b, _rain_b = _publish_draft(care_model, care_run, client, trip_b, ids_b)
    mismatch = resume_confirmation(care_confirm_thread(trip_b), {"action": "replace_hotel", "hotel_name": "柏悦"})
    assert mismatch is not None and not mismatch.confirmed, "care 线程复核只认自己的提案动作"


def test_care_apply_rejects_repeat_cross_user_and_stale_revision(care_model, care_run):
    """重复应用被消费挡下、跨用户被归属拒绝、版本推进后被 CAS 拒绝（原语义零放宽）。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    data, message, _rain = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    base_revision = itinerary_chat.action_base_revision(message)
    with pytest.raises(ApiError) as cross:
        itinerary_plan_apply.apply_plans(2, trip_id, message.id, base_revision)
    assert cross.value.status == 404, "非所有者按行程不存在处理"
    itinerary_plan_apply.apply_plans(1, trip_id, message.id, base_revision)
    with pytest.raises(ApiError) as repeat:
        itinerary_plan_apply.apply_plans(1, trip_id, message.id, base_revision)
    assert repeat.value.status == 409, "草稿已消费，重复应用 409"
    # 版本推进后再应用另一趟行程的 care 草稿 → 基准不匹配（CAS）
    trip_b, ids_b = _make_trip()
    data_b, message_b, _rain_b = _publish_draft(care_model, care_run, client, trip_b, ids_b)
    with db_session.session_scope() as session:
        item = session.get(ItineraryItem, ids_b[0])
        assert item is not None
        item.poi_name = "行程被手动改过"
    with pytest.raises(ApiError) as stale:
        itinerary_plan_apply.apply_plans(1, trip_b, message_b.id, itinerary_chat.action_base_revision(message_b))
    assert stale.value.status == 409, "行程已变化 → 草稿失效（原 CAS 语义）"
    del data, data_b


def test_care_get_derives_pending_applied_and_stale(care_model, care_run):
    """GET care 按消息现状派生：pending → applied（确认）/ stale（被取代、清空聊天）。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    data, message, _rain = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    assert data["status"] == "pending" and data["lastResult"]["pendingMessageId"] == message.id

    # 被更新的草稿取代 → stale，且不再提供可点击的消息引用
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=trip_id, user_id=1, role="ai", content="更新的方案", plans_json='[{"day_no":1}]'
            )
        )
    superseded = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert superseded["status"] == "stale" and superseded["lastResult"]["pendingMessageId"] is None

    # 确认应用后 → applied
    trip_b, ids_b = _make_trip()
    _data_b, message_b, _rain_b = _publish_draft(care_model, care_run, client, trip_b, ids_b)
    itinerary_plan_apply.apply_plans(1, trip_b, message_b.id, itinerary_chat.action_base_revision(message_b))
    applied = client.get(f"/api/itinerary/{trip_b}/care").json()["data"]
    assert applied["status"] == "applied" and applied["lastResult"]["pendingMessageId"] is None

    # 清空聊天后 → stale（没有悬挂可应用方案，§4.5）
    trip_c, ids_c = _make_trip()
    data_c, _message_c, _rain_c = _publish_draft(care_model, care_run, client, trip_c, ids_c)
    assert data_c["status"] == "pending"
    itinerary_chat.clear_history(1, trip_c)
    cleared = client.get(f"/api/itinerary/{trip_c}/care").json()["data"]
    assert cleared["status"] == "stale" and cleared["lastResult"]["pendingMessageId"] is None, (
        "清空聊天后不得宣称消息可确认（§4.5）"
    )


def test_care_dismiss_consumes_care_draft_and_keeps_chat_draft(care_model, care_run):
    """忽略（§4.3）：仅消费本表 pendingMessageId 的 care 草稿，不清空普通聊天草稿。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    data, message, _rain = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    assert data["status"] == "pending"
    # 之后用户又产生一份普通聊天草稿（取代 care 草稿）
    with db_session.session_scope() as session:
        session.add(
            ItineraryChatMessage(
                itinerary_id=trip_id, user_id=1, role="ai", content="普通聊天草稿", plans_json='[{"day_no":1}]'
            )
        )
    response = client.post(f"/api/itinerary/{trip_id}/care/dismiss")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "applied", "忽略即消费：care 草稿不可再应用（§4.3）"
    with db_session.session_scope() as session:
        consumed = session.get(ItineraryChatMessage, message.id)
        assert consumed is not None and consumed.plans_json == "[]" and consumed.changed == 0
        others = [m for m in session.scalars(select(ItineraryChatMessage)).all() if m.id != message.id]
        assert others and all(itinerary_chat.read_json_list(m.plans_json) for m in others), (
            "普通聊天草稿不被忽略动作清空（§4.3）"
        )


def test_care_dismiss_idempotent_cross_user_and_without_pending(care_model, care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    mallory = _client("mallory")
    assert _enable(client, trip_id, item_ids).status_code == 200
    nothing = client.post(f"/api/itinerary/{trip_id}/care/dismiss")
    assert nothing.status_code == 200 and nothing.json()["data"]["lastResult"] is None, "无待忽略对象幂等返回"
    assert mallory.post(f"/api/itinerary/{trip_id}/care/dismiss").status_code == 404, "非所有者 404"
    _data, message, _rain = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    assert client.post(f"/api/itinerary/{trip_id}/care/dismiss").status_code == 200
    again = client.post(f"/api/itinerary/{trip_id}/care/dismiss")
    assert again.status_code == 200 and again.json()["data"]["status"] == "applied", "重复忽略幂等"
    with db_session.session_scope() as session:
        consumed = session.get(ItineraryChatMessage, message.id)
        assert consumed is not None and consumed.plans_json == "[]"


def test_care_dismiss_works_after_care_disabled(care_model, care_run):
    """关闭照看后已呈现的草稿仍可忽略（§4.3「保留已经呈现的草稿供查看或忽略」）。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    _data, _message, _rain = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    assert client.put(f"/api/itinerary/{trip_id}/care", json={"enabled": False}).status_code == 200
    response = client.post(f"/api/itinerary/{trip_id}/care/dismiss")
    assert response.status_code == 200 and response.json()["data"]["status"] == "applied"


def test_care_dismissed_risk_does_not_renotify(care_model, care_run):
    """已忽略的风险不自动重复（§3.3/§7「已忽略」行）：忽略只消费草稿、去重键保留，
    下轮扫描继续天气观测但不烧模型、不出新消息。"""
    trip_id, item_ids = _make_trip()
    client = _client()
    _data, _message, _rain = _publish_draft(care_model, care_run, client, trip_id, item_ids)
    assert client.post(f"/api/itinerary/{trip_id}/care/dismiss").status_code == 200
    keys_before = _care_row(trip_id).notified_risks_json
    _become_due(trip_id)
    assert itinerary_care_check.scan_due_checks() == 1, "扫描照常进行，天气观测不省略"
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "no_risk"
    assert data["lastResult"]["reason"] == "这些风险已经提醒过，出现新变化会再次提醒"
    assert data["lastResult"]["days"][0]["risk"] is True, "风险照实记录，只是不再打扰"
    assert len(care_model.calls) == 1 and _chat_message_count() == 1, "忽略后不重复通知、不重复烧模型"
    assert _care_row(trip_id).notified_risks_json == keys_before, "忽略不清去重键（用户忽略也不自动重复）"


def test_care_scan_waits_for_pending_user_draft(care_run):
    trip_id, item_ids = _make_trip()
    with db_session.session_scope() as session:
        draft = ItineraryChatMessage(
            itinerary_id=trip_id, user_id=1, role="ai", content="用户待确认的方案", plans_json='[{"day_no":1}]'
        )
        session.add(draft)
        session.flush()
        draft_id, draft_plans = draft.id, draft.plans_json
    client = _client()
    _enable(client, trip_id, item_ids)
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=90)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "waiting_for_user", "有前台待确认草稿不发起后台模型调用（规格 §3.3）"
    row = _care_row(trip_id)
    assert (row.notified_risks_json or []) == [] and row.last_ai_attempt_on is None, "未占用尝试也未记去重键"
    with db_session.session_scope() as session:
        untouched = session.get(ItineraryChatMessage, draft_id)
        assert untouched is not None and untouched.plans_json == draft_plans, "前台草稿不被顶掉"
    assert len(care_run.calls) == 1 and _chat_message_count() == 1, "天气照查、草稿只增这一条前台消息"


def test_care_quota_exhausted_stops_before_attempt(care_run, monkeypatch):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=80)

    def _blocked(user_id: int) -> None:
        raise ApiError(429, "平台费用窗口已达上限")

    monkeypatch.setattr(itinerary_care_risk.cost_admission, "enforce_windows", _blocked)
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "quota_exhausted"
    assert data["lastResult"]["reason"] == "未生成调整建议：额度不足"
    row = _care_row(trip_id)
    assert row.last_ai_attempt_on is None and (row.notified_risks_json or []) == [], "额度闸不占尝试、不留键"


def test_care_daily_attempt_limit_defers_within_same_day(care_model, care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=80)
    care_model.behavior = "raise"  # 模型失败：当日不重烧，也不留键
    assert itinerary_care_check.scan_due_checks() == 1  # 第一次：失败、零消息
    _become_due(trip_id)
    care_model.behavior = "succeed"
    assert itinerary_care_check.scan_due_checks() == 1
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "deferred", "失败/崩溃不在当日自动重烧（规格 §3.3）"
    assert _care_row(trip_id).last_ai_attempt_on == shanghai_today()
    assert _chat_message_count() == 0 and len(care_model.calls) == 1, "第二次不再进入模型流程"


def test_care_cron_and_manual_check_race_only_one_executes(care_model, care_run, monkeypatch):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    submissions: list[tuple[object, ...]] = []

    def _submit(task: Callable[..., object], *args: object) -> Future[object]:
        submissions.append(args)
        return cast("Future[object]", task(*args))

    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", _submit)

    def _racing(start_date: str | None, end_date: str | None) -> list[dict[str, object]] | None:
        # cron 持锁做天气查询时，「立即检查」到达：应看到 checking 且不再提交第二份
        response = client.post(f"/api/itinerary/{trip_id}/care/check")
        assert response.status_code == 202
        assert response.json()["data"]["status"] == "checking"
        assert submissions == [], "正在检查返回当前状态，不创建第二份（规格 §4.3）"
        return _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=80)

    care_run.dynamic = _racing
    assert itinerary_care_check.scan_due_checks() == 1
    assert len(care_run.calls) == 1, "并发只赢一次"
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "pending" and _care_row(trip_id).check_token is None


def test_care_manual_check_busy_releases_claim(care_db, monkeypatch):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)

    def _full(task: Callable[..., object], *args: object) -> Future[object]:
        raise itinerary_chat.TaskRejected("travel-chat queue full")

    monkeypatch.setattr(itinerary_chat.chat_pool, "submit", _full)
    response = client.post(f"/api/itinerary/{trip_id}/care/check")
    assert response.status_code == 429, "满载返回现有繁忙错误（规格 §3.2）"
    assert response.json()["message"] == itinerary_chat.BUSY_MESSAGE
    row = _care_row(trip_id)
    assert row.check_token is None and row.checking_until is None, "满载必须释放本次争抢"


def test_care_manual_check_endpoint_contract(care_run, _sync_pool):
    trip_id, item_ids = _make_trip()
    client = _client()
    mallory = _client("mallory")
    assert client.post(f"/api/itinerary/{trip_id}/care/check").status_code == 400, "未开启照看先 400"
    assert mallory.post(f"/api/itinerary/{trip_id}/care/check").status_code == 404, "非所有者 404"
    assert _enable(client, trip_id, item_ids).status_code == 200
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3))
    response = client.post(f"/api/itinerary/{trip_id}/care/check")
    assert response.status_code == 202, "POST /check 返回 202（规格 §4.3）"
    data = response.json()["data"]
    assert data["status"] == "no_risk" and len(_sync_pool) == 1, "同步执行后返回当前状态"
    assert len(care_run.calls) == 1


def test_care_manual_check_404_when_globally_disabled(care_db, monkeypatch):
    monkeypatch.setattr(settings, "care_enabled", False)
    trip_id, _item_ids = _make_trip()
    client = _client()
    assert client.post(f"/api/itinerary/{trip_id}/care/check").status_code == 404, "全局关闭按功能不存在处理"


def test_care_expired_token_reclaimed_after_crash(care_model, care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=80)
    with db_session.session_scope() as session:
        row = session.get(ItineraryCare, trip_id)
        assert row is not None
        row.check_token = "crashed-worker"
        row.checking_until = datetime.now() - timedelta(seconds=1)  # 崩溃残留的过期令牌
    assert itinerary_care_check.scan_due_checks() == 1, "过期 token 可重领，无需恢复中途推理（规格 §4.4 步骤 9）"
    data = client.get(f"/api/itinerary/{trip_id}/care").json()["data"]
    assert data["status"] == "pending" and _care_row(trip_id).check_token is None


def test_care_live_foreign_token_blocks_second_claim(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    with db_session.session_scope() as session:
        row = session.get(ItineraryCare, trip_id)
        assert row is not None
        row.check_token = "other-worker"
        row.checking_until = datetime.now() + timedelta(minutes=5)
    assert itinerary_care_check.scan_due_checks() == 0, "未过期令牌在他手：本次扫描不得重复执行"
    assert care_run.calls == []


def test_care_paused_mid_check_publishes_nothing(care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)

    def _pause_then_report(start_date: str | None, end_date: str | None) -> list[dict[str, object]] | None:
        # 外呼已开始后用户暂停（清令牌，同 update_care 的关闭路径）
        with db_session.session_scope() as session:
            row = session.get(ItineraryCare, trip_id)
            assert row is not None
            row.enabled = False
            row.check_token = None
            row.checking_until = None
            row.next_check_at = None
            row.last_status = "disabled"
        return _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=80)

    care_run.dynamic = _pause_then_report
    assert itinerary_care_check.scan_due_checks() == 1, "已开始的外呼照常完成（供应商侧不可撤回）"
    row = _care_row(trip_id)
    assert row.last_status == "disabled" and row.last_result_json is None, "暂停后结果不得发布（规格 §3.2）"
    assert (row.notified_risks_json or []) == [] and row.last_ai_attempt_on is None, "不为不会发布的流程占名额"
    assert _chat_message_count() == 0


def test_care_state_readable_after_restart_and_not_rechecked_early(care_model, care_run):
    trip_id, item_ids = _make_trip()
    client = _client()
    _enable(client, trip_id, item_ids)
    care_run.rows = _day_rows(shanghai_today() + timedelta(days=3), code=61, prob=80)
    assert itinerary_care_check.scan_due_checks() == 1
    fresh = _client().get(f"/api/itinerary/{trip_id}/care").json()["data"]  # 全新客户端=重启后的读路径
    assert fresh["enabled"] is True and fresh["status"] == "pending", "发布成功的草稿按消息现状派生 pending"
    assert fresh["lastResult"]["affectedItemIds"] and fresh["nextCheckAt"] is not None
    assert fresh["lastResult"]["pendingMessageId"] is not None
    assert itinerary_care_check.scan_due_checks() == 0, "next_check_at 未到不重复检查"
    assert len(care_run.calls) == 1
    row = _care_row(trip_id)
    assert row.notified_risks_json == [f"{(shanghai_today() + timedelta(days=3)).isoformat()}:rain"]
    assert row.last_ai_attempt_on == shanghai_today()


# ---------- S3：确定性复核（§4.5 草稿保护规则，weather_care 单元面） ----------


def _care_plans() -> list[dict]:
    return [
        {
            "day_no": 1,
            "note": "第一天",
            "items": [
                {
                    "id": 11,
                    "item_type": "hotel",
                    "poi_name": "西湖酒店",
                    "cost": 300.0,
                    "start_time": None,
                    "end_time": None,
                },
                {
                    "id": 12,
                    "item_type": "attraction",
                    "poi_name": "断桥",
                    "cost": 0.0,
                    "start_time": "09:00",
                    "end_time": "11:00",
                    "duration_min": 120,
                },
                {
                    "id": 13,
                    "item_type": "transport",
                    "poi_name": "地铁1号线",
                    "cost": 5.0,
                    "start_time": "11:30",
                    "end_time": "12:00",
                    "duration_min": 30,
                },
                {
                    "id": 14,
                    "item_type": "attraction",
                    "poi_name": "雷峰塔",
                    "cost": 40.0,
                    "start_time": "13:00",
                    "end_time": "15:00",
                    "duration_min": 120,
                },
            ],
        },
        {
            "day_no": 2,
            "note": "第二天",
            "items": [
                {
                    "id": 21,
                    "item_type": "attraction",
                    "poi_name": "灵隐寺",
                    "cost": 30.0,
                    "start_time": "09:00",
                    "end_time": "12:00",
                    "duration_min": 180,
                }
            ],
        },
    ]


def _care_input(plans: list[dict], *, day_no: int = 1, watched: tuple[int, ...] = (12,), requirements=None):
    day = CareAffectedDay(
        day_no=day_no,
        date="2026-10-20",
        condition="雨",
        precip_prob=85.0,
        weather_code=61,
        items=tuple(CareWatchedItem(id=item_id, name="断桥") for item_id in watched),
    )
    return CareAdjustInput(
        city="杭州", plans=plans, requirements=requirements, affected_days=[day], persons=1, budget=1000.0
    )


def _respond(plans: list[dict], *, changed: bool = True, reply: str = "已按雨天调整") -> ChatTurnResponse:
    return ChatTurnResponse(reply=reply, plans=plans, changed=changed, requires_confirmation=changed)


def _respond_copy(plans: list[dict], *, changed: bool = True, reply: str = "已按雨天调整") -> ChatTurnResponse:
    """浅拷贝每天/每条（draft 与基线不共享可变对象，模拟模型真实回显）。"""
    return _respond(
        [{"day_no": d["day_no"], "note": d["note"], "items": [dict(i) for i in d["items"]]} for d in plans],
        changed=changed,
        reply=reply,
    )


@dataclass
class _CareLlmStub:
    """weather_care 的 run_chat_turn 替身：记录请求、回放预设响应。"""

    response: ChatTurnResponse | None = None
    calls: list[ChatTurnRequest] = dataclass_field(default_factory=list)

    def __call__(self, request: ChatTurnRequest, *, confirmation_thread: str | None = None) -> ChatTurnResponse:
        assert confirmation_thread is None, "后台生成不绑共享确认线程（§4.5）"
        self.calls.append(request)
        assert self.response is not None, "测试未设置响应"
        return self.response


@pytest.fixture
def care_llm(monkeypatch):
    """weather_care 的 run_chat_turn 桩：记录请求、返回预设响应。"""
    from app.agent.editing import weather_care

    stub = _CareLlmStub()
    monkeypatch.setattr(weather_care, "run_chat_turn", stub)
    return stub


def _run_care(llm: _CareLlmStub, data: CareAdjustInput):
    """默认响应 = 原样回显基线（除非测试显式设置了 response——那是待复核的草稿）。"""
    from app.agent.editing import weather_care

    if llm.response is None:
        llm.response = _respond_copy(data.plans)
    return weather_care.run_weather_care_adjustment(data)


def test_care_review_keeps_unaffected_day_and_passes_valid_adjustment(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans)
    draft[0]["items"][1]["start_time"] = "08:30"  # 监控项（断桥）调时间，避开 11:30 的交通接驳
    draft[0]["items"][1]["end_time"] = "10:30"
    draft[0]["items"][3]["start_time"] = "13:30"  # 非监控活动只调时间（满足转场）
    draft[0]["items"][3]["end_time"] = "15:30"
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert result.ok and len(result.plans) == 2, "合法的受影响日调整应通过复核"
    assert result.plans[1] == plans[1], "未受影响日逐字段原样（§4.5）"
    by_id = {item["id"]: item for item in result.plans[0]["items"]}
    assert by_id[11] == plans[0]["items"][0], "酒店身份/价格/时间全部保留"
    assert by_id[13] == plans[0]["items"][2], "交通身份/价格/时间全部保留"
    assert by_id[14]["start_time"] == "13:30" and by_id[14]["cost"] == 40.0, "非监控活动只动了时间"
    assert result.response is not None, "发布侧需要原始提案绑确认线程"


@pytest.mark.parametrize("mutate", [False, True])
def test_care_review_preserves_omitted_metadata_and_rejects_unaffected_changes(care_llm, mutate):
    plans = _care_plans()
    for plan in plans:
        plan.update(theme="原主题", practicalNotes=["原提示"], miniRoute=["原路线"])
    draft = _respond_copy(plans).plans  # 模拟紧凑输出省略元数据。
    draft[0]["items"][1].update(start_time="08:30", end_time="10:30")
    if mutate:
        draft[1]["theme"] = "擅自重写其他日"
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    if mutate:
        assert not result.ok
    else:
        assert result.ok and result.plans[1] == plans[1]
        assert result.plans[0]["practicalNotes"] == ["原提示"]
        assert result.response is not None and result.response.plans == result.plans


@pytest.mark.parametrize(
    ("baseline_end", "draft_end", "accepted"),
    [("09:30", "10:59", False), ("09:30", "09:31", True), ("10:50", "10:49", True), ("10:50", "10:59", False)],
)
def test_care_review_checks_transfer_gaps_without_rejecting_legacy_improvements(
    care_llm, baseline_end, draft_end, accepted
):
    plans = [
        {
            "day_no": 1,
            "note": "转场测试",
            "items": [
                {
                    "id": 12,
                    "item_type": "attraction",
                    "poi_name": "公园",
                    "start_time": "09:00",
                    "end_time": baseline_end,
                    "latitude": 30.0,
                    "longitude": 120.0,
                },
                {
                    "id": 14,
                    "item_type": "food",
                    "poi_name": "午餐",
                    "start_time": "11:00",
                    "end_time": "11:30",
                    "latitude": 30.02,
                    "longitude": 120.02,
                },
            ],
        }
    ]
    draft = deepcopy(plans)
    draft[0]["items"][0]["end_time"] = draft_end
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert result.ok is accepted
    if not accepted:
        assert result.reason is not None
        assert "转场时间不足" in result.reason


def test_care_review_rejects_single_day_reply(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans[:1])  # 只回传第 1 天
    draft[0]["items"][1]["start_time"] = "10:00"
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok and result.plans == []
    assert result.reason is not None and "天数" in result.reason, "只回传一天会误删其他天（§4.5 点名回归）"


def test_care_review_rejects_drift_on_unaffected_day(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans)
    draft[1]["items"][0]["poi_name"] = "飞来峰"  # 第 2 天不在受影响范围却被改动
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok and result.reason is not None and "第 2 天" in result.reason


def test_care_review_rejects_hotel_and_transport_changes(care_llm):
    from copy import deepcopy

    mutations = (
        lambda d: d[0]["items"][0].__setitem__("cost", 350.0),  # 酒店价格
        lambda d: d[0]["items"][2].__setitem__("start_time", "12:30"),  # 交通时间
    )
    for mutate in mutations:
        plans = _care_plans()
        draft = deepcopy(plans)
        mutate(draft)
        care_llm.response = _respond(draft)
        result = _run_care(care_llm, _care_input(plans))
        assert not result.ok and result.reason is not None and "酒店或交通" in result.reason, (
            "酒店/交通身份、价格、日期、时间全部保留（§4.5）"
        )


def test_care_review_rejects_removing_non_watched(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans)
    draft[0]["items"] = [item for item in draft[0]["items"] if item["id"] != 14]  # 雷峰塔未被关注却被删
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok and result.reason is not None and "雷峰塔" in result.reason


def test_care_review_allows_watched_replacement_with_indoor_attraction(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans)
    draft[0]["items"] = [item for item in draft[0]["items"] if item["id"] != 12]  # 受监控的户外景点被移除
    draft[0]["items"].append(
        {"item_type": "attraction", "poi_name": "浙江省博物馆", "start_time": "09:00", "end_time": "11:00"}
    )
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert result.ok, "受影响日的监控项可换成室内替代景点"


def test_care_review_rejects_fabricated_id_non_attraction(care_llm):
    """带幻觉/跨日 id 的凭空非景点条目必须拒绝：否则要么 apply 身份校验 400 变死端
    草稿（永远无法应用还占住 pending），要么撞软删行被静默按新条目落库。"""
    from copy import deepcopy

    fabrications = (
        {"id": 999, "item_type": "food", "poi_name": "凭空餐厅", "start_time": "12:00", "end_time": "13:00"},
        {"id": 21, "item_type": "food", "poi_name": "别日餐厅", "start_time": "12:00", "end_time": "13:00"},
    )
    for fabricated in fabrications:
        plans = _care_plans()
        draft = deepcopy(plans)
        draft[0]["items"].append(dict(fabricated))
        care_llm.response = _respond(draft)
        result = _run_care(care_llm, _care_input(plans))
        assert not result.ok and result.reason is not None and "凭空新增" in result.reason, (
            f"带 id 的凭空非景点条目必须拒绝：{fabricated}"
        )


def test_care_review_allows_watched_item_moved_between_affected_days(care_llm):
    """两个受影响日之间移动监控项（保留 id、类型仍景点）是合法的已选景点调整。"""
    from copy import deepcopy

    plans = _care_plans()
    day1 = CareAffectedDay(
        day_no=1, date="2026-10-20", condition="雨", precip_prob=90.0, items=(CareWatchedItem(id=12, name="断桥"),)
    )
    day2 = CareAffectedDay(
        day_no=2, date="2026-10-21", condition="雨", precip_prob=88.0, items=(CareWatchedItem(id=21, name="灵隐寺"),)
    )
    data = CareAdjustInput(city="杭州", plans=plans, requirements=None, affected_days=[day1, day2], persons=1)
    draft = deepcopy(plans)
    item12 = next(item for item in draft[0]["items"] if item["id"] == 12)
    draft[0]["items"].remove(item12)
    item12["start_time"], item12["end_time"] = "13:00", "15:00"  # 挪到第 2 天下午避让灵隐寺
    draft[1]["items"].append(item12)
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, data)
    assert result.ok, "监控项在受影响日之间移动应放行（§4.5 只调整已选景点）"
    assert {item["id"] for item in result.plans[1]["items"]} == {21, 12}


def test_care_review_rejects_new_non_attraction(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans)
    draft[0]["items"].append({"item_type": "food", "poi_name": "新餐厅", "start_time": "12:00", "end_time": "13:00"})
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok and result.reason is not None and "非景点" in result.reason


def test_care_review_rejects_requirements_patch(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans)
    draft[0]["items"][1]["start_time"] = "10:00"
    draft[0]["_proposedRequirements"] = {"required_places": []}
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok and result.reason is not None and "需求" in result.reason, "不修改正式 requirements（§4.5）"


def test_care_review_rejects_no_change(care_llm):
    plans = _care_plans()
    care_llm.response = _respond_copy(plans)
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok and result.reason is not None and "没有实际改变" in result.reason, "不生成空的「已调整」消息"


def test_care_review_rejects_novel_time_conflict(care_llm):
    from copy import deepcopy

    plans = _care_plans()
    draft = deepcopy(plans)
    draft[0]["items"][1]["start_time"] = "13:30"  # 与雷峰塔 13:00-15:00 重叠
    draft[0]["items"][1]["end_time"] = "15:30"
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok and result.reason is not None and "重叠" in result.reason


def test_care_review_rejects_missing_required_place(care_llm):
    from app.schemas.trip_requirements import RequiredPlace, TripRequirements

    requirements = TripRequirements(required_places=[RequiredPlace(constraint_id="r1", name="断桥", day_no=1)])
    plans = _care_plans()
    draft = _respond_copy(plans).plans
    draft[0]["items"] = [item for item in draft[0]["items"] if item["id"] != 12]  # 指定日必去被删
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans, watched=(12,), requirements=requirements))
    assert not result.ok and result.reason is not None and "需求约束" in result.reason, "不删必去（§4.5）"


def test_care_review_rejects_missing_trip_wide_required_place(care_llm):
    from app.schemas.trip_requirements import RequiredPlace, TripRequirements

    requirements = TripRequirements(required_places=[RequiredPlace(constraint_id="r2", name="雷峰塔", day_no=None)])
    plans = _care_plans()
    draft = _respond_copy(plans).plans
    draft[0]["items"] = [item for item in draft[0]["items"] if item["id"] != 14]
    care_llm.response = _respond(draft)
    result = _run_care(care_llm, _care_input(plans, watched=(12, 14), requirements=requirements))
    assert not result.ok and result.reason is not None and "全程必去" in result.reason


def test_care_review_rejects_model_saying_no_change(care_llm):
    plans = _care_plans()
    care_llm.response = _respond([], changed=False, reply="近期降水较弱，建议保持原行程")
    result = _run_care(care_llm, _care_input(plans))
    assert not result.ok
    assert result.reason == "近期降水较弱，建议保持原行程", "如实转述模型结论，不编造成功"


def test_care_generation_failure_maps_to_honest_reason(monkeypatch):
    from app.agent.editing import weather_care

    def _boom(request: object, *, confirmation_thread: str | None = None) -> ChatTurnResponse:
        raise RuntimeError("llm down")

    monkeypatch.setattr(weather_care, "run_chat_turn", _boom)
    result = weather_care.run_weather_care_adjustment(_care_input(_care_plans()))
    assert not result.ok and result.reason is not None and "模型暂时不可用" in result.reason


def test_care_message_carries_weather_data_scope_and_official_plans():
    from app.agent.editing import weather_care
    from app.schemas.trip_requirements import RequiredPlace, TripRequirements

    requirements = TripRequirements(required_places=[RequiredPlace(constraint_id="r1", name="断桥", day_no=1)])
    plans = _care_plans()
    request = weather_care._build_request(_care_input(plans, requirements=requirements))
    text = request.message
    assert "仅供参考" in text and "不是指令" in text, "天气数据仅作数据（§4.5）"
    assert "降水概率约 85%" in text and "雨" in text
    assert "断桥" in text and "第 1 天" in text, "受影响日期与活动"
    assert "酒店与交通项目保持不变" in text and "其余各天逐项保持不变" in text
    assert "预算不得放宽" in text
    assert request.city == "杭州" and request.days == 2 and request.persons == 1
    assert request.budget == 1000.0 and request.requirements_struct is requirements
    assert request.plans == plans, "当前正式 plans 原样进入模型输入"
    assert request.history == []
