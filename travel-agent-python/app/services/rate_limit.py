"""匿名端点的按 IP 闸门（R1-4，业务轨：超限抛 ApiError(429)）。

为什么需要：`/api/image-proxy` 与 `/api/poi-photo` 在 `PUBLIC_PATHS` 里匿名可达，
而 `poi-photo` 未命中一次最多要打 6 个上游请求（维基 → 图库链）。没有按 IP 的闸门时，
一个不带凭据的循环就能耗尽出网配额与 worker——OOM 放大之外还多烧一份钱。

归因地址只信 `app.common.client_ip`（可信代理链上从右往左数第一个不可信跳）：
直接取 `X-Forwarded-For` 首段等于让攻击者每次换个 IP。

Redis 不可用时 `sliding_hit` 退进程内窗口（与登录/分享限速同口径）：单机演示有效，
多副本部署下预算会翻倍，但不会因为 Redis 挂了就把端点关掉。
"""

from __future__ import annotations

from app.common.config import settings
from app.common.envelope import ApiError
from app.services import state_and_sessions

_WINDOW_SECONDS = 60


def enforce_public_bucket(bucket: str, client_ip: str) -> None:
    """记一次并判定：窗口内超过 `public_rate_limit_per_minute` 即 429。"""
    hits = state_and_sessions.sliding_hit(f"public:{bucket}:{client_ip}", _WINDOW_SECONDS)
    if hits > settings.public_rate_limit_per_minute:
        raise ApiError(429, "请求过于频繁，请稍后再试")
