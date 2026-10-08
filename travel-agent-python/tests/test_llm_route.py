"""LLM 路由上下文（BYOK）单测：contextvar 语义、get_llm_client 路由分发、
业务腿注入探针（把「contextvars 不跨线程池、必须在 worker 入口进入」钉进测试）。

全部 mock：LLMClient 构造不发网络；恢复腿按 tests/test_checkpoint_resume.py 的
僵尸行程种子模式打桩 resume_day/submit_planning。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

import app.agent  # noqa: F401  先完成 agent 门面导入：存量导入环 llm_client→facade→web_search→llm_client（同 test_llm_backoff.py 的口径）。
from app.common import llm_client
from app.common.envelope import FAIL_TRIP_NOTE
from app.common.llm_route import LLMRoute, current_route, use_route
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryMain
from app.schemas.trip_requirements import IntakeState
from app.services import (
    generation_recovery,
    itinerary_city,
    itinerary_enricher,
    itinerary_generation,
    llm_gateway_service,
    poi_search,
)

SENTINEL = LLMRoute(base_url="https://byok.example/v1", api_key="sk-probe-key-9", model="probe-model", label="probe")

# autouse 夹具会把 resolve_route 桩成 None；fail-open 用例需要真实现，这里先存原件
_REAL_RESOLVE_ROUTE = llm_gateway_service.resolve_route


@pytest.fixture(autouse=True)
def _clean_route_state(monkeypatch):
    """路由上下文与 client 缓存逐用例归零；resolve_route 默认无网关（不打 DB）。"""
    monkeypatch.setattr(llm_gateway_service, "resolve_route", lambda _uid: None)
    llm_client._route_clients.clear()
    assert current_route() is None
    yield
    llm_client._route_clients.clear()
    assert current_route() is None, "use_route 退出后必须还原（泄漏会串味后续用例）"


# ---------- contextvar 语义 ----------


def test_route_repr_hides_api_key() -> None:
    """结构性红线：dataclass 自动 __repr__ 不得携带明文密钥（一次误用即破线）。"""
    text = repr(SENTINEL)
    assert SENTINEL.api_key not in text
    assert "sk-probe" not in text
    assert SENTINEL.base_url in text and SENTINEL.model in text, "非密钥字段仍可读（排障需要）"


def test_use_route_sets_and_restores() -> None:
    assert current_route() is None
    with use_route(SENTINEL):
        assert current_route() is SENTINEL
    assert current_route() is None


def test_use_route_none_is_explicit_and_nested_restores() -> None:
    with use_route(SENTINEL):
        with use_route(None):
            assert current_route() is None, "显式压 None = 子树强制默认通道"
        assert current_route() is SENTINEL
    assert current_route() is None


def test_route_var_does_not_leak_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with use_route(SENTINEL):
            raise RuntimeError("boom")
    assert current_route() is None


# ---------- get_llm_client 路由分发 ----------


def test_no_route_returns_same_default_singleton() -> None:
    assert llm_client.get_llm_client() is llm_client.get_llm_client()


def test_route_returns_dedicated_client_with_cache_hit() -> None:
    with use_route(SENTINEL):
        first = llm_client.get_llm_client()
        second = llm_client.get_llm_client()
    assert first is second, "同路由应命中缓存（同一实例）"
    assert first is not llm_client.get_llm_client(), "出上下文后回到默认单例"
    assert first._base_url == SENTINEL.base_url and first._model == SENTINEL.model
    assert first._api_key == SENTINEL.api_key


def test_different_keys_get_different_clients() -> None:
    other = LLMRoute(base_url=SENTINEL.base_url, api_key="sk-other-key-3", model=SENTINEL.model)
    with use_route(SENTINEL):
        mine = llm_client.get_llm_client()
    with use_route(other):
        theirs = llm_client.get_llm_client()
    assert mine is not theirs


def test_route_client_cache_clears_at_capacity() -> None:
    for index in range(llm_client._ROUTE_CLIENT_CAPACITY + 1):
        with use_route(LLMRoute("https://gw.example", f"sk-key-{index:03d}", "m")):
            client = llm_client.get_llm_client()
    assert len(llm_client._route_clients) <= llm_client._ROUTE_CLIENT_CAPACITY
    assert client is not None


# ---------- resolve_route fail-open ----------


def test_resolve_route_fail_open_on_db_error(monkeypatch, caplog) -> None:
    """网关表不可读：resolve_route 自己的 try/except 兜住（fail-open），只 warning。"""
    from contextlib import contextmanager as cm

    @cm
    def _broken_session():
        raise RuntimeError("gateway table unavailable")
        yield  # pragma: no cover

    monkeypatch.setattr(llm_gateway_service, "session_scope", _broken_session)
    monkeypatch.setattr(llm_gateway_service, "resolve_route", _REAL_RESOLVE_ROUTE)
    with caplog.at_level("WARNING", logger="app.services.llm_gateway_service"):
        with llm_gateway_service.route_scope(7):
            assert current_route() is None
            assert llm_client.get_llm_client() is llm_client.get_llm_client()
    assert "falling back" in caplog.text


# ---------- 业务腿注入探针（迁移面契约） ----------


def _fake_clarify_response() -> SimpleNamespace:
    # M1a：clarify 响应恒带权威 state（服务端序列化走 state.model_dump）
    return SimpleNamespace(
        state=IntakeState(),
        slots={},
        missing=["days"],
        question="几天？",
        ready=False,
        options=[],
        blocked=False,
        degraded=False,
    )


def test_clarify_leg_carries_route(monkeypatch) -> None:
    seen: list[LLMRoute | None] = []
    monkeypatch.setattr(llm_gateway_service, "resolve_route", lambda uid: SENTINEL if uid == 7 else None)
    monkeypatch.setattr(itinerary_city, "run_clarify", lambda _req: (_seen_append(seen), _fake_clarify_response())[1])
    itinerary_city.clarify(7, "想去杭州", {})
    assert seen == [SENTINEL], "clarify 腿必须在 agent 调用期间携带用户路由"
    # 出口还原
    assert current_route() is None


def _seen_append(seen: list) -> None:
    seen.append(current_route())


def test_poi_search_leg_carries_route(monkeypatch) -> None:
    seen: list[LLMRoute | None] = []
    monkeypatch.setattr(llm_gateway_service, "resolve_route", lambda uid: SENTINEL if uid == 7 else None)
    monkeypatch.setattr(poi_search, "workbench_search", lambda city, **_kw: (_seen_append(seen), [])[1])
    monkeypatch.setattr(itinerary_city, "supported_cities", lambda: ["杭州"])
    poi_search.search_local(7, "杭州")
    assert seen == [SENTINEL], "search_local 的联网补池腿必须携带用户路由"


@pytest.fixture
def sqlite_env(monkeypatch, tmp_path):
    from app.common import cache_store
    from app.services import state_and_sessions

    # 续跑锁在 state_and_sessions 的进程内 map：SQLite 自增 id 跨用例重号会串锁
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    engine = create_engine(f"sqlite:///{tmp_path / 'route_probe.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()


def _seed_stale_generating_trip(user_id: int = 7) -> int:
    """僵尸行程：GENERATING + 天 PENDING + 心跳拨老（同 test_checkpoint_resume 的 _zombie）。"""
    stale = datetime.now() - timedelta(minutes=30)
    with db_session.session_scope() as session:
        main = ItineraryMain(
            user_id=user_id,
            title="杭州 1 日游",
            city="杭州",
            days=1,
            persons=1,
            status=1,
            gen_state="GENERATING",
            updated_at=stale,
        )
        session.add(main)
        session.flush()
        session.add(ItineraryDay(itinerary_id=main.id, day_no=1, city="杭州", generation_status="PENDING"))
        session.flush()
        session.execute(update(ItineraryDay).where(ItineraryDay.itinerary_id == main.id).values(updated_at=stale))
        return main.id


def test_recovery_resume_leg_carries_route(sqlite_env, monkeypatch) -> None:
    """恢复线程内的 resume_day 检查点续跑腿必须携带用户路由（决策 5 的回归防线）。"""
    trip_id = _seed_stale_generating_trip()
    itinerary_generation.reset_active_planning_for_tests()
    seen: list[LLMRoute | None] = []
    monkeypatch.setattr(llm_gateway_service, "resolve_route", lambda uid: SENTINEL if uid == 7 else None)
    monkeypatch.setattr(generation_recovery, "resume_day", lambda _aid: (_seen_append(seen), None)[1])
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *_a, **_k: None)

    assert generation_recovery.recover_one(trip_id, failed_resume=False) is False
    assert seen == [SENTINEL], "resume_day 探针必须见到哨兵路由（不包则静默回落默认通道）"


def test_recovery_leg_without_gateway_stays_default(sqlite_env, monkeypatch) -> None:
    trip_id = _seed_stale_generating_trip()
    itinerary_generation.reset_active_planning_for_tests()
    seen: list[LLMRoute | None] = []
    monkeypatch.setattr(generation_recovery, "resume_day", lambda _aid: (_seen_append(seen), None)[1])
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *_a, **_k: None)

    generation_recovery.recover_one(trip_id, failed_resume=False)
    assert seen == [None], "无网关用户：route_scope 注入 None，get_llm_client 仍拿默认单例"


# ---------- 密文解不开：worker 腿的响亮收尾（终审补丁） ----------

_BROKEN = "密钥密文无法解密（加密 key 已轮换），请重新填写 API 密钥"


def _broken_route(_user_id: int):
    from app.common.envelope import ApiError

    raise ApiError(409, _BROKEN)


def test_plan_days_fails_trip_when_cipher_broken(sqlite_env, monkeypatch) -> None:
    """409 不得裸穿 generation_pool：就地 fail_trip + SSE 错误 + 解注册，防僵尸/注册表泄漏。"""
    trip_id = _seed_stale_generating_trip()
    itinerary_generation.reset_active_planning_for_tests()
    itinerary_generation._register_planning(trip_id)
    errors: list[tuple] = []
    monkeypatch.setattr(itinerary_generation.generation_events, "error", lambda *a, **k: errors.append((a, k)))
    monkeypatch.setattr(llm_gateway_service, "resolve_route", _broken_route)

    command = itinerary_generation.GenerateCommand(city="杭州", days=1, persons=1, stay_nights=0)
    itinerary_generation.plan_days(7, trip_id, command)  # 不抛 = 收尾已就地完成

    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.gen_state == "FAILED" and main.status == 3
        assert _BROKEN in (main.plan_note or "")
    assert itinerary_generation.is_planning_active(trip_id) is False, "注册表必须解注册（漏了会堵住重提）"
    assert errors, "SSE 错误事件必须发出（用户侧看得见失败原因）"
    assert errors[0][0][1] == "AGENT_ERROR" and errors[0][0][3] is True


def test_recovery_fails_trip_and_releases_lock_when_cipher_broken(sqlite_env, monkeypatch) -> None:
    trip_id = _seed_stale_generating_trip()
    itinerary_generation.reset_active_planning_for_tests()
    resumed: list[str] = []
    monkeypatch.setattr(generation_recovery, "resume_day", lambda _aid: (resumed.append(_aid), None)[1])
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *_a, **_k: None)
    monkeypatch.setattr(llm_gateway_service, "resolve_route", _broken_route)

    assert generation_recovery.recover_one(trip_id, failed_resume=False) is True
    assert resumed == [], "路由已断，续跑腿不得开跑"
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        assert main.gen_state == "FAILED" and _BROKEN in (main.plan_note or "")
    # 放锁验证：锁必须已释放（可再次获取）。
    # 旧预期为什么是错的（R2-F7 收窄，2026-10-05 拍板）：此处原断言"天未终态的
    # FAILED 行被 _resumable_failed_trip 闸挡住、failed_resume 扫描不再重复处理"
    # ——那正是 R2-F7 缺口本体（部分失败行程永不恢复），被当成特性钉进了测试。
    # 新语义：天未终态的 FAILED 行程允许恢复一次（第二次 failed_resume 扫描真重拉，
    # 撞断路由再次优雅失败），重拉即置 gen_resumed=1，第三次起由防死循环闸封顶。
    assert generation_recovery.generation_gate.try_resume_lock(trip_id) is True, "resume 锁必须已释放"
    generation_recovery.generation_gate.release_resume_lock(trip_id)
    assert generation_recovery.recover_one(trip_id, failed_resume=True) is True, "部分失败行程允许恢复一次"
    assert resumed == [], "路由已断，续跑腿仍不得开跑"
    generation_recovery.generation_gate.release_resume_lock(trip_id)
    assert generation_recovery.recover_one(trip_id, failed_resume=True) is False, "gen_resumed=1 封顶，不重拉"


def test_enrich_skips_quietly_when_cipher_broken(sqlite_env, monkeypatch) -> None:
    """富化是完成后锦上添花：路由断只跳过（记日志），不炸 enricher 线程。"""
    butler_calls: list[tuple] = []
    monkeypatch.setattr(itinerary_enricher, "_write_butler_note", lambda *a, **k: butler_calls.append(a))
    monkeypatch.setattr(llm_gateway_service, "resolve_route", _broken_route)

    itinerary_enricher.enrich_itinerary(7, 999_999, object())
    assert butler_calls == []


def test_plan_days_failure_note_and_sse_are_user_facing(sqlite_env, monkeypatch) -> None:
    """R2-F3：原始异常文本（含上游 URL）只进日志；plan_note 与 SSE 错误帧都给用户文案。

    （判据在 app/common/envelope.user_reason / FAIL_TRIP_NOTE。）

    旧行为把 str(exc) 原样写进 plan_note 并塞进 SSE error/degraded 帧——失败行程的
    详情页副标题和聊天回显会出现 "https://api.deepseek.com/..." 这样的运维细节。
    """

    trip_id = _seed_stale_generating_trip()
    itinerary_generation.reset_active_planning_for_tests()
    errors: list[tuple] = []
    monkeypatch.setattr(itinerary_generation.generation_events, "error", lambda *a, **k: errors.append((a, k)))

    def _boom(*_a, **_k):
        raise RuntimeError("Client error '400 Bad Request' for url 'https://api.deepseek.com/chat/completions'")

    monkeypatch.setattr(itinerary_generation, "_shell_exists", _boom)
    command = itinerary_generation.GenerateCommand(city="杭州", days=1, persons=1, stay_nights=0)
    itinerary_generation.plan_days(7, trip_id, command)

    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        # plan_note 必须是用户文案（R2-F3，无 URL/原文）；API-1 起追加稳定错误码
        # （envelope.error_code：ApiError → API_{status}，其余 → 异常类名）
        assert main.plan_note == f"{FAIL_TRIP_NOTE}（错误码 RuntimeError）"
    assert errors, "SSE 错误帧必须发出"
    frame_message = str(errors[0][0][2])
    assert "api.deepseek.com" not in frame_message, "SSE 帧不得携带上游 URL"
    assert FAIL_TRIP_NOTE in frame_message
