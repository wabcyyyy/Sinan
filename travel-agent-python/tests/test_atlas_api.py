"""Atlas 聚合（SPEC v2.3 §6.7，S3）。

钉住六步口径的每一格：质心自聚合（含跨行程合并与 0/0 剔除）、`city_geo` 兜底
（geo_fallback）、双覆盖度轴（no_coordinates 与 dict_miss 分开计/分开报）、归国
绝不默认 CN、scope 纯日期启发式（visited 需「已结束 + 生成完成」）、归档与软删
不入图鉴、未知 scope 400、未登录 401。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.business.atlas import router as atlas_router
from app.common.envelope import install_exception_handlers
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base, CityGeo, ItineraryDay, ItineraryItem, ItineraryMain

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"  # 测试占位串，非真实凭据


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    engine = create_engine(f"sqlite:///{tmp_path / 'atlas.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))

    monkeypatch.setattr(deps.settings, "jwt_secret", SIGNING_MATERIAL)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 42, "username": "alice", "role": "user", "status": 1},
    )

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(atlas_router)
    yield TestClient(app)
    db_session.init_engine(None, None)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def _add_trip(
    city: str, end_offset: int, gen_state: str = "COMPLETED", coords: tuple = (), *, archived: int = 0, deleted: int = 0
) -> int:
    with db_session.session_scope() as session:
        main = ItineraryMain(
            user_id=42,
            title=f"{city}行程",
            city=city,
            days=1,
            persons=1,
            start_date=date.today() - timedelta(days=abs(end_offset) + 3),
            end_date=date.today() + timedelta(days=end_offset),
            status=2,
            gen_state=gen_state,
            archived=archived,
            deleted=deleted,
        )
        session.add(main)
        session.flush()
        day = ItineraryDay(itinerary_id=main.id, day_no=1)
        session.add(day)
        session.flush()
        for index, coord in enumerate(coords):
            lat, lng = (Decimal(coord[0]), Decimal(coord[1])) if coord else (None, None)
            session.add(
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=main.id,
                    item_type="attraction",
                    poi_name=f"{city}点位{index}",
                    latitude=lat,
                    longitude=lng,
                    sort_no=index,
                )
            )
        return main.id


def _seed() -> None:
    # 城市字典：与 V2 种子的口径一致（巴黎显式无坐标 → 只支撑归国不支撑兜底）
    with db_session.session_scope() as session:
        session.add_all(
            [
                CityGeo(city_name="杭州", country="中国", country_code="CN", is_domestic=1),
                CityGeo(city_name="上海", country="中国", country_code="CN", is_domestic=1),
                CityGeo(
                    city_name="大阪",
                    country="日本",
                    country_code="JP",
                    lat=Decimal("34.690000"),
                    lng=Decimal("135.500000"),
                    is_domestic=0,
                ),
                CityGeo(city_name="巴黎", country="法国", country_code="FR", is_domestic=0),
            ]
        )
    # 杭州：一趟已结束 + 一趟未来（验证跨行程质心与 per-pin tripCount）；
    # 混入一条 0/0 占位坐标——按口径必须被剔除（也算缺坐标）
    _add_trip(
        "杭州",
        -30,
        coords=(
            ("30.220000", "120.120000"),
            ("30.240000", "120.160000"),
            ("0.000000", "0.000000"),
            None,
        ),
    )
    _add_trip("杭州", +10, coords=(("30.300000", "120.200000"),))
    _add_trip("上海", +30, coords=(("31.230000", "121.470000"),))
    # 大阪：无点位坐标，字典有坐标 → geo_fallback
    _add_trip("大阪", -60, coords=(None,))
    # 巴黎：无点位坐标、字典也无坐标 → no_coordinates（不进 pins）
    _add_trip("巴黎", -90, coords=(None,))
    # 雷克雅未克：有点位坐标但字典未命中 → 出钉但 country=null + dict_miss
    _add_trip("雷克雅未克", -120, coords=(("64.146600", "-21.942600"),))
    # 归档 / 软删一律不入图鉴
    _add_trip("成都", -45, coords=(("30.570000", "104.060000"),), archived=1)
    _add_trip("西安", -45, coords=(("34.340000", "108.940000"),), deleted=1)


def _atlas(client: TestClient, scope: str | None = None) -> dict:
    query = f"?scope={scope}" if scope else ""
    response = client.get(f"/api/atlas{query}", headers=_headers())
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _pin(atlas: dict, city: str) -> dict:
    return next(pin for pin in atlas["pins"] if pin["city"] == city)


def test_centroid_aggregation_and_scope(client: TestClient) -> None:
    _seed()
    atlas = _atlas(client)

    hangzhou = _pin(atlas, "杭州")
    assert hangzhou["coordSource"] == "items"
    assert hangzhou["lat"] == pytest.approx((30.22 + 30.24 + 30.30) / 3, abs=1e-6)
    assert hangzhou["lng"] == pytest.approx((120.12 + 120.16 + 120.20) / 3, abs=1e-6)
    assert hangzhou["country"] == "中国" and hangzhou["countryCode"] == "CN"
    assert hangzhou["tripCount"] == 2
    assert {trip["scope"] for trip in hangzhou["trips"]} == {"visited", "planned"}
    assert all(trip["coverUrl"] is None for trip in hangzhou["trips"])


def test_geo_fallback_when_items_have_no_coords(client: TestClient) -> None:
    _seed()
    osaka = _pin(_atlas(client), "大阪")
    assert osaka["coordSource"] == "geo_fallback"
    assert osaka["lat"] == pytest.approx(34.69) and osaka["lng"] == pytest.approx(135.5)
    assert osaka["countryCode"] == "JP"


def test_coverage_axes_and_unknown_cities(client: TestClient) -> None:
    _seed()
    atlas = _atlas(client)
    # 巴黎：无坐标（不进 pins）；雷克雅未克：未归国（出钉但 country=null）
    assert atlas["unknownCities"] == [
        {"city": "巴黎", "reason": "no_coordinates"},
        {"city": "雷克雅未克", "reason": "dict_miss"},
    ]
    reykjavik = _pin(atlas, "雷克雅未克")
    assert reykjavik["country"] is None and reykjavik["countryCode"] is None
    assert reykjavik["coordSource"] == "items"

    assert atlas["coverage"] == {
        "tripsTotal": 6,
        "pinsRendered": 4,
        "itemsWithoutCoord": 4,  # 杭州 2（含 0/0）+ 大阪 1 + 巴黎 1
        "dictMiss": 1,  # 只算未命中的雷克雅未克
    }
    assert atlas["stats"] == {
        "cityCount": 4,
        "countryCount": 2,
        "tripCount": 6,
        "plannedTripCount": 2,  # 杭州（未来）+ 上海
        "visitedTripCount": 4,
    }
    assert atlas["highlightCountryCodes"] == ["CN", "JP"]


def test_scope_filters_server_side(client: TestClient) -> None:
    _seed()
    visited = _atlas(client, "visited")
    # 巴黎无坐标本就不进 pins；杭州只剩「已结束」那一趟
    assert {pin["city"] for pin in visited["pins"]} == {"杭州", "大阪", "雷克雅未克"}
    assert _pin(visited, "杭州")["tripCount"] == 1
    assert visited["stats"]["visitedTripCount"] == 4
    assert visited["stats"]["plannedTripCount"] == 0

    planned = _atlas(client, "planned")
    assert {pin["city"] for pin in planned["pins"]} == {"杭州", "上海"}
    assert planned["stats"]["plannedTripCount"] == 2
    assert planned["stats"]["visitedTripCount"] == 0


def test_archived_and_deleted_trips_are_excluded(client: TestClient) -> None:
    _seed()
    cities = {pin["city"] for pin in _atlas(client)["pins"]}
    assert "成都" not in cities and "西安" not in cities


def test_unknown_scope_is_400(client: TestClient) -> None:
    _seed()
    response = client.get("/api/atlas?scope=everywhere", headers=_headers())
    assert response.status_code == 400


def test_atlas_requires_auth(client: TestClient) -> None:
    assert client.get("/api/atlas").status_code == 401
