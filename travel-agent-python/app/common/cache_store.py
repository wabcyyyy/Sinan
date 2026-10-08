"""带命名空间的键值缓存（Redis 优先，故障降级进程内）。

双跑期隔离原因（PLAN v3.0 §1.3）：Java 侧 `RedisCacheConfig` 用 Jackson 多态序列化并
白名单 `com.travel.backend.` 包名，Python **无法反序列化** Java 写的 value。因此
本模块统一给 key 加 `py:` 前缀，两侧各用各的命名空间、各自独占失效权，
避免"看起来共享、实际互相看不见"的诡异缓存行为。

命名规范 `<域>:<实体>:<标识>`；空结果也缓存（防穿透），用哨兵值表示。
（历史：Java 时代的 `amap:poi` 命名空间已无写入者，去高德后不再有图商键。）
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, cast

from app.common import redis_client

logger = logging.getLogger(__name__)

KEY_PREFIX = "py:"
_EMPTY_SENTINEL = "\x00empty"

#: 进程内兜底表的条目上限：Redis outage 期间所有写入都落到这张表上（R2-10）
LOCAL_CACHE_MAX_ENTRIES = 4096

_local: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def _get_client():
    """共享快失败客户端；保留本函数作为单测注入点。

    熔断窗口内抛 ConnectionError，让下面三个命令既有的 `except Exception` 分支
    走进程内兜底——降级路径与 Redis 真故障时完全一致。
    """
    client = redis_client.client()
    if client is None:
        raise ConnectionError("redis circuit breaker open")
    return client


def full_key(namespace: str, key: str) -> str:
    return f"{KEY_PREFIX}{namespace}:{key}"


def get_json(namespace: str, key: str) -> Any | None:
    """返回缓存值；未命中返回 None。空结果哨兵转成 None（调用方按未命中处理但不再打外网）。"""
    full = full_key(namespace, key)
    try:
        raw = cast("str | None", _get_client().get(full))
    except Exception as exc:
        redis_client.note_failure(exc)
        logger.debug("cache redis miss/unavailable (%s): %s", namespace, exc)
        raw = _local_get(full)
    if raw is None:
        return None
    if raw == _EMPTY_SENTINEL:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _local_put(full: str, expires_at: float, raw: str) -> None:
    """写进程内兜底缓存，并保证这张表**有界**（R2-10）。

    Redis 长时间不可用时，这里会接住全部写入：旧实现只在"读同一个键"或 delete 时
    才清条目，于是 outage 越久 RSS 越大，直到进程被 OOM 杀掉——缓存降级反而变成
    故障放大器。调用方必须已持有 `_lock`。
    先清过期项；仍超限就按最早到期逐出（不含刚写入的这个，避免占位被自己挤掉）。
    """
    _local[full] = (expires_at, raw)
    if len(_local) <= LOCAL_CACHE_MAX_ENTRIES:
        return
    now = time.time()
    for key in [k for k, (exp, _v) in _local.items() if exp <= now]:
        _local.pop(key, None)
    overflow = len(_local) - LOCAL_CACHE_MAX_ENTRIES
    if overflow > 0:
        candidates = sorted((k for k in _local if k != full), key=lambda k: _local[k][0])
        for key in candidates[:overflow]:
            _local.pop(key, None)


def set_json(namespace: str, key: str, value: Any, ttl_seconds: int) -> None:
    full = full_key(namespace, key)
    raw = _EMPTY_SENTINEL if value is None else json.dumps(value, ensure_ascii=False, default=str)
    try:
        _get_client().set(full, raw, ex=int(ttl_seconds))
        return
    except Exception as exc:
        redis_client.note_failure(exc)
        # 分裂脑窗口（R2-F11，2026-10-05 拍板：接受现状）：Redis 写失败只落本地、
        # 不撤 Redis 旧值；恢复连通后读路径 Redis 优先 → stale 最长一个 TTL 自愈
        # （详情缓存 600s）。触发需「通→抖>熔断窗→通」且抖动期有写，单机部署概率
        # 极低；上生产多实例前如需根除，做待补偿键（findings 台账 R2-F11 待办）。
        logger.debug("cache redis write failed, fallback local (%s): %s", namespace, exc)
    with _lock:
        _local_put(full, time.time() + ttl_seconds, raw)


def reserve(namespace: str, key: str, value: Any, ttl_seconds: int) -> bool:
    """原子占位：key 不存在时写入并返回 True，已存在返回 False（不覆盖）。

    幂等键的底层原语（G-3.x backlog：生成端点防重复建壳）。与 get+set 的
    两步写不同，本操作在两种存储下都是原子的：
    - Redis：`SET NX EX`（单命令原子）；
    - 进程内降级：`dict.setdefault` 在 _lock 下执行（GIL + 锁，等效原子）。

    值为 None 时存哨兵（与 set_json 同口径），调用方读到 None 也能区分
    「占位成功」与「key 已存在」。
    """
    full = full_key(namespace, key)
    raw = _EMPTY_SENTINEL if value is None else json.dumps(value, ensure_ascii=False, default=str)
    try:
        claimed = _get_client().set(full, raw, ex=int(ttl_seconds), nx=True)
        return bool(claimed)
    except Exception as exc:
        redis_client.note_failure(exc)
        logger.debug("cache redis reserve failed, fallback local (%s): %s", namespace, exc)
    with _lock:
        _expired = _local.get(full)
        if _expired is not None and _expired[0] <= time.time():
            _local.pop(full, None)  # 过期项先清，setdefault 才能占位
        claimed = full not in _local
        if claimed:
            _local_put(full, time.time() + ttl_seconds, raw)
    return claimed


def delete(namespace: str, key: str) -> None:
    """精确失效一个键（写路径用；不做整片清空）。"""
    full = full_key(namespace, key)
    try:
        _get_client().delete(full)
    except Exception as exc:
        redis_client.note_failure(exc)
        # 分裂脑同 set_json 注记（R2-F11）：删除失败不撤 Redis 旧值，stale ≤TTL 自愈。
        logger.debug("cache delete redis fallback (%s): %s", namespace, exc)
    with _lock:
        _local.pop(full, None)


def reset_for_tests() -> None:
    with _lock:
        _local.clear()
    redis_client.reset_for_tests()


def _local_get(full: str) -> str | None:
    with _lock:
        entry = _local.get(full)
        if entry is None:
            return None
        expires_at, raw = entry
        if expires_at < time.time():
            _local.pop(full, None)
            return None
        return raw
