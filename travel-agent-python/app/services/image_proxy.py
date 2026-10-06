"""同源图片代理：SSRF 白名单 + 类型校验。

存在的意义：把 Unsplash/Wikimedia/Pexels 的图片转成同源响应，避免浏览器被热链
策略、防盗链或混合内容拦截；同时保证**只有白名单图床**能被服务端代取，防止
`?url=http://169.254.169.254/...` 这类内网探测。

高德/autonavi 图床已从白名单移除（2026-09-15 去高德）：存量行里可能仍存着
`store.is.autonavi.com` 的旧 URL，代理会 400，前端按既有降级链落到占位图。

与 Java 的差异（有意为之）：8MB 上限是**流式**判定。Java 全量读进内存后才看大小，
"上限"因此只是拒绝、不是保护——一张超大原图仍会先把 worker 内存吃满。这里边读边累计，
越过阈值立刻中断并回 413（超限不是上游故障，不该伪装成 502）。

同源 SVG 一律拒绝（R1-2）：本服务没有 CSP，`image/svg+xml` 里可以带脚本，
把它以同源响应吐给浏览器，等于给任何能在白名单图站上放文件的人一次
"在你的源上执行 JS"的机会（HttpOnly Cookie 在那之后不再保护任何东西）。
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx
from fastapi import Response

from app.common.http_client import IMAGE_USER_AGENT, image_client

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 8 * 1024 * 1024
CACHE_CONTROL = "public, max-age=21600"  # 6 小时，与 Java CacheControl.maxAge(6h) 一致
# 上游没给 content-type 时按扩展名兜底；刻意不含 .svg（见模块 docstring）
_GUESSABLE = ((".png", "image/png"), (".webp", "image/webp"), (".gif", "image/gif"), (".avif", "image/avif"))
# 响应头说是图片还不够：前 1KB 里出现 svg/xml 标记就按脚本载体处理
_SNIFF_BYTES = 1024

_ALLOWED_EXACT = {
    "images.unsplash.com",
    "images.pexels.com",
    # images.weserv.nl 曾在此列，已删：它是"按 query 参数替你抓取任意目标"的
    # 图片代理，放行这个主机等于让白名单不再约束**实际被抓取的对象**
    # （`https://images.weserv.nl/?url=http://169.254.169.254/...` 直接通过校验）。
    # 全仓没有任何一处产出 weserv 图片地址，删它不损功能。
    "upload.wikimedia.org",
    "wikipedia.org",
}
_ALLOWED_SUFFIX = (
    ".unsplash.com",
    ".pexels.com",
    ".wikimedia.org",
    ".wikipedia.org",
)


def is_allowed_image_host(host: str) -> bool:
    normalized = (host or "").lower()
    if not normalized:
        return False
    return normalized in _ALLOWED_EXACT or any(normalized.endswith(suffix) for suffix in _ALLOWED_SUFFIX)


def is_safe_image_url(url: str) -> bool:
    """scheme 必须 http/https、必须有 host、且 host 命中白名单。

    刻意拒绝带 userinfo 的 URL（`https://allowed.host@evil/`）：`urlsplit.hostname`
    会正确返回 evil，但某些下游库会按不同方式解析，不给自己留歧义面。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if parts.username or parts.password:
        return False
    host = parts.hostname
    return bool(host) and is_allowed_image_host(host)


def guess_image_content_type(path: str | None) -> str:
    lowered = (path or "").lower()
    for suffix, media in _GUESSABLE:
        if lowered.endswith(suffix):
            return media
    return "image/jpeg"


def _carries_svg_markup(body: bytes) -> bool:
    """响应头说是图片不代表是位图：SVG 能带脚本，同源吐出即可执行。"""
    head = body[:_SNIFF_BYTES].lstrip().lower()
    return head.startswith(b"<?xml") or b"<svg" in head


def proxy_image(url: str) -> Response:
    """非白名单/非法 400、空 404、超限 413、非图片（含 SVG）或上游异常 502。"""
    if not is_safe_image_url(url):
        return Response(status_code=400)
    declared = ""
    chunks: list[bytes] = []
    total = 0
    try:
        with image_client().stream(
            "GET",
            url,
            headers={
                "User-Agent": IMAGE_USER_AGENT,
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            },
        ) as upstream:
            upstream.raise_for_status()
            declared = (upstream.headers.get("content-type") or "").split(";")[0].strip()
            for chunk in upstream.iter_bytes():
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    logger.warning(
                        "image proxy aborted oversized stream: >%d bytes from %s",
                        MAX_IMAGE_BYTES,
                        urlsplit(url).hostname,
                    )
                    return Response(status_code=413)
                chunks.append(chunk)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("image proxy fetch failed: %s", exc)
        return Response(status_code=502)

    body = b"".join(chunks)
    if not body:
        return Response(status_code=404)
    content_type = declared or guess_image_content_type(urlsplit(url).path)
    if not content_type.lower().startswith("image/") or content_type.lower() == "image/svg+xml":
        return Response(status_code=502)
    if _carries_svg_markup(body):
        logger.warning("image proxy rejected svg-capable payload from %s", urlsplit(url).hostname)
        return Response(status_code=502)
    return Response(content=body, media_type=content_type, headers={"Cache-Control": CACHE_CONTROL})
