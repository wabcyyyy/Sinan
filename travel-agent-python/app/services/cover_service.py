"""封面：图库搜索代理 / 快照落盘 / 压缩（SPEC v2.3 §6.1–6.3，S1）。

三条纪律（做错即回归）：
1. **只信服务端解析的 coverRef**：客户端传来的 URL 一律不下载（防 SSRF/白名单绕过），
   Unsplash 的下载地址由服务端调 `GET /photos/{id}` 解析；
2. **选定即 snapshot**：下载 → 压缩 → 写 `{uploads_dir}/covers/{userId}/`，
   DB 只存应用路径 `/api/uploads/...`，绝不把第三方 CDN URL 当唯一真相；
3. **写路径守单列纪律**（§4 纪律 1）：`update(...).values(...)` 列级写 + 精确 evict，
   整行写会冲掉异步生成链路的并发回写。

无 GC（明确取舍）：本期不实现孤儿回收——恢复默认/删除行程都不删文件，README 如实写。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from PIL import Image
from sqlalchemy import update

from app.common.config import settings
from app.common.envelope import ApiError
from app.common.external_client import redact_secrets
from app.common.http_client import IMAGE_USER_AGENT, image_client
from app.db.models import ItineraryMain
from app.db.session import session_scope
from app.services import itinerary_query
from app.services.image_proxy import MAX_IMAGE_BYTES, is_safe_image_url

logger = logging.getLogger(__name__)

UNSPLASH_SEARCH_URL = "https://api.unsplash.com/search/photos"
UNSPLASH_PHOTO_URL = "https://api.unsplash.com/photos/{ref}"
UNSPLASH_LICENSE = "Unsplash License"
# cover 下载入口比通用代理白名单更窄：只接受 Unsplash 图床（别照抄宽表）
ALLOWED_COVER_HOSTS = ("images.unsplash.com",)

COVER_MAX_DIM = 1600
COVER_TARGET_BYTES = 400 * 1024
JPEG_QUALITIES = (82, 75, 68, 60, 50)

_UPLOAD_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png"}

# 进程内有界 LRU（上限 2048）：搜索预览是短时体验缓存，刻意**不进 Redis**——
# 共享缓存命名空间 py: 只归详情缓存（E5）。空结果短 TTL 防匿名穿透打爆上游配额。
_SEARCH_CACHE_MAX = 2048
_SEARCH_CACHE_TTL = 600.0
_SEARCH_CACHE_EMPTY_TTL = 120.0
_search_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


# ---------- 搜索代理 ----------


def search_covers(query: str, page: int, per_page: int, provider: str) -> dict[str, Any]:
    if provider != "unsplash":
        raise ApiError(400, f"暂不支持的封面图库：{provider}")
    key = settings.unsplash_access_key
    if not key:
        raise ApiError(400, "封面图库未配置（UNSPLASH_ACCESS_KEY）")
    keyword = (query or "").strip()
    if not keyword:
        raise ApiError(400, "搜索词不能为空")

    cache_key = f"{keyword}|{page}|{per_page}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        response = image_client().get(
            UNSPLASH_SEARCH_URL,
            params={
                "query": keyword,
                "page": page,
                "per_page": per_page,
                "orientation": "landscape",
                "client_id": key,  # key 绝不下发前端、不进 bundle
            },
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        # 异常文本内嵌含 client_id 的完整 URL，进日志前必须脱敏（P1-5）
        logger.warning("unsplash search failed: %s", redact_secrets(exc))
        raise ApiError(502, "图库检索失败，请稍后再试") from exc

    items = [_map_search_item(row) for row in (body.get("results") or [])]
    payload: dict[str, Any] = {"total": int(body.get("total") or 0), "items": items}
    _cache_put(cache_key, payload, _SEARCH_CACHE_EMPTY_TTL if not items else _SEARCH_CACHE_TTL)
    return payload


def _map_search_item(row: dict[str, Any]) -> dict[str, Any]:
    urls = row.get("urls") or {}
    user = row.get("user") or {}
    return {
        "unsplashId": row.get("id"),
        "thumb": urls.get("small") or urls.get("thumb") or urls.get("regular"),
        "regular": urls.get("regular"),
        "width": row.get("width"),
        "height": row.get("height"),
        "author": user.get("name"),
        "authorUrl": (user.get("links") or {}).get("html"),
        "license": UNSPLASH_LICENSE,
        "downloadTrackUrl": (row.get("links") or {}).get("download_location"),
    }


def _cache_get(key: str) -> dict[str, Any] | None:
    item = _search_cache.get(key)
    if item is None:
        return None
    expires, payload = item
    if expires < time.time():
        _search_cache.pop(key, None)
        return None
    _search_cache.move_to_end(key)
    return payload


def _cache_put(key: str, payload: dict[str, Any], ttl: float) -> None:
    _search_cache[key] = (time.time() + ttl, payload)
    _search_cache.move_to_end(key)
    while len(_search_cache) > _SEARCH_CACHE_MAX:
        _search_cache.popitem(last=False)


# ---------- 选定：unsplash snapshot ----------


def set_cover_unsplash(user_id: int, itinerary_id: int, unsplash_id: str) -> dict[str, Any]:
    ref = (unsplash_id or "").strip()
    if not ref:
        raise ApiError(400, "unsplashId 不能为空")
    itinerary_query.find_owned_main(user_id, itinerary_id)  # 归属失败一律 404

    photo = resolve_unsplash_photo(ref)
    _trigger_download(photo.get("downloadTrackUrl"))
    raw = _download_cover(str(photo.get("url") or ""))
    try:
        compressed = compress_cover(raw)
    except ValueError as exc:
        logger.warning("cover decode failed: %s", exc)
        raise ApiError(502, "封面下载失败，请重试或改上传") from exc

    filename = f"{hashlib.sha256(ref.encode('utf-8')).hexdigest()[:16]}.jpg"
    cover_url = _write_cover(user_id, filename, compressed)
    credit = {
        "author": photo.get("author"),
        "authorUrl": photo.get("authorUrl"),
        "license": UNSPLASH_LICENSE,
        "source": "unsplash",
    }
    _write_cover_columns(
        user_id,
        itinerary_id,
        cover_url=cover_url,
        cover_source="unsplash",
        cover_ref=ref,
        cover_credit=json.dumps(credit, ensure_ascii=False),
    )
    return itinerary_query.detail(user_id, itinerary_id)


def resolve_unsplash_photo(ref: str) -> dict[str, Any]:
    """服务端解析下载地址：**只信这一步的结果**，客户端传 URL 一律不下载。"""
    key = settings.unsplash_access_key
    if not key:
        raise ApiError(400, "封面图库未配置（UNSPLASH_ACCESS_KEY）")
    try:
        response = image_client().get(UNSPLASH_PHOTO_URL.format(ref=ref), params={"client_id": key})
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.warning("unsplash photo resolve failed: %s", redact_secrets(exc))
        raise ApiError(502, "封面下载失败，请重试或改上传") from exc

    urls = body.get("urls") or {}
    url = urls.get("regular") or urls.get("full") or urls.get("small")
    if not url:
        raise ApiError(502, "封面下载失败，请重试或改上传")
    user = body.get("user") or {}
    return {
        "url": str(url),
        "author": user.get("name"),
        "authorUrl": (user.get("links") or {}).get("html"),
        "downloadTrackUrl": (body.get("links") or {}).get("download_location"),
    }


def _trigger_download(url: str | None) -> None:
    """Unsplash 下载触发（合规要求）；失败仅记日志，不阻断（SPEC §6.3 step 3）。"""
    if not url:
        return
    try:
        image_client().get(str(url), params={"client_id": settings.unsplash_access_key})
    except Exception as exc:
        logger.info("unsplash download trigger failed (ignored): %s", redact_secrets(exc))


def _download_cover(url: str) -> bytes:
    if not is_safe_image_url(url) or urlsplit(url).hostname not in ALLOWED_COVER_HOSTS:
        raise ApiError(502, "封面下载失败，请重试或改上传")
    try:
        response = image_client().get(url, headers={"User-Agent": IMAGE_USER_AGENT})
        response.raise_for_status()
    except Exception as exc:
        logger.warning("cover download failed: %s", exc)
        raise ApiError(502, "封面下载失败，请重试或改上传") from exc
    body = response.content
    if not body or len(body) > MAX_IMAGE_BYTES:
        raise ApiError(502, "封面下载失败，请重试或改上传")
    return body


# ---------- 压缩与落盘 ----------


def _decode_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ValueError("图片无法解码") from exc
    return image


def compress_cover(data: bytes) -> bytes:
    """长边 ≤1600、JPEG q≈0.82 起、目标 ≤400KB（逐档降质直到达标或到底）。"""
    image = _decode_image(data).convert("RGB")
    image.thumbnail((COVER_MAX_DIM, COVER_MAX_DIM))
    buffer = io.BytesIO()
    for quality in JPEG_QUALITIES:
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True)
        if buffer.tell() <= COVER_TARGET_BYTES:
            break
    return buffer.getvalue()


def _cover_dir(user_id: int) -> Path:
    target = Path(settings.uploads_dir) / "covers" / str(user_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _write_cover(user_id: int, filename: str, data: bytes) -> str:
    target = _cover_dir(user_id) / filename
    if not target.exists():  # 内容哈希名 → 去重（同图不重复写盘）
        target.write_bytes(data)
    return f"/api/uploads/covers/{user_id}/{filename}"


def _write_cover_columns(user_id: int, itinerary_id: int, **values: Any) -> None:
    with session_scope() as session:
        session.execute(update(ItineraryMain).where(ItineraryMain.id == itinerary_id).values(**values))
    itinerary_query.evict_detail(user_id, itinerary_id)


# ---------- 其余两个来源 ----------


def set_cover_default(user_id: int, itinerary_id: int) -> dict[str, Any]:
    """恢复默认：四个 cover 列全部置 NULL（DB 不存 default 字面量）；**不删**已落盘文件。"""
    itinerary_query.find_owned_main(user_id, itinerary_id)
    _write_cover_columns(user_id, itinerary_id, cover_url=None, cover_source=None, cover_ref=None, cover_credit=None)
    return itinerary_query.detail(user_id, itinerary_id)


def read_upload_capped(source: BinaryIO, limit_bytes: int) -> bytes:
    """流式读取上传体并在超限时立刻中止（S0-3）：FastAPI 不限制 multipart 体积，
    一次性 read() 会让超大文件把内存打爆。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit_bytes:
            raise ApiError(413, "图片不能超过 5MB")
        chunks.append(chunk)
    return b"".join(chunks)


def set_cover_upload(
    user_id: int,
    itinerary_id: int,
    *,
    filename: str | None,
    content_type: str | None,
    data: bytes,
) -> dict[str, Any]:
    itinerary_query.find_owned_main(user_id, itinerary_id)
    extension = _UPLOAD_EXTENSIONS.get((content_type or "").split(";")[0].strip().lower())
    if extension is None:
        raise ApiError(400, "仅支持 jpeg/png 图片")
    try:
        image = _decode_image(data)
        image_format = (image.format or "").upper()
    except ValueError as exc:
        raise ApiError(400, "图片文件已损坏或不是有效图片") from exc
    if image_format not in {"JPEG", "PNG"}:
        raise ApiError(400, "仅支持 jpeg/png 图片")

    # 客户端文件名一律不使用（`..` 过滤因此无必要性）：uuid 随机名
    stored_name = f"{uuid.uuid4().hex}{extension}"
    cover_url = _write_cover(user_id, stored_name, data)
    _write_cover_columns(
        user_id,
        itinerary_id,
        cover_url=cover_url,
        cover_source="upload",
        cover_ref=None,
        cover_credit=json.dumps({"source": "upload"}, ensure_ascii=False),
    )
    return itinerary_query.detail(user_id, itinerary_id)
