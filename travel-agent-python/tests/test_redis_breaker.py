"""Redis 快失败 + 熔断：连不上 Redis 时，热路径不能每次命令都付连接超时。

钉住三件真实代价很高的事：
1. 共享客户端必须带 `socket_connect_timeout`（本机实测：不带时一次失败命令 2.0s，
   而 `is_revoked` 在每个鉴权请求上、`cache_store` 在每次详情读上）；
2. 一次失败后开熔断窗口，窗口内 `client()` 直接返回 None，各调用方走既有的进程内兜底；
3. 窗口**不因后续失败续期**，否则有持续流量时 Redis 恢复后也永远不会被重试。
"""

from __future__ import annotations

import pytest
import redis

from app.common import cache_store, redis_client, token_revocation
from app.common.config import settings
from app.services import state_and_sessions


@pytest.fixture(autouse=True)
def isolated_dead_redis(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    redis_client.reset_for_tests()
    cache_store.reset_for_tests()
    token_revocation.reset_for_tests()
    yield
    redis_client.reset_for_tests()


def test_shared_client_sets_fast_fail_timeouts():
    client = redis_client.client()
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == redis_client.CONNECT_TIMEOUT_SECONDS
    assert kwargs["socket_timeout"] == redis_client.CONNECT_TIMEOUT_SECONDS
    assert kwargs["decode_responses"] is True
    # 单例：热路径不重复建连接池
    assert redis_client.client() is client


def test_failed_command_opens_breaker_and_client_returns_none():
    with pytest.raises(redis.exceptions.RedisError):
        redis_client.client().exists("probe")
    redis_client.note_failure(RuntimeError("connection refused"))
    assert redis_client.is_down() is True
    assert redis_client.client() is None


def test_consumers_degrade_to_local_state_while_breaker_open(monkeypatch):
    redis_client.note_failure(RuntimeError("down"))
    # 缓存：读写都退回进程内，不抛异常
    cache_store.set_json("itinerary:detail", "1", {"id": 1}, 60)
    assert cache_store.get_json("itinerary:detail", "1") == {"id": 1}
    # 黑名单：本地表仍然生效（登出后 token 仍被判为已吊销）
    token_revocation.revoke("tok-1", 60)
    assert token_revocation.is_revoked("tok-1") is True
    # 限流计数：进程内滑动窗口继续计数
    assert state_and_sessions.sliding_hit("auth:login:fail:1.1.1.1|alice", 900) == 1
    assert state_and_sessions.sliding_hit("auth:login:fail:1.1.1.1|alice", 900) == 2


def test_breaker_window_is_not_extended_by_repeated_failures(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(redis_client.time, "monotonic", lambda: clock["now"])

    redis_client.note_failure(RuntimeError("first"))
    clock["now"] += redis_client.BREAKER_COOLDOWN_SECONDS / 2
    redis_client.note_failure(RuntimeError("second"))
    clock["now"] += redis_client.BREAKER_COOLDOWN_SECONDS
    # 从首次失败起满一个窗口即恢复探测；续期过的话这里仍是 None
    assert redis_client.client() is not None


def test_local_fallback_tables_stay_bounded_during_outage(monkeypatch):
    """熔断期进程内兜底表必须有界（R2-10 的同一课，本文件当时漏了）。

    键里含**匿名可控**的一段：`auth:login:fail:{ip}|{user}`、`public:{bucket}:{ip}`。
    不设上限时，Redis 故障窗口内刷不同 IP 就是"降级把进程喂到 OOM"。
    """
    monkeypatch.setattr(state_and_sessions, "LOCAL_STATE_MAX_ENTRIES", 8)
    state_and_sessions.reset_for_tests()
    for i in range(200):
        state_and_sessions.sliding_hit(
            f"auth:login:fail:10.0.{i // 256}.{i % 256}|alice", state_and_sessions.LOGIN_WINDOW_SECONDS
        )
        state_and_sessions.try_mark(f"public:gen:{i}", 60)
    assert len(state_and_sessions._windows) <= 8, "滑动窗口表随匿名 IP 数量无界增长"
    assert len(state_and_sessions._marks) <= 8, "一次性标记表无界增长"
    # 淘汰只能丢"最快过期"的键，不能把当日额度桶扫掉：
    # quota:llm:day:{uid} 用的是 86400s 窗口，按全局 15 分钟 cutoff 扫表会让
    # 熔断期的当日限额退化成 15 分钟限额（等于无限刷付费调用）。
    day_key = "quota:llm:day:42"
    state_and_sessions.sliding_hit(day_key, 86400)
    for i in range(200):
        state_and_sessions.sliding_hit(
            f"auth:login:fail:10.0.{i // 256}.{i % 256}|u", state_and_sessions.LOGIN_WINDOW_SECONDS
        )
    assert state_and_sessions.sliding_count(day_key, 86400) == 1, "当日额度桶被挤掉 → 限额退化"
    key = "auth:login:fail:1.2.3.4|bob"
    for _ in range(5):
        state_and_sessions.sliding_hit(key, state_and_sessions.LOGIN_WINDOW_SECONDS)
    assert state_and_sessions.sliding_count(key, state_and_sessions.LOGIN_WINDOW_SECONDS) == 5


def test_full_mark_table_refuses_new_lock_instead_of_evicting(monkeypatch):
    """锁表满时宁可拒绝占用，也不能淘汰未到期的占用。

    `gen:day:*` 与续跑锁都走这张表：挤掉一把未过期的锁 = 同一天交给两个 worker
    同时生成（互相覆盖、重复烧钱），比"这一单先不做"严重一个量级。
    """
    monkeypatch.setattr(state_and_sessions, "LOCAL_STATE_MAX_ENTRIES", 4)
    state_and_sessions.reset_for_tests()
    acquired = [state_and_sessions.try_mark(f"gen:day:{i}", 600) for i in range(4)]
    assert all(acquired)
    assert state_and_sessions.try_mark("gen:day:999", 600) is False, "满了还接受新占用"
    assert all(state_and_sessions.is_marked(f"gen:day:{i}") for i in range(4)), "已有占用被挤掉了"
    # 同键重入仍回 False（不续期别人的占用）
    assert state_and_sessions.try_mark("gen:day:0", 600) is False
