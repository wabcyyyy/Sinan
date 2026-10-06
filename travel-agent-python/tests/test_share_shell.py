"""分享卡外壳（P1-5）：/s/{token} 的 og 注入与降级路径。

断言焦点：
1. inject_og_meta 纯函数：替换 <title>、<head> 后插入 og:*、用户内容转义、
   异形壳原样返回（宁可不注入也不产出坏 HTML）；
2. 好 token：匿名 200 HTML，og:title 含行程标题，Cache-Control no-cache；
3. 坏 token：200 **未注入**原壳（坏链交给 SPA 错误态渲染，不给爬虫记 404 死链）；
4. 未配置 frontend_shell_url：302 回首页（分享链接绝不 500）。
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.business.share_shell import router as share_shell_router
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import Base, ItineraryMain
from app.services import share_card, share_service, state_and_sessions

CANNED_SHELL = (
    '<!doctype html>\n<html lang="zh-CN">\n<head>\n'
    '<title>司南 Sinan</title>\n<script src="/assets/index-abc123.js"></script>\n'
    '</head>\n<body><div id="app"></div></body>\n</html>'
)


@pytest.fixture(autouse=True)
def _shell_ready(monkeypatch):
    """壳缓存预热 + 能力开启：跳过真实 HTTP 取壳，测试只关心注入与路由行为。"""
    monkeypatch.setattr(settings, "frontend_shell_url", "http://caddy/index.html")
    share_card._shell_cache = (time.monotonic() + 999, CANNED_SHELL)
    yield
    share_card.reset_for_tests()


@pytest.fixture
def client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'share_shell.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    state_and_sessions.reset_for_tests()
    with db_session.session_scope() as session:
        session.add(
            ItineraryMain(
                user_id=42,
                title="杭州3日游<script>",
                city="杭州",
                start_date=date(2026, 4, 1),
                end_date=date(2026, 4, 3),
                days=3,
                persons=2,
                budget=Decimal("3000.00"),
                status=2,
            )
        )
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(share_shell_router)
    yield TestClient(app)
    db_session.init_engine(None, None)
    state_and_sessions.reset_for_tests()


def _create_token() -> str:
    with db_session.session_scope() as session:
        trip = session.query(ItineraryMain).one()
        trip_id = trip.id
    return str(share_service.create_share(42, trip_id, 7)["shareToken"])


# ---------- inject_og_meta 纯函数 ----------


def test_inject_replaces_title_and_adds_og() -> None:
    out = share_card.inject_og_meta(CANNED_SHELL, "杭州3日游", "杭州·3天行程 · 司南 Sinan")
    assert "<title>杭州3日游</title>" in out
    assert '<meta property="og:title" content="杭州3日游">' in out
    assert '<meta property="og:site_name" content="司南 Sinan">' in out
    # og 标签落在 <head> 内（插在 <head> 开标签之后）
    assert out.index('<meta property="og:title"') < out.index("</head>")


def test_inject_escapes_user_content() -> None:
    out = share_card.inject_og_meta(CANNED_SHELL, '<img src=x>"', "d")
    assert '<meta property="og:title" content="&lt;img src=x&gt;&quot;">' in out


def test_inject_without_head_returns_untouched() -> None:
    shell = "<p>no head here</p>"
    assert share_card.inject_og_meta(shell, "t", "d") == shell


# ---------- 端点行为 ----------


def test_valid_token_serves_injected_shell(client: TestClient) -> None:
    token = _create_token()
    response = client.get(f"/s/{token}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    # 标题含 <script> 的用户内容必须以转义形态出现，绝不能拼出可执行标签
    assert "杭州3日游&lt;script&gt;" in response.text
    assert "<title>杭州3日游&lt;script&gt;</title>" in response.text
    assert "杭州·3天行程" in response.text


def test_invalid_token_serves_plain_shell(client: TestClient) -> None:
    response = client.get("/s/not-a-real-token")
    assert response.status_code == 200
    assert "og:title" not in response.text
    assert "<title>司南 Sinan</title>" in response.text


def test_disabled_feature_redirects_home(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "frontend_shell_url", "")
    share_card.reset_for_tests()
    response = client.get("/s/whatever", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/"


def test_fetch_failure_without_cache_degrades(monkeypatch) -> None:
    share_card.reset_for_tests()

    def _raise(_url: str, **_kwargs: object) -> None:
        raise httpx.ConnectError("edge down")

    monkeypatch.setattr(share_card.httpx, "get", _raise)
    assert share_card.shared_shell_html("any") is None
