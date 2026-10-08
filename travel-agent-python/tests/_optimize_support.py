"""优化器两个测试文件共享的夹具与种子（W3/C1.3）。

放在非测试模块（不叫 test_*）再由 `pytest_plugins` 挂载，是刻意的：直接
`pytest_plugins = ["test_itinerary_optimize_day"]` 借邻居夹具，一旦邻居先被
收集成测试模块（命令行顺序、目录收收取决），夹具就注册不上——顺序脆弱。
本模块永远不会被收集，两个测试文件任意顺序都稳定。

提供：临时 SQLite + 已登录 client + 共线三点种子（甲 0.0 / 丙 0.2 / 乙 0.1，
唯一最优序 甲 → 乙 → 丙）。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

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
from app.db.models import (
    Base,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.services import user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'w3.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=hashed, status=1, role="user"))
        trip = ItineraryMain(
            user_id=1,
            title="杭州2日游",
            city="杭州",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 2),
            days=2,
            persons=2,
            budget=Decimal("2000.00"),
            status=2,
        )
        session.add(trip)
        session.flush()
        first = ItineraryDay(
            itinerary_id=trip.id, day_no=1, generation_status="SUCCEEDED", metadata_json='{"theme": "初版标题"}'
        )
        second = ItineraryDay(itinerary_id=trip.id, day_no=2, generation_status="SUCCEEDED")
        session.add_all([first, second])
        session.flush()
        # 共线三点、故意乱序：甲(经度 0.0) 丙(0.02) 乙(0.01) —— 唯一最优路径为 甲 → 乙 → 丙。
        # M3 步行 4.5km/h：0.2°(≈21km) 邻点转场 226 分钟会把第三点挤出单日窗口，
        # 收拢到 0.02°(≈1.9km、转场约 53 分钟) 保持"唯一最优序"的断言意图。
        session.add_all(
            [
                ItineraryItem(
                    day_id=first.id,
                    itinerary_id=trip.id,
                    item_type="attraction",
                    poi_name="甲点",
                    latitude=Decimal("30.000000"),
                    longitude=Decimal("120.000000"),
                    sort_no=0,
                ),
                ItineraryItem(
                    day_id=first.id,
                    itinerary_id=trip.id,
                    item_type="attraction",
                    poi_name="丙点",
                    latitude=Decimal("30.000000"),
                    longitude=Decimal("120.020000"),
                    sort_no=1,
                ),
                ItineraryItem(
                    day_id=first.id,
                    itinerary_id=trip.id,
                    item_type="attraction",
                    poi_name="乙点",
                    latitude=Decimal("30.000000"),
                    longitude=Decimal("120.010000"),
                    sort_no=2,
                ),
            ]
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/auth/login", json={"username": "alice", "password": "x"})
    return client


def _trip_id(client: TestClient) -> int:
    return client.get("/api/itinerary").json()["data"][0]["id"]


def _day_ids(trip_id: int) -> list[int]:
    with db_session.session_scope() as session:
        return [
            row.id
            for row in session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        ]
