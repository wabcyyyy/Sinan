"""POI 实景照片多源解析（无高德版：只剩免 key / 图库源）。

源顺序按「拍的就是该点位」的精度排列，图库模糊检索只作最后兜底：
Wikipedia(zh/en/ja) → Wikimedia Commons → Unsplash → Pexels。
国内高德实拍源已移除（2026-09-15 去高德）：高德有 key/配额且属于项目要摆脱的
外部 API；移除后国内点位走维基/图库链，未命中由前端落本地分类占位图。

两条不可回退的纪律（都来自踩过的坑）：
1. **图库查询只准用点位名称、禁止携带城市词**——否则同一城市的所有卡片都会命中
   同一批泛化风景图；错误的图比没有图更伤观感。本文件有测试钉住这一点。
2. **有界 LRU + 空结果缓存**——匿名端点、高基数名称，无界缓存会被撑爆；
   未命中的名称若不留哨兵，每次开页面都会重放整条外网链路。

为什么 `app/agent/tools/impl.py` 里还有一份相似的图片链（R3-1 的结论，别再"顺手合并"）：
分层是 `api → services → agent`，**agent 不能反向 import services**。本文件服务匿名的
`GET /api/poi-photo`（services 面），生成链路要取图只能经 `tools.poi_image`（agent 面）。
两条链共用同一套纪律（那边的 docstring 指回这里），但共用代码要等"地点事实"这一层
落到 agent 侧、缓存有非 services 的落点之后才能合并——那是结构决策，不是复制粘贴。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict

import httpx

from app.common.config import settings
from app.common.external_client import redact_secrets
from app.common.http_client import WIKI_USER_AGENT, image_client

logger = logging.getLogger(__name__)

PHOTO_CACHE_MAX = 2048
WIKI_COOLDOWN_SECONDS = 5 * 60
_PAREN_RE = re.compile(r"[（(][^（）()]*[)）]")

_cache: OrderedDict[str, str] = OrderedDict()
_cache_lock = threading.Lock()
_wiki_cooldown_until = 0.0


def reset_state_for_tests() -> None:
    global _wiki_cooldown_until
    with _cache_lock:
        _cache.clear()
    _wiki_cooldown_until = 0.0


def _cache_get(key: str) -> str | None:
    with _cache_lock:
        if key not in _cache:
            return None
        _cache.move_to_end(key)
        return _cache[key]


def _cache_put(key: str, value: str) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > PHOTO_CACHE_MAX:
            _cache.popitem(last=False)


def wiki_available() -> bool:
    return time.time() >= _wiki_cooldown_until


def mark_wiki_down() -> None:
    global _wiki_cooldown_until
    _wiki_cooldown_until = time.time() + WIKI_COOLDOWN_SECONDS


def resolve_poi_photo(name: str, city: str) -> str:
    """返回图片 URL，未命中返回空串（结果含空串一起缓存，防穿透）。"""
    cache_key = f"{city}:{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    resolved = _do_resolve(name) or ""
    _cache_put(cache_key, resolved)
    return resolved


def _do_resolve(name: str) -> str | None:
    if wiki_available():
        url = _safe_wiki(wikipedia_photo, name)
        if url:
            return url
    if wiki_available():
        url = _safe_wiki(wikimedia_commons_photo, name)
        if url:
            return url
    url = _safe(lambda: unsplash_photo(name))
    if url:
        return url
    # 维基熔断期跳过 langlinks（同走维基网络），直接用原名快速失败
    query_name = english_search_name(name) if wiki_available() else (name or "").strip()
    return _safe(lambda: pexels_photo(query_name))


def _safe(fn, *args) -> str | None:
    try:
        return fn(*args)
    except Exception as exc:  # 单源失败不影响下一源；unsplash 的异常文本含 client_id（P1-5）
        logger.debug("photo source %s failed: %s", getattr(fn, "__name__", fn), redact_secrets(exc))
        return None


def _safe_wiki(fn, *args) -> str | None:
    """维基系连接异常才进冷却；HTTP 200 但无图不算故障，避免误伤可达环境。"""
    try:
        return fn(*args)
    except httpx.TransportError:
        mark_wiki_down()
        return None
    except Exception:
        return None


def _wikipedia_title(name: str) -> str:
    return _PAREN_RE.sub("", name or "").strip()


def wikipedia_photo(name: str) -> str | None:
    title = _wikipedia_title(name)
    if not title:
        return None
    for lang in ("zh", "en", "ja"):
        response = image_client().get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "generator": "search",
                "gsrsearch": title,
                "gsrlimit": 1,
                "prop": "pageimages",
                "piprop": "thumbnail",
                "pithumbsize": 640,
                "format": "json",
            },
            headers={"User-Agent": WIKI_USER_AGENT},
        )
        response.raise_for_status()
        pages = (response.json().get("query") or {}).get("pages") or {}
        for page in pages.values():
            thumb = ((page or {}).get("thumbnail") or {}).get("source")
            if thumb:
                return str(thumb)
    return None


def wikimedia_commons_photo(name: str) -> str | None:
    title = _wikipedia_title(name)
    if not title:
        return None
    response = image_client().get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": title,
            "gsrnamespace": 6,
            "gsrlimit": 1,
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": 640,
            "format": "json",
        },
        headers={"User-Agent": WIKI_USER_AGENT},
    )
    response.raise_for_status()
    pages = (response.json().get("query") or {}).get("pages") or {}
    for page in pages.values():
        info = ((page or {}).get("imageinfo") or [{}])[0]
        return str(info.get("thumburl") or info.get("url") or "") or None
    return None


def english_search_name(name: str) -> str:
    """非 ASCII 名称先经 Wikipedia langlinks 解析英文标题，供 Pexels/Unsplash 使用。"""
    raw = (name or "").strip()
    if not raw or raw.isascii():
        return raw
    for lang in ("zh", "ja"):
        try:
            response = image_client().get(
                f"https://{lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "titles": raw,
                    "prop": "langlinks",
                    "lllang": "en",
                    "format": "json",
                },
                headers={"User-Agent": WIKI_USER_AGENT},
            )
            response.raise_for_status()
            pages = (response.json().get("query") or {}).get("pages") or {}
            for page in pages.values():
                links = (page or {}).get("langlinks") or []
                if links and links[0].get("*"):
                    title = str(links[0]["*"]).strip()
                    if title:
                        return title
        except Exception as exc:
            logger.debug("langlinks lookup failed (%s): %s", lang, exc)
    return raw


def unsplash_photo(name: str) -> str | None:
    key = settings.unsplash_access_key
    query = (name or "").strip()
    if not key or not query:
        return None
    response = image_client().get(
        "https://api.unsplash.com/search/photos",
        params={
            "query": query,  # 只传名称：携带城市词会命中泛化风景图（纪律 1）
            "per_page": 1,
            "orientation": "landscape",
            "client_id": key,
        },
    )
    response.raise_for_status()
    results = response.json().get("results") or []
    if not results:
        return None
    regular = (results[0].get("urls") or {}).get("regular")
    return str(regular) if regular else None


def pexels_photo(query: str) -> str | None:
    key = settings.pexels_access_key
    query = (query or "").strip()
    if not key or not query:
        return None
    response = image_client().get(
        "https://api.pexels.com/v1/search",
        params={"query": query, "per_page": 1, "orientation": "landscape"},
        # Pexels 文档要求裸 API key，不是 Bearer
        headers={"Authorization": key},
    )
    response.raise_for_status()
    photos = response.json().get("photos") or []
    if not photos:
        return None
    src = photos[0].get("src") or {}
    url = src.get("large") or src.get("medium")
    return str(url) if url else None
