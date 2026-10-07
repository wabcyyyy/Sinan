"""深度健康探测（审计 §3.2.6/§3.5.4 → P1-6）：MySQL SELECT 1 + Redis ping，结果短缓存。

与浅探 /health 的分工：浅探只回答「进程活着」（Dockerfile/compose 的 HEALTHCHECK
与部署等待继续用它，报告拍板不动）；本模块回答「依赖可用吗」，端点
`/api/agent/health/deep`（app/api/agent.py）给部署冒烟与外部拨测用——依赖挂掉时
返回 503 + degraded，而探活端点自身绝不 500。

结果缓存 ~10s：探活调用方常按秒级轮询，而每次真探要付 MySQL connect 3s / Redis
connect 0.5s 的超时上限，不缓存等于探活反过来打依赖（防打爆正是深探的初衷）。
探测在锁内进行，缓存过期瞬间的并发探活只有一条真探，其余等锁后读缓存。

失败语义：任一依赖异常→该项 down、整体 degraded；Redis 熔断窗内（共享客户端
`client()` 返回 None）同样如实报 down。本模块独立成文件以便单测（mock 探针即可，
不需要活库）——同 timezone_check。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import text

from app.common import redis_client
from app.db.session import session_scope

if TYPE_CHECKING:
    # redis_client.client() 的返回类型刻意写宽为 object（延迟导入保离线单测），
    # 这里只在类型层收窄，不引入运行期 import。
    from redis import Redis

logger = logging.getLogger(__name__)

#: 结果缓存窗口（秒）：~10s——够拨测方拿到新鲜结论，又不至于每次轮询都真探。
CACHE_TTL_SECONDS = 10.0

UP = "up"
DOWN = "down"
DEGRADED = "degraded"

_lock = threading.Lock()
_cached_payload: dict[str, str] | None = None
_cached_at = 0.0


def probe_mysql() -> bool:
    """SELECT 1 走业务主路径（services 同款 session_scope，引擎带 pool_pre_ping）；
    任何异常（连不上/超时/鉴权失败）都只记一条 warning 并返回 False——依赖挂掉时
    探活端点的本职就是如实报 down，而不是自己变成 500。"""
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("deep health: mysql probe failed: %s: %s", type(exc).__name__, str(exc)[:200])
        return False


def probe_redis() -> bool:
    """共享快失败客户端 ping 一把；熔断窗内（client() 返回 None）按不可用如实上报。
    ping 失败走 note_failure 开熔断窗，与其他调用方同款降级路径。"""
    client = redis_client.client()
    if client is None:
        return False
    try:
        return bool(cast("Redis", client).ping())
    except Exception as exc:
        redis_client.note_failure(exc)
        logger.warning("deep health: redis probe failed: %s: %s", type(exc).__name__, str(exc)[:200])
        return False


def snapshot() -> dict[str, Any]:
    """当前依赖状态 {"status","mysql","redis"}；缓存窗口内复用上次探测结果。

    返回**拷贝**：缓存条目是模块内部状态，调用方（含测试）改返回值不得污染缓存。
    """
    global _cached_payload, _cached_at
    with _lock:
        now = time.monotonic()
        if _cached_payload is not None and now - _cached_at < CACHE_TTL_SECONDS:
            return dict(_cached_payload)
        payload: dict[str, Any] = {
            "mysql": UP if probe_mysql() else DOWN,
            "redis": UP if probe_redis() else DOWN,
        }
        payload["status"] = UP if payload["mysql"] == UP and payload["redis"] == UP else DEGRADED
        _cached_payload, _cached_at = payload, now
        return dict(payload)


def reset_for_tests() -> None:
    """单测隔离用：丢弃缓存（不触碰任何真实连接）。"""
    global _cached_payload, _cached_at
    with _lock:
        _cached_payload = None
        _cached_at = 0.0
