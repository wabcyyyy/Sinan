"""JWT 服务端吊销黑名单，与 Java `TokenRevocationService` 逐字对齐。

对齐点（任一不符即会出现「一边登出、另一边仍有效」的安全回归）：
- key = `auth:jwt:revoked:` + sha256(token) 的小写十六进制（**按整串 token 哈希**，不是 jti）；
- value 为常量 "1"，TTL 覆盖到 token 自然过期时刻；
- 读写顺序与 Java 相同：`is_revoked` 先问 Redis，再查进程内兜底表；Redis 异常时退回本地。

Redis 不可用时降级为进程内 Map（仅单机有效），与 Java 侧同语义——避免缓存故障拖垮鉴权。
"""

from __future__ import annotations

import hashlib
import logging
import time

from app.common import redis_client
from app.common.config import settings

logger = logging.getLogger(__name__)

KEY_PREFIX = "auth:jwt:revoked:"

_local_blacklist: dict[str, int] = {}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_client():
    """共享快失败客户端；保留本函数作为单测注入点。

    熔断窗口内抛 ConnectionError，由下面两个命令既有的 `except Exception` 分支退回
    进程内兜底表——与 Redis 真故障时同一条路径。
    """
    client = redis_client.client()
    if client is None:
        raise ConnectionError("redis circuit breaker open")
    return client


def revoke(token: str, ttl_seconds: int) -> None:
    if not token or ttl_seconds <= 0:
        return
    key = token_hash(token)
    if settings.jwt_revocation_prefer_redis:
        try:
            _get_client().set(KEY_PREFIX + key, "1", ex=int(ttl_seconds))
            return
        except Exception as exc:
            already_down = redis_client.is_down()
            redis_client.note_failure(exc)
            logger.log(
                logging.DEBUG if already_down else logging.WARNING, "redis revoke failed, fallback to local: %s", exc
            )
    _local_blacklist[key] = int(time.time()) + int(ttl_seconds)


def is_revoked(token: str) -> bool:
    if not token:
        return False
    key = token_hash(token)
    if settings.jwt_revocation_prefer_redis:
        try:
            if _get_client().exists(KEY_PREFIX + key):
                return True
        except Exception as exc:
            redis_client.note_failure(exc)
            logger.debug("redis revoke check failed, fallback to local: %s", exc)
    expires_at = _local_blacklist.get(key)
    if expires_at is None:
        return False
    if expires_at < time.time():
        _local_blacklist.pop(key, None)
        return False
    return True


def reset_for_tests() -> None:
    """单测隔离用：清空进程内兜底表与共享客户端/熔断状态（不连接 Redis）。"""
    _local_blacklist.clear()
    redis_client.reset_for_tests()
