"""迁移管道端到端验证（第一个真实业务端点）。

选 `GET /api/itinerary/supported-cities` 作试点，因为它一次跑完就同时验证了
四件全站共用的地基：Result 信封兼容、鉴权依赖链（Bearer 与 Cookie 两条通道）、
SQLAlchemy 业务读、以及 main.py 的路由挂载。后续每个域迁移都复用这套断言形状。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.business.itinerary import router
from app.common import token_revocation
from app.common.envelope import ApiError, install_exception_handlers, ok
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base, CityGeo

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"  # 测试占位串，非真实凭据


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    engine = create_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    with db_session.session_scope() as s:
        # supported-cities 现以 city_geo 字典为源（V4 退役 poi_knowledge）
        s.add_all(
            [
                CityGeo(city_name="北京", country="中国", country_code="CN", is_domestic=True),
                CityGeo(city_name="杭州", country="中国", country_code="CN", is_domestic=True),
            ]
        )
    monkeypatch.setattr(deps.settings, "jwt_secret", SIGNING_MATERIAL)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 42, "username": "alice", "role": "user", "status": 1},
    )

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(router)
    yield TestClient(app)
    db_session.init_engine(None, None)


def _bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def test_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/itinerary/supported-cities").status_code == 401


def test_revoked_token_is_rejected(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(token_revocation, "is_revoked", lambda _t: True)
    assert client.get("/api/itinerary/supported-cities", headers=_bearer()).status_code == 401


def test_bearer_and_cookie_both_authenticate(client: TestClient) -> None:
    by_bearer = client.get("/api/itinerary/supported-cities", headers=_bearer())
    assert by_bearer.status_code == 200
    token = encode_token("alice", SIGNING_MATERIAL, 3600)
    client.cookies.set(deps.COOKIE_NAME, token)
    by_cookie = client.get("/api/itinerary/supported-cities")
    assert by_cookie.status_code == 200
    assert by_cookie.json()["data"] == by_bearer.json()["data"]


def test_envelope_matches_java_contract(client: TestClient) -> None:
    """前端 request.ts 判 code!==200 并整体取 res.data，信封形状不可变。"""
    body = client.get("/api/itinerary/supported-cities", headers=_bearer()).json()
    assert set(body) == {"code", "message", "data"}
    assert body["code"] == 200
    assert body["data"] == ["北京", "杭州"]  # 去重 + 按城市名排序


def test_api_error_maps_status_into_both_http_and_body() -> None:
    """BizException.code 兼作 HTTP 状态码（Java GlobalExceptionHandler 语义）必须保住。"""
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/_probe")
    def _probe():
        raise ApiError(404, "行程不存在")

    response = TestClient(app).get("/_probe")
    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "行程不存在", "data": None}


def test_ok_helper_shape() -> None:
    assert ok(["a"]) == {"code": 200, "message": "success", "data": ["a"]}


def test_business_router_mounted_on_real_app() -> None:
    """回归防线：main.py 若漏挂 router，端点会 404 而单域测试仍然全绿。"""
    import main

    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/api/itinerary/supported-cities" in paths
    assert "/api/agent/v1/chat-turn" in paths  # 既有 agent 路由不能被挂载改动弄丢
