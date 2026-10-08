"""公开分享（SPEC v2.3 §6.6，S2）。

钉住四件事：
① token 生命周期（创建 / 状态 / 撤销 / 过期 / 轮换）；
② **统一 404**——不存在、已过期、已撤销、已软删四种情况同状态码同文案，
   不给枚举者区分信号；
③ **脱敏白名单**——匿名 VO 里不允许出现 userId / 内部主键 / chat / trace /
   qualityReport 等任何内部字段（递归扫描断言）；
④ 按 IP 限流（429）与归档语义（归档后旧分享仍可开、新建分享 400）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.business.itinerary import router as itinerary_router
from app.api.business.share import router as share_router
from app.common.envelope import install_exception_handlers
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base, BudgetDetail, ItineraryDay, ItineraryItem, ItineraryMain
from app.services import share_service, state_and_sessions

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"  # 测试占位串，非真实凭据


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    engine = create_engine(f"sqlite:///{tmp_path / 'share.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))

    monkeypatch.setattr(deps.settings, "jwt_secret", SIGNING_MATERIAL)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 42, "username": "alice", "role": "user", "status": 1},
    )
    state_and_sessions.reset_for_tests()

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(itinerary_router)
    app.include_router(share_router)
    yield TestClient(app)
    db_session.init_engine(None, None)
    state_and_sessions.reset_for_tests()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def _seed_trip() -> int:
    with db_session.session_scope() as session:
        main = ItineraryMain(
            user_id=42,
            title="杭州3日游",
            city="杭州",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 3),
            days=3,
            persons=2,
            budget=Decimal("3000.00"),
            status=2,
            trip_theme="西湖慢行",
            plan_note="建议早出发",
            cover_url="/api/uploads/covers/42/cover.jpg",
            cover_credit=json.dumps({"author": "Ada", "source": "unsplash"}),
        )
        session.add(main)
        session.flush()
        day = ItineraryDay(
            itinerary_id=main.id, day_no=1, travel_date=date(2026, 4, 1), note="到达", metadata_json='{"theme":"湖畔"}'
        )
        session.add(day)
        session.flush()
        session.add_all(
            [
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=main.id,
                    item_type="attraction",
                    poi_name="西湖",
                    address="西湖区",
                    start_time=time(9, 30),
                    end_time=time(11, 0),
                    cost=Decimal("0.00"),
                    latitude=Decimal("30.220000"),
                    longitude=Decimal("120.120000"),
                    source="mysql.poi_knowledge",
                    verification_status="verified",
                    freshness_status="fresh",
                    review_requirement="none",
                    sort_no=0,
                ),
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=main.id,
                    item_type="food",
                    poi_name="楼外楼",
                    cost=Decimal("88.50"),
                    source="llm.open_day",
                    verification_status="unverified",
                    freshness_status="stale",
                    review_requirement="before_departure",
                    sort_no=1,
                ),
            ]
        )
        session.add(BudgetDetail(itinerary_id=main.id, category="餐饮", amount=Decimal("88.50"), item_count=1))
        return main.id


def _create_share(client: TestClient, trip_id: int, expire_days: int | None = None) -> str:
    response = client.post(f"/api/itinerary/{trip_id}/share", json={"expireDays": expire_days}, headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["data"]["shareToken"]


def test_share_lifecycle(client: TestClient) -> None:
    trip_id = _seed_trip()
    assert client.get(f"/api/itinerary/{trip_id}/share", headers=_headers()).json()["data"] == {"shared": False}

    token = _create_share(client, trip_id, expire_days=30)
    assert len(token) >= 24

    status = client.get(f"/api/itinerary/{trip_id}/share", headers=_headers()).json()["data"]
    assert status["shared"] is True and status["shareToken"] == token
    assert status["shareUrl"] == f"/s/{token}"
    assert status["shareExpiresAt"] is not None

    assert client.get(f"/api/share/{token}").status_code == 200

    assert client.delete(f"/api/itinerary/{trip_id}/share", headers=_headers()).status_code == 200
    assert client.get(f"/api/itinerary/{trip_id}/share", headers=_headers()).json()["data"] == {"shared": False}
    assert client.get(f"/api/share/{token}").status_code == 404


def test_recreate_rotates_token(client: TestClient) -> None:
    trip_id = _seed_trip()
    first = _create_share(client, trip_id)
    second = _create_share(client, trip_id)
    assert first != second, "重复创建必须轮换 token"
    assert client.get(f"/api/share/{first}").status_code == 404
    assert client.get(f"/api/share/{second}").status_code == 200


def test_anonymous_view_is_sanitized_whitelist(client: TestClient) -> None:
    trip_id = _seed_trip()
    token = _create_share(client, trip_id)

    response = client.get(f"/api/share/{token}")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]

    assert set(data) == {
        "city",
        "title",
        "days",
        "persons",
        "startDate",
        "endDate",
        "budget",
        "hotelTier",
        "tripTheme",
        "coverUrl",
        "coverCredit",
        "totalAmount",
        "dayList",
        "budgetList",
        "planNote",
    }
    assert data["coverUrl"] == "/api/uploads/covers/42/cover.jpg"
    assert data["coverCredit"] == {"author": "Ada", "source": "unsplash"}
    assert data["totalAmount"] == 88.5

    day = data["dayList"][0]
    assert day["dayNo"] == 1 and day["theme"] == "湖畔"
    assert day["dayTotalAmount"] == 88.5, "dayTotalAmount 为对 items[].cost 的新算值"
    item = day["items"][0]
    assert set(item) == {
        "poiName",
        "itemType",
        "address",
        "startTime",
        "endTime",
        "cost",
        "image",
        "latitude",
        "longitude",
        "source",
    }
    assert item["startTime"] == "09:30"

    forbidden = {
        "userId",
        "id",
        "itemId",
        "shareToken",
        "intent",
        "chat",
        "trace",
        "qualityStatus",
        "qualityReport",
        "factEvidence",
        "factEvidenceJson",
        "sources",
        "suggestions",
        "schemaVersion",
        "pendingFactCount",
    }
    assert not (_all_keys(data) & forbidden), "匿名 VO 不允许出现任何内部字段"


def _all_keys(node) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            keys |= _all_keys(value)
    elif isinstance(node, list):
        for value in node:
            keys |= _all_keys(value)
    return keys


def test_unknown_expired_revoked_deleted_share_are_uniform_404(client: TestClient) -> None:
    trip_id = _seed_trip()
    messages = []

    unknown = client.get("/api/share/does-not-exist-token")
    messages.append((unknown.status_code, unknown.json()["message"]))

    expired_token = _create_share(client, trip_id)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.share_expires_at = datetime.now() - timedelta(days=1)
    expired = client.get(f"/api/share/{expired_token}")
    messages.append((expired.status_code, expired.json()["message"]))

    revoked_token = _create_share(client, trip_id)
    client.delete(f"/api/itinerary/{trip_id}/share", headers=_headers())
    revoked = client.get(f"/api/share/{revoked_token}")
    messages.append((revoked.status_code, revoked.json()["message"]))

    deleted_token = _create_share(client, trip_id)
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        main.deleted = 1
    deleted = client.get(f"/api/share/{deleted_token}")
    messages.append((deleted.status_code, deleted.json()["message"]))

    assert all(status == 404 for status, _ in messages)
    assert len({message for _, message in messages}) == 1, "四种失效原因必须同文案"


def test_archive_blocks_new_share_but_old_link_still_works(client: TestClient) -> None:
    trip_id = _seed_trip()
    token = _create_share(client, trip_id)

    client.post(f"/api/itinerary/{trip_id}/archive", json={"archived": True}, headers=_headers())
    assert client.get(f"/api/share/{token}").status_code == 200, "归档不撤回已发出的链接"

    response = client.post(f"/api/itinerary/{trip_id}/share", json={"expireDays": None}, headers=_headers())
    assert response.status_code == 400


def test_share_rate_limit_is_enforced(client: TestClient, monkeypatch) -> None:
    trip_id = _seed_trip()
    token = _create_share(client, trip_id)
    monkeypatch.setattr(share_service.settings, "share_rate_limit_per_minute", 1)

    assert client.get(f"/api/share/{token}").status_code == 200
    assert client.get(f"/api/share/{token}").status_code == 429


def test_share_owner_endpoints_require_auth(client: TestClient) -> None:
    trip_id = _seed_trip()
    assert client.post(f"/api/itinerary/{trip_id}/share", json={}).status_code == 401
    assert client.get(f"/api/itinerary/{trip_id}/share").status_code == 401
    assert client.delete(f"/api/itinerary/{trip_id}/share").status_code == 401


def test_expire_days_validation(client: TestClient) -> None:
    trip_id = _seed_trip()
    bad = client.post(f"/api/itinerary/{trip_id}/share", json={"expireDays": 5}, headers=_headers())
    assert bad.status_code == 400
    ok = client.post(f"/api/itinerary/{trip_id}/share", json={"expireDays": 7}, headers=_headers())
    assert ok.status_code == 200 and ok.json()["data"]["shareExpiresAt"] is not None


def test_detail_reflects_share_token_after_create_and_remove(client: TestClient) -> None:
    trip_id = _seed_trip()
    token = _create_share(client, trip_id)
    detail = client.get(f"/api/itinerary/{trip_id}", headers=_headers()).json()["data"]
    assert detail["shareToken"] == token
    assert detail["coverUrl"] == "/api/uploads/covers/42/cover.jpg"

    client.delete(f"/api/itinerary/{trip_id}/share", headers=_headers())
    detail = client.get(f"/api/itinerary/{trip_id}", headers=_headers()).json()["data"]
    assert detail["shareToken"] is None
