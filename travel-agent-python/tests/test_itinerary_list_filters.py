"""列表筛选（`GET /api/itinerary?view=&q=`，SPEC v2.3 §6.5 / S0-9）。

`view=active` 必须包含 `gen_state IS NULL` 的迁移前存量（SPEC E9）：漏掉会让老行程
从「计划中」里凭空消失。本文件按五档 + 关键词逐格钉住，直接打路由（含鉴权链）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.business.itinerary import router
from app.common.envelope import install_exception_handlers
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base, ItineraryMain

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"  # 测试占位串，非真实凭据


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    engine = create_engine(f"sqlite:///{tmp_path / 'list.db'}")
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
    app.include_router(router)
    yield TestClient(app)
    db_session.init_engine(None, None)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def _seed() -> None:
    rows = [
        # (title, city, gen_state, favorite, archived)
        ("杭州 3 日 · 老行程", "杭州", None, 0, 0),  # 迁移前存量：active 必须包含
        ("北京 2 日 · 生成中", "北京", "GENERATING", 0, 0),
        ("上海 4 日 · 已完成", "上海", "COMPLETED", 1, 0),  # 收藏 + done
        ("成都 1 日 · 已归档", "成都", "COMPLETED", 1, 1),  # 归档（不出现在 all/favorite）
    ]
    with db_session.session_scope() as session:
        for title, city, gen_state, favorite, archived in rows:
            session.add(
                ItineraryMain(
                    user_id=42,
                    title=title,
                    city=city,
                    days=1,
                    persons=1,
                    gen_state=gen_state,
                    favorite=favorite,
                    archived=archived,
                )
            )


def _titles(client: TestClient, query: str = "") -> list[str]:
    response = client.get(f"/api/itinerary{query}", headers=_headers())
    assert response.status_code == 200, response.text
    return [row["title"] for row in response.json()["data"]]


def test_view_all_excludes_archived(client: TestClient) -> None:
    _seed()
    titles = _titles(client)
    assert "成都 1 日 · 已归档" not in titles
    assert len(titles) == 3


def test_view_active_includes_null_gen_state(client: TestClient) -> None:
    _seed()
    titles = _titles(client, "?view=active")
    assert "杭州 3 日 · 老行程" in titles, "gen_state 为 NULL 的存量必须算『计划中』（E9）"
    assert "北京 2 日 · 生成中" in titles
    assert "上海 4 日 · 已完成" not in titles


def test_view_done_requires_completed(client: TestClient) -> None:
    _seed()
    assert _titles(client, "?view=done") == ["上海 4 日 · 已完成"]


def test_view_favorite_excludes_archived(client: TestClient) -> None:
    _seed()
    assert _titles(client, "?view=favorite") == ["上海 4 日 · 已完成"]


def test_view_archived_only_archived(client: TestClient) -> None:
    _seed()
    assert _titles(client, "?view=archived") == ["成都 1 日 · 已归档"]


def test_query_matches_title_or_city(client: TestClient) -> None:
    _seed()
    assert _titles(client, "?q=上海") == ["上海 4 日 · 已完成"]
    assert _titles(client, "?q=已归档") == [], "归档行不参与默认视图的搜索"


def test_unknown_view_is_400(client: TestClient) -> None:
    _seed()
    response = client.get("/api/itinerary?view=weird", headers=_headers())
    assert response.status_code == 400


def _row_id(client: TestClient, title: str) -> int:
    rows = client.get("/api/itinerary", headers=_headers()).json()["data"]
    return next(row["id"] for row in rows if row["title"] == title)


def test_favorite_toggle(client: TestClient) -> None:
    _seed()
    trip_id = _row_id(client, "北京 2 日 · 生成中")
    response = client.post(f"/api/itinerary/{trip_id}/favorite", json={"favorite": True}, headers=_headers())
    assert response.status_code == 200
    assert "北京 2 日 · 生成中" in _titles(client, "?view=favorite")


def test_archive_toggle_moves_row_out_of_default_view(client: TestClient) -> None:
    _seed()
    trip_id = _row_id(client, "北京 2 日 · 生成中")
    response = client.post(f"/api/itinerary/{trip_id}/archive", json={"archived": True}, headers=_headers())
    assert response.status_code == 200
    assert "北京 2 日 · 生成中" not in _titles(client)
    assert "北京 2 日 · 生成中" in _titles(client, "?view=archived")
