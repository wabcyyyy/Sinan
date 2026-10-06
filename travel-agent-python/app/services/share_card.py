"""分享链接的爬虫外壳：把 SPA index.html 注入 og meta 后整页返回。

为什么要这一层：SPA 的 /s/<token> 浏览器能跑，但微信/Twitter 等抓分享卡的
爬虫不执行 JS，拿到的永远是空壳标题。后端查得到行程标题/城市，又经
`frontend_shell_url` 取得到边缘容器里的同一份 index.html——注入 og:*
后返回，同一个 URL 对爬虫是带卡片的 HTML、对浏览器就是正常 SPA 首帧。

设计口径：
- 壳进程内缓存（60s TTL，失败时允许用过期壳顶一轮）：热路径一次分享卡请求
  就是一次 DB 读，不再放大到边缘容器；
- token 无效/过期返回**未注入**的原壳：URL 语义保持 200，坏链交给 SPA 自己的
  错误态渲染，爬虫也不会把 404 记成死链；
- 注入值全部 html.escape：标题/城市是用户内容，进属性上下文必须转义；
- `frontend_shell_url` 未配置 = 能力关闭，返回 None 由路由层 302 回首页。
"""

from __future__ import annotations

import html
import re
import time
from typing import Any

import httpx

from app.common.config import settings
from app.common.envelope import ApiError
from app.services import share_service

_SHELL_TTL_SECONDS = 60.0
_SHELL_FETCH_TIMEOUT = 2.0

#: (过期时刻 monotonic, 壳 HTML)；None = 还没取过
_shell_cache: tuple[float, str] | None = None

_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)


def reset_for_tests() -> None:
    global _shell_cache
    _shell_cache = None


def inject_og_meta(shell: str, title: str, description: str) -> str:
    """替换 <title> 并在 <head> 后插入 og:* 标签；无 <head> 的异形壳原样返回
    （宁可不注入，也不产出结构坏掉的 HTML）。"""
    escaped_title = html.escape(title, quote=True)
    og_tags = (
        f'<meta property="og:title" content="{escaped_title}">'
        f'<meta property="og:description" content="{html.escape(description, quote=True)}">'
        '<meta property="og:type" content="website">'
        '<meta property="og:site_name" content="司南 Sinan">'
    )
    if _TITLE_RE.search(shell):
        shell = _TITLE_RE.sub(f"<title>{escaped_title}</title>", shell, count=1)
    head = _HEAD_RE.search(shell)
    if head is None:
        return shell
    return shell[: head.end()] + og_tags + shell[head.end() :]


def shared_shell_html(token: str) -> str | None:
    """返回 /s/{token} 应当响应的外壳 HTML；None = 取不到壳，调用方降级 302。"""
    shell = _load_shell()
    if shell is None:
        return None
    try:
        vo: dict[str, Any] = share_service.view_shared(token)
    except ApiError:
        # 坏链/过期：返回原壳（200），让 SPA 渲染自己的错误态
        return shell
    title = str(vo.get("title") or "行程分享")
    city = str(vo.get("city") or "").strip()
    days = vo.get("days")
    lead = f"{city}·{days}天行程" if city and days else (city or "行程分享")
    return inject_og_meta(shell, title, f"{lead} · 司南 Sinan")


def _load_shell() -> str | None:
    global _shell_cache
    if not settings.frontend_shell_url:
        return None
    now = time.monotonic()
    if _shell_cache is not None and _shell_cache[0] > now:
        return _shell_cache[1]
    try:
        response = httpx.get(settings.frontend_shell_url, timeout=_SHELL_FETCH_TIMEOUT, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError:
        # 边缘暂时不可达：有过期壳就顶一轮（比把用户 302 走好），没有才放弃
        return _shell_cache[1] if _shell_cache is not None else None
    _shell_cache = (now + _SHELL_TTL_SECONDS, response.text)
    return response.text
