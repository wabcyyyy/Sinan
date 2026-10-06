"""生成端点幂等键测试（机会主义 backlog→实现：「被重复请求咬过」）。

覆盖：
- cache_store.reserve 原语的原子语义（首次 True / 重复 False / 值不被覆盖 /
  过期后可重新占位 / None 哨兵往返）；
- generate 服务层：同键重试返回同一个行程（不重复建壳）、不同键独立、
  键按 user 隔离、入队被拒（429）后键仍指向同一个失败壳；
- API 层：X-Idempotency-Key 头透传，两次同键请求 data.id 一致。

基建：临时 SQLite 库 + 显式禁用 Redis 客户端
（cache_store 走进程内降级，确定性不依赖外部 Redis）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.business import itinerary as itinerary_routes
from app.common import cache_store
from app.common.envelope import ApiError, install_exception_handlers
from app.common.task_pool import TaskRejected
from app.db import session as db_session
from app.db.models import Base
from app.services import itinerary_generation


@pytest.fixture(autouse=True)
def _local_cache_only(monkeypatch):
    def _unavailable():
        raise ConnectionError("Redis disabled for offline idempotency tests")

    monkeypatch.setattr(cache_store, "_get_client", _unavailable)
    cache_store.reset_for_tests()


def _setup_db(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'idem.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))


def teardown_function(_):
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


# ---------- cache_store.reserve 原语 ----------


def test_reserve_is_atomic_claim(monkeypatch, tmp_path):
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    assert cache_store.reserve("ns", "k", 1, 60) is True, "首次占位"
    assert cache_store.reserve("ns", "k", 2, 60) is False, "重复占位被拒"
    assert cache_store.get_json("ns", "k") == 1, "先占位的值不被后到者覆盖"


def test_reserve_expired_allows_reclaim(monkeypatch, tmp_path):
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    import time

    cache_store.reserve("ns", "k", 1, 60)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 61)
    assert cache_store.reserve("ns", "k", 2, 60) is True, "过期后可重新占位"
    assert cache_store.get_json("ns", "k") == 2


def test_reserve_none_value_roundtrip(monkeypatch, tmp_path):
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    assert cache_store.reserve("ns", "k", None, 60) is True
    assert cache_store.get_json("ns", "k") is None, "哨兵值往返保持 None"


# ---------- generate 服务层 ----------


def test_generate_with_same_key_replays_same_itinerary(monkeypatch, tmp_path):
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    body = itinerary_generation.GenerateTripRequest(city="杭州", days=1)
    first = itinerary_generation.generate(1, body, idempotency_key="abc")
    second = itinerary_generation.generate(1, body, idempotency_key="abc")
    assert first["id"] == second["id"], "同键重试必须返回同一个行程，不建第二份壳"
    ids = [first["id"], second["id"]]
    assert len(set(ids)) == 1


def test_concurrent_same_key_waits_backfill_and_replays(monkeypatch, tmp_path):
    """API-3：回填窗口（PENDING 占位未回填）内并发同键——等待回填后重放
    同一个行程，不再放行建第二份壳。"""
    import threading
    import time as _time

    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    body = itinerary_generation.GenerateTripRequest(city="杭州", days=1)
    # 无键先建一份真实壳，模拟「首个请求」即将回填的目标 id
    seed = itinerary_generation.generate(1, body)
    sid = seed["id"]
    # 手动占住 PENDING，模拟首个请求正处于「占位成功 → 建壳提交 → 回填」窗口
    assert cache_store.reserve(itinerary_generation._IDEM_NAMESPACE, "1:abc", "PENDING", 600)
    threading.Timer(0.3, lambda: cache_store.set_json(itinerary_generation._IDEM_NAMESPACE, "1:abc", sid, 600)).start()
    started = _time.monotonic()
    other = itinerary_generation.generate(1, body, idempotency_key="abc")
    waited = _time.monotonic() - started
    assert other["id"] == sid, "等待者拿到回填后的同一个行程，不另建壳"
    assert waited >= 0.25, "确实经历了等待回填（而非立即放行）"
    from app.db.models import ItineraryMain

    with db_session.session_scope() as session:
        mains = session.query(ItineraryMain).all()
    assert len(mains) == 1, "全程只有 seed 那一份壳"


def test_stale_pending_released_for_fresh_attempt(monkeypatch, tmp_path):
    """API-3 崩溃窗口：占位后一直未回填（建壳者进程死亡）——等待超时后释放
    键并降级为新一次生成（宁重复、不 500，与旧口径一致）。"""
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "_IDEM_BACKFILL_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    assert cache_store.reserve(itinerary_generation._IDEM_NAMESPACE, "1:abc", "PENDING", 600)
    body = itinerary_generation.GenerateTripRequest(city="杭州", days=1)
    result = itinerary_generation.generate(1, body, idempotency_key="abc")
    from app.db.models import ItineraryMain

    with db_session.session_scope() as session:
        mains = session.query(ItineraryMain).all()
    assert len(mains) == 1 and result["id"] == mains[0].id, "超时释放后按新请求建壳"


def test_generate_without_key_creates_each_time(monkeypatch, tmp_path):
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    body = itinerary_generation.GenerateTripRequest(city="杭州", days=1)
    first = itinerary_generation.generate(1, body)
    second = itinerary_generation.generate(1, body)
    assert first["id"] != second["id"], "无键 = 每次都是新操作"


def test_generate_different_keys_are_independent(monkeypatch, tmp_path):
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    body = itinerary_generation.GenerateTripRequest(city="杭州", days=1)
    a = itinerary_generation.generate(1, body, idempotency_key="k1")
    b = itinerary_generation.generate(1, body, idempotency_key="k2")
    assert a["id"] != b["id"]


def test_generate_keys_are_scoped_per_user(monkeypatch, tmp_path):
    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    body = itinerary_generation.GenerateTripRequest(city="杭州", days=1)
    a = itinerary_generation.generate(1, body, idempotency_key="shared")
    b = itinerary_generation.generate(2, body, idempotency_key="shared")
    assert a["id"] != b["id"], "键按 user 隔离"


def test_queue_full_releases_key_for_fresh_retry(monkeypatch, tmp_path):
    """入队被拒（429）是"结果已知"的失败：键随之释放，用户重试 = 新一次
    尝试（新建壳）。幂等只防"结果未知"（网络超时/断连）的重复——那类重试
    在 try 块之外拿到 200 后不会发生。"""

    def _reject(*_a, **_k):
        raise TaskRejected()

    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "submit_planning", _reject)
    body = itinerary_generation.GenerateTripRequest(city="杭州", days=1)
    with pytest.raises(ApiError) as first:
        itinerary_generation.generate(1, body, idempotency_key="abc")
    assert first.value.status == 429
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    retry = itinerary_generation.generate(1, body, idempotency_key="abc")
    from app.db.models import ItineraryMain

    with db_session.session_scope() as session:
        mains = session.query(ItineraryMain).order_by(ItineraryMain.id).all()
    assert len(mains) == 2, "429 后重试应建第二份壳（键已释放）"
    assert retry["id"] == mains[1].id


# ---------- API 层 ----------


def test_api_passes_idempotency_header(monkeypatch, tmp_path):
    from app.api import deps
    from app.common import token_revocation
    from app.common.jwt_compat import encode_token

    _setup_db(tmp_path)
    cache_store.reset_for_tests()
    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *a, **k: None)
    # 鉴权三件套（同 test_business_api_pilot）：签名材料 + 吊销关 + 用户回查
    monkeypatch.setattr(deps.settings, "jwt_secret", "example-only-hs256-test-signing-material")
    monkeypatch.setattr(token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 7, "username": "alice", "role": "user", "status": 1},
    )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(itinerary_routes.router)
    client = TestClient(app)
    auth = {"Authorization": f"Bearer {encode_token('alice', 'example-only-hs256-test-signing-material', 3600)}"}
    body = {"city": "杭州", "days": 1}
    first = client.post("/api/itinerary/generate", json=body, headers={**auth, "X-Idempotency-Key": "ui-123"}).json()
    assert first["code"] == 200, f"首次请求应成功，实际：{first}"
    second = client.post("/api/itinerary/generate", json=body, headers={**auth, "X-Idempotency-Key": "ui-123"}).json()
    assert second["code"] == 200, f"重试应成功，实际：{second}"
    assert first["data"]["id"] == second["data"]["id"], "同键两次请求 = 同一个行程"
