"""显式长期记忆（规格 §3.1）：GET/PUT /api/itinerary/preferences/memory 的 S1 行为。

隔离测试库 + TestClient。核心边界：
- 历史累计标签（其他 source）不冒充长期记忆，只有显式保存后才进入；
- 同名保存直接更新现有行（唯一键），不插重复行；
- 空集合 = 遗忘此入口管理的全部记忆，不删除其他来源标签；
- 记忆按用户隔离；历史信号接口（/preferences/signals）与旧数据保留。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common.cache_store import reset_for_tests as reset_cache
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryMain, SysUser, UserPreference
from app.services import preferences, user_service

MEMORY_SOURCE = "explicit_memory"

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture
def memory_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    reset_cache()
    with db_session.session_scope() as session:
        hashed = user_service.hash_password("x")
        session.add_all(
            [
                SysUser(username="alice", password=hashed, status=1, role="user"),
                SysUser(username="mallory", password=hashed, status=1, role="user"),
            ]
        )
    yield
    db_session.init_engine(None, None)
    reset_cache()


def _client(username: str = "alice") -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": username, "password": "x"}).status_code == 200
    return client


def _memory(client: TestClient) -> dict:
    return client.get("/api/itinerary/preferences/memory").json()["data"]


def _labels_by_source() -> dict[str, list[tuple[str, int]]]:
    with db_session.session_scope() as session:
        rows = session.scalars(select(UserPreference)).all()
        grouped: dict[str, list[tuple[str, int]]] = {}
        for row in rows:
            grouped.setdefault(row.source, []).append((row.pref_label, row.negative))
        return grouped


# ---------- 历史标签不冒充长期记忆 ----------


def test_historical_signals_do_not_impersonate_memory(memory_db):
    client = _client()
    assert (
        client.post(
            "/api/itinerary/preferences/signals",
            json={"explicitPreferences": ["美食"], "negativePreferences": ["购物"], "source": "explicit"},
        ).status_code
        == 200
    )
    assert _memory(client) == {"positive": [], "negative": []}, "历史累计标签不是长期记忆（规格 §3.1.3/§3.1.4）"
    signals = client.get("/api/itinerary/preferences/signals").json()["data"]
    assert {row["label"] for row in signals} == {"美食", "购物"}, "历史信号接口与旧数据保留"


def test_generation_no_longer_accumulates_memory(memory_db, monkeypatch):
    """规格 §3.1.3：普通建壳不再把本次偏好写入长期记忆（只随壳存主表 preferences）。"""
    from app.schemas.business.itinerary import GenerateTripRequest
    from app.services import itinerary_generation

    monkeypatch.setattr(itinerary_generation, "submit_planning", lambda *_args: None)
    client = _client()
    request = GenerateTripRequest(city="杭州", days=1, persons=1, preferences=["美食", "亲子"])
    detail = itinerary_generation.generate(1, request)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, detail["id"])
        assert main is not None and main.preferences == "美食,亲子", "本次偏好仍进行程参数"
    assert _memory(client) == {"positive": [], "negative": []}, "建壳不写长期记忆"
    assert _labels_by_source() == {}, "user_preference 表完全不被建壳触碰"


# ---------- 显式保存 / 同名更新 / 遗忘 ----------


def test_explicit_save_update_and_forget(memory_db):
    client = _client()
    saved = client.put(
        "/api/itinerary/preferences/memory", json={"positive": ["人文历史", "美食"], "negative": ["购物"]}
    ).json()["data"]
    # 视图按标签码点排序（确定性输出，与保存顺序无关）
    assert saved == {"positive": ["人文历史", "美食"], "negative": ["购物"]}

    # 同名保存直接更新现有行，不插重复行；从集合移除的行被删除
    saved = client.put(
        "/api/itinerary/preferences/memory", json={"positive": ["美食"], "negative": ["人文历史"]}
    ).json()["data"]
    assert saved == {"positive": ["美食"], "negative": ["人文历史"]}
    rows = _labels_by_source()[MEMORY_SOURCE]
    assert sorted(label for label, _neg in rows) == ["人文历史", "美食"], "集合外行被移除，无重复行"

    # 空集合 = 遗忘此入口管理的全部记忆
    saved = client.put("/api/itinerary/preferences/memory", json={"positive": [], "negative": []}).json()["data"]
    assert saved == {"positive": [], "negative": []}
    assert _labels_by_source() == {}, "explicit_memory 行已删除"


def test_forget_keeps_other_source_labels(memory_db):
    client = _client()
    client.post("/api/itinerary/preferences/signals", json={"explicitPreferences": ["亲子"], "source": "explicit"})
    client.put("/api/itinerary/preferences/memory", json={"positive": ["美食"]})
    client.put("/api/itinerary/preferences/memory", json={"positive": []})
    grouped = _labels_by_source()
    assert grouped.get(MEMORY_SOURCE) is None, "遗忘只清 explicit_memory"
    assert grouped.get("explicit") == [("亲子", 0)], "其他来源标签不被删除（规格 §3.1.6）"


def test_saving_historical_label_promotes_source_in_place(memory_db):
    """历史标签被显式保存后转正：更新同一行的 source/negative，不插重复行（规格 §3.1.4）。"""
    preferences.record_signals(1, ["美食"], [], [], "explicit", 1.0)
    with db_session.session_scope() as session:
        row = session.scalars(
            select(UserPreference).where(UserPreference.user_id == 1, UserPreference.pref_label == "美食")
        ).one()
        row.count = 7
    client = _client()
    assert client.put("/api/itinerary/preferences/memory", json={"negative": ["美食"]}).status_code == 200
    with db_session.session_scope() as session:
        rows = session.scalars(
            select(UserPreference).where(UserPreference.user_id == 1, UserPreference.pref_label == "美食")
        ).all()
        assert len(rows) == 1, "唯一键 (user_id, pref_label)：就地更新，不插重复行"
        assert rows[0].source == MEMORY_SOURCE and rows[0].negative == 1 and rows[0].count == 7
    assert _memory(client) == {"positive": [], "negative": ["美食"]}


# ---------- 校验与隔离 ----------


def test_memory_rejects_overlap_overflow_and_long_labels(memory_db):
    client = _client()
    overlap = client.put("/api/itinerary/preferences/memory", json={"positive": ["美食"], "negative": ["美食"]})
    assert overlap.status_code == 400 and "同时" in overlap.json()["message"]
    too_many = client.put(
        "/api/itinerary/preferences/memory",
        json={"positive": [f"标签{i}" for i in range(15)], "negative": [f"忌口{i}" for i in range(6)]},
    )
    assert too_many.status_code == 400, "正负合计最多 20 项（规格 §3.1.2）"
    too_long = client.put("/api/itinerary/preferences/memory", json={"positive": ["喜" * 33]})
    assert too_long.status_code == 400, "每项 trim 后 1–32 字符"
    trimmed = client.put("/api/itinerary/preferences/memory", json={"positive": ["  美食  ", "美食"]})
    assert trimmed.json()["data"]["positive"] == ["美食"], "trim + 去重保序"


def test_memory_is_per_user(memory_db):
    alice = _client()
    alice.put("/api/itinerary/preferences/memory", json={"positive": ["美食"]})
    mallory = _client("mallory")
    assert _memory(mallory) == {"positive": [], "negative": []}, "不能读取其他用户的记忆"
    mallory.put("/api/itinerary/preferences/memory", json={"positive": ["徒步"]})
    assert _memory(alice) == {"positive": ["美食"], "negative": []}, "mallory 的保存不影响 alice"


def test_memory_literal_route_is_not_swallowed_by_id(memory_db):
    client = _client()
    assert client.get("/api/itinerary/preferences/memory").status_code == 200
    assert client.get("/api/itinerary/preferences").status_code == 200, "既有 /preferences 字面量路由不受影响"
