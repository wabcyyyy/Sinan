"""共享 Redis 客户端：快速失败 + 短熔断。

两个模块级单例客户端各自为政会踩同一个坑：redis-py 不带 `socket_connect_timeout` 时，
连不上的 Redis 会让**每一次**命令都付出秒级等待（实测被墙端口 2.0s/次），而黑名单和
缓存都在每个鉴权请求的热路径上——Redis 一宕，整站请求集体变慢。

因此这里统一：连接超时 0.5s，失败后开 5s 熔断窗口（窗口内 `client()` 直接返回 None，
调用方走自己原有的进程内降级分支）。熔断不改变可见语义：Redis 恢复后最多 5s 即回到
共享状态，而这段窗口本来就只能用单机兜底。
"""

from __future__ import annotations

import logging
import threading
import time

from app.common.config import settings

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 0.5
BREAKER_COOLDOWN_SECONDS = 5.0

_client = None
_down_until = 0.0
_lock = threading.Lock()


def client() -> object | None:
    """返回共享客户端；熔断窗口内返回 None，调用方按 Redis 不可用处理。"""
    global _client
    if is_down():
        return None
    with _lock:
        if _client is None:
            from redis import Redis  # 延迟导入：离线单测不需要 Redis

            _client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
                socket_timeout=CONNECT_TIMEOUT_SECONDS,
            )
        return _client


def note_failure(exc: BaseException) -> None:
    """记一次 Redis 故障并开启熔断窗口。

    窗口已开时**不续期**：否则在持续流量下每次失败都把窗口推后，Redis 恢复后再也不会
    被重试。保持固定窗口，到期后的第一个请求负责再探一次。
    """
    global _down_until
    now = time.monotonic()
    if now < _down_until:
        return
    _down_until = now + BREAKER_COOLDOWN_SECONDS
    logger.debug("redis unavailable, breaker open %.1fs: %s", BREAKER_COOLDOWN_SECONDS, exc)


def is_down() -> bool:
    return time.monotonic() < _down_until


def reset_for_tests() -> None:
    """单测隔离用：丢弃客户端与熔断状态（不连接 Redis）。"""
    global _client, _down_until
    with _lock:
        _client = None
        _down_until = 0.0
