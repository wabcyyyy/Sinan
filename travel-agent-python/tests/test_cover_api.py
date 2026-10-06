"""封面与上传文件访问（SPEC v2.3 §6.1–6.4，S1 / S0-3）。

覆盖：搜索代理（未配 key 400 / 上游失败 502 / 映射与缓存）、snapshot 落盘
（内容哈希名、DB 只存应用路径、credit 必写、精确 evict）、`_download_cover` 只收
Unsplash 图床、default 四列置 NULL 且不删文件、上传（类型白名单 / 5MB 流式 413 /
有效图落盘）、静态访问（匿名可读、分档缓存头、miss 为 PlainText 404、穿越拒绝）。

上游与下载全部打桩（单测不出网）；下载返回 Pillow 现场生成的真实 JPEG/PNG，
压缩链路走真代码。
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api import deps
from app.api.business import uploads as uploads_module
from app.api.business.covers import router as covers_router
from app.api.business.itinerary import router as itinerary_router
from app.api.business.uploads import router as uploads_router
from app.common.envelope import ApiError, install_exception_handlers
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base, ItineraryMain
from app.services import cover_service

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"  # 测试占位串，非真实凭据


@pytest.fixture
def uploads_dir(tmp_path, monkeypatch) -> Path:
    target = tmp_path / "uploads"
    target.mkdir()
    monkeypatch.setattr(cover_service.settings, "uploads_dir", str(target))
    monkeypatch.setattr(cover_service.settings, "unsplash_access_key", "example-unsplash-key")
    cover_service._search_cache.clear()
    return target


@pytest.fixture
def client(tmp_path, uploads_dir, monkeypatch) -> TestClient:
    engine = create_engine(f"sqlite:///{tmp_path / 'cover.db'}")
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
    app.include_router(itinerary_router)
    app.include_router(covers_router)
    app.include_router(uploads_router)
    yield TestClient(app)
    db_session.init_engine(None, None)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def _seed_trip() -> int:
    with db_session.session_scope() as session:
        main = ItineraryMain(user_id=42, title="杭州 3 日", city="杭州", days=3, persons=1)
        session.add(main)
        session.flush()
        return main.id


def _jpeg_bytes(size: tuple[int, int] = (2400, 1600)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (30, 90, 140)).save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def _png_bytes(size: tuple[int, int] = (60, 40)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (12, 120, 110)).save(buffer, "PNG")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, payload: dict, calls: list) -> None:
        self._payload = payload
        self._calls = calls

    def get(self, url: str, **kwargs) -> _FakeResponse:
        self._calls.append(url)
        return _FakeResponse(self._payload)


# ---------- 搜索代理 ----------


def test_search_requires_key(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(cover_service.settings, "unsplash_access_key", "")
    response = client.get("/api/covers/search?q=杭州", headers=_headers())
    assert response.status_code == 400
    assert "UNSPLASH_ACCESS_KEY" in response.json()["message"]


def test_search_requires_auth(client: TestClient) -> None:
    assert client.get("/api/covers/search?q=杭州").status_code == 401


def test_search_maps_items_and_caches(client: TestClient, monkeypatch) -> None:
    payload = {
        "total": 7,
        "results": [
            {
                "id": "abc",
                "width": 4000,
                "height": 2667,
                "urls": {"small": "https://images.unsplash.com/s", "regular": "https://images.unsplash.com/r"},
                "user": {"name": "Ada", "links": {"html": "https://unsplash.com/@ada"}},
                "links": {"download_location": "https://api.unsplash.com/photos/abc/download"},
            }
        ],
    }
    calls: list[str] = []
    monkeypatch.setattr(cover_service, "image_client", lambda: _FakeClient(payload, calls))

    first = client.get("/api/covers/search?q=西湖", headers=_headers()).json()["data"]
    second = client.get("/api/covers/search?q=西湖", headers=_headers()).json()["data"]
    assert first == second
    assert len(calls) == 1, "第二次命中进程内 LRU，不应再打上游"
    item = first["items"][0]
    assert item["unsplashId"] == "abc"
    assert item["author"] == "Ada" and item["license"] == "Unsplash License"
    assert first["total"] == 7


def test_search_upstream_failure_is_502(client: TestClient, monkeypatch, caplog) -> None:
    class _Boom:
        def get(self, url, **kwargs):
            raise RuntimeError(
                "500 for url 'https://api.unsplash.com/search/photos?query=x&client_id=example-unsplash-key'"
            )

    monkeypatch.setattr(cover_service, "image_client", lambda: _Boom())
    with caplog.at_level("WARNING", logger="app.services.cover_service"):
        response = client.get("/api/covers/search?q=西湖", headers=_headers())
    assert response.status_code == 502
    assert "example-unsplash-key" not in caplog.text, "client_id 密钥不得泄漏进日志（P1-5）"
    assert "client_id=<redacted>" in caplog.text, "脱敏后保留参数名，便于排障"


# ---------- 设定封面（unsplash snapshot） ----------


def test_set_unsplash_cover_snapshots_locally(client: TestClient, uploads_dir: Path, monkeypatch) -> None:
    trip_id = _seed_trip()
    monkeypatch.setattr(
        cover_service,
        "resolve_unsplash_photo",
        lambda ref: {
            "url": "https://images.unsplash.com/photo-x",
            "author": "Ada",
            "authorUrl": "https://unsplash.com/@ada",
            "downloadTrackUrl": None,  # 不触发 download trigger 的联网分支
        },
    )
    monkeypatch.setattr(cover_service, "_download_cover", lambda url: _jpeg_bytes())
    evicted: list[tuple[int, int]] = []
    monkeypatch.setattr(cover_service.itinerary_query, "evict_detail", lambda uid, iid: evicted.append((uid, iid)))

    response = client.post(
        f"/api/itinerary/{trip_id}/cover",
        json={"source": "unsplash", "unsplashId": "abc"},
        headers=_headers(),
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]

    filename = f"{hashlib.sha256(b'abc').hexdigest()[:16]}.jpg"
    assert data["coverUrl"] == f"/api/uploads/covers/42/{filename}"
    assert data["coverSource"] == "unsplash"
    written = uploads_dir / "covers" / "42" / filename
    assert written.is_file(), "选定即 snapshot：文件必须已落盘"

    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, trip_id)
        assert row.cover_source == "unsplash" and row.cover_ref == "abc"
        credit = json.loads(row.cover_credit)
        assert credit["author"] == "Ada" and credit["source"] == "unsplash"
    assert (42, trip_id) in evicted, "写路径必须精确失效详情缓存"


def test_compress_cover_resizes_long_edge() -> None:
    compressed = cover_service.compress_cover(_jpeg_bytes())
    with Image.open(io.BytesIO(compressed)) as image:
        assert max(image.size) <= cover_service.COVER_MAX_DIM
        assert image.format == "JPEG"


def test_resolve_unsplash_failure_is_502_and_redacts_key(client: TestClient, monkeypatch, caplog) -> None:
    class _Boom:
        def get(self, url, **kwargs):
            raise RuntimeError("500 for url 'https://api.unsplash.com/photos/abc?client_id=example-unsplash-key'")

    monkeypatch.setattr(cover_service, "image_client", lambda: _Boom())
    with caplog.at_level("WARNING", logger="app.services.cover_service"):
        with pytest.raises(ApiError) as exc:
            cover_service.resolve_unsplash_photo("abc")
    assert exc.value.status == 502
    assert "example-unsplash-key" not in caplog.text, "client_id 密钥不得泄漏进日志（P1-5）"
    assert "client_id=<redacted>" in caplog.text


def test_download_trigger_failure_ignored_and_redacts_key(client: TestClient, monkeypatch, caplog) -> None:
    class _Boom:
        def get(self, url, **kwargs):
            raise RuntimeError(
                "500 for url 'https://api.unsplash.com/photos/abc/download?client_id=example-unsplash-key'"
            )

    monkeypatch.setattr(cover_service, "image_client", lambda: _Boom())
    with caplog.at_level("INFO", logger="app.services.cover_service"):
        cover_service._trigger_download("https://api.unsplash.com/photos/abc/download")
    assert "example-unsplash-key" not in caplog.text, "client_id 密钥不得泄漏进日志（P1-5）"
    assert "client_id=<redacted>" in caplog.text


def test_download_cover_rejects_non_unsplash_host() -> None:
    for url in ("https://evil.example/x.jpg", "https://images.pexels.com/x.jpg", "ftp://images.unsplash.com/x.jpg"):
        with pytest.raises(ApiError) as exc:
            cover_service._download_cover(url)
        assert exc.value.status == 502


def test_unsplash_cover_requires_id(client: TestClient) -> None:
    trip_id = _seed_trip()
    response = client.post(f"/api/itinerary/{trip_id}/cover", json={"source": "unsplash"}, headers=_headers())
    assert response.status_code == 400


def test_cover_unknown_source_is_400(client: TestClient) -> None:
    trip_id = _seed_trip()
    response = client.post(f"/api/itinerary/{trip_id}/cover", json={"source": "pexels"}, headers=_headers())
    assert response.status_code == 400


# ---------- 上传 ----------


def test_upload_cover_ok(client: TestClient, uploads_dir: Path) -> None:
    trip_id = _seed_trip()
    response = client.post(
        f"/api/itinerary/{trip_id}/cover/upload",
        headers=_headers(),
        files={"file": ("selfie.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["coverSource"] == "upload"
    assert data["coverUrl"].startswith("/api/uploads/covers/42/")
    assert data["coverUrl"].endswith(".png")
    assert (uploads_dir / data["coverUrl"].removeprefix("/api/uploads/")).is_file()


def test_upload_rejects_wrong_type(client: TestClient) -> None:
    trip_id = _seed_trip()
    response = client.post(
        f"/api/itinerary/{trip_id}/cover/upload",
        headers=_headers(),
        files={"file": ("x.gif", b"GIF89a-not-an-image", "image/gif")},
    )
    assert response.status_code == 400


def test_upload_rejects_oversize_413(client: TestClient) -> None:
    trip_id = _seed_trip()
    limit = cover_service.settings.cover_upload_max_bytes
    response = client.post(
        f"/api/itinerary/{trip_id}/cover/upload",
        headers=_headers(),
        files={"file": ("big.png", b"0" * (limit + 1), "image/png")},
    )
    assert response.status_code == 413


def test_upload_requires_auth(client: TestClient) -> None:
    trip_id = _seed_trip()
    response = client.post(
        f"/api/itinerary/{trip_id}/cover/upload",
        files={"file": ("selfie.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 401


# ---------- 恢复默认 ----------


def test_default_clears_columns_and_keeps_file(client: TestClient, uploads_dir: Path) -> None:
    trip_id = _seed_trip()
    uploaded = client.post(
        f"/api/itinerary/{trip_id}/cover/upload",
        headers=_headers(),
        files={"file": ("selfie.png", _png_bytes(), "image/png")},
    ).json()["data"]
    stored = uploads_dir / uploaded["coverUrl"].removeprefix("/api/uploads/")

    response = client.post(f"/api/itinerary/{trip_id}/cover", json={"source": "default"}, headers=_headers())
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["coverUrl"] is None and data["coverSource"] is None
    with db_session.session_scope() as session:
        row = session.get(ItineraryMain, trip_id)
        assert row.cover_url is None and row.cover_source is None
        assert row.cover_ref is None and row.cover_credit is None
    assert stored.is_file(), "无 GC：恢复默认不删已落盘文件"


# ---------- 静态访问（§6.4） ----------


def test_uploads_served_anonymously_with_uuid_cache(client: TestClient, uploads_dir: Path) -> None:
    trip_id = _seed_trip()
    uploaded = client.post(
        f"/api/itinerary/{trip_id}/cover/upload",
        headers=_headers(),
        files={"file": ("selfie.png", _png_bytes(), "image/png")},
    ).json()["data"]

    anonymous = client.get(uploaded["coverUrl"])  # 无 Authorization：白名单放行
    assert anonymous.status_code == 200
    assert anonymous.headers["cache-control"] == "public, max-age=3600"
    assert anonymous.headers["content-type"].startswith("image/png")


def test_uploads_hash_name_is_immutable(client: TestClient, uploads_dir: Path) -> None:
    target = uploads_dir / "covers" / "42"
    target.mkdir(parents=True)
    (target / f"{'a' * 16}.jpg").write_bytes(_jpeg_bytes((40, 30)))
    response = client.get(f"/api/uploads/covers/42/{'a' * 16}.jpg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "public, max-age=604800, immutable"


def test_uploads_miss_is_plain_404(client: TestClient) -> None:
    response = client.get("/api/uploads/covers/42/none.jpg")
    assert response.status_code == 404
    assert response.text == "not found"
    assert not response.headers["content-type"].startswith("application/json")


def test_resolve_upload_path_rejects_traversal(tmp_path, monkeypatch) -> None:
    base = tmp_path / "u"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("x")
    monkeypatch.setattr(uploads_module.settings, "uploads_dir", str(base))

    assert uploads_module.resolve_upload_path("../secret.txt") is None
    assert uploads_module.resolve_upload_path("covers/../../secret.txt") is None
    (base / "covers").mkdir()
    (base / "covers" / "ok.jpg").write_bytes(b"x")
    assert uploads_module.resolve_upload_path("covers/ok.jpg") is not None


def test_new_routers_mounted_on_real_app() -> None:
    """回归防线：business_routers 漏注册时端点会 404 而单域测试仍绿。"""
    import main

    paths = {getattr(route, "path", None) for route in main.app.routes}
    assert "/api/covers/search" in paths
    assert any(path and path.startswith("/api/uploads/") for path in paths)
