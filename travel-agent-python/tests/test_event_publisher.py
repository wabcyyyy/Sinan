"""event_publisher 单测：伪造 Redis 客户端验证信封协议与尽力而为语义。

协议契约（与 Java SSE 网关共用，不得偏离）：
- 通道 gen:events:{itineraryId}；信封五键 camelCase；
- seq 取自 Redis INCR gen:seq:{itineraryId}，每次 INCR 后 EXPIRE 7200s；
- ts 为东八区 ISO-8601；
- itineraryId 缺失完全不触碰 Redis；Redis 故障不向上抛异常。
"""

import asyncio
import json
import logging

import pytest

from app.common import event_publisher
from app.common.config import settings


class FakeRedis:
    """最小 Redis 桩：记录 incr/expire/publish 调用，可注入故障。"""

    def __init__(self, fail: bool = False):
        self.counters: dict[str, int] = {}
        self.expires: list[tuple[str, int]] = []
        self.published: list[tuple[str, str]] = []
        self.fail = fail

    def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, ttl: int) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.expires.append((key, ttl))

    def publish(self, channel: str, payload: str) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, payload))


@pytest.fixture(autouse=True)
def _isolated_publisher():
    """用例前后重置单例，避免跨用例串状态。"""
    event_publisher.reset_event_publisher()
    yield
    event_publisher.reset_event_publisher()


@pytest.fixture()
def fake_redis(monkeypatch) -> FakeRedis:
    """注入伪造客户端并记录其调用，供断言。"""
    fake = FakeRedis()
    monkeypatch.setattr(event_publisher, "_get_client", lambda: fake)
    return fake


def test_publish_event_envelope_and_channel(fake_redis):
    event_publisher.publish_event(123, "research_start", {"domains": ["attraction"]})

    assert len(fake_redis.published) == 1
    channel, payload = fake_redis.published[0]
    assert channel == "gen:events:123"
    envelope = json.loads(payload)
    # 信封五键齐全且全部 camelCase
    assert set(envelope) == {"type", "itineraryId", "seq", "ts", "data"}
    assert envelope["type"] == "research_start"
    assert envelope["itineraryId"] == 123
    assert envelope["data"] == {"domains": ["attraction"]}
    # ts 为东八区 ISO-8601（带 +08:00 偏移）
    assert envelope["ts"].endswith("+08:00")


def test_seq_increments_and_expire_called(fake_redis):
    event_publisher.publish_event(7, "research_start", {})
    event_publisher.publish_event(7, "research_done", {})
    event_publisher.publish_event(8, "research_start", {})

    first = json.loads(fake_redis.published[0][1])
    second = json.loads(fake_redis.published[1][1])
    third = json.loads(fake_redis.published[2][1])
    # 同一行程 seq 单调递增；不同行程使用独立计数键
    assert (first["seq"], second["seq"], third["seq"]) == (1, 2, 1)
    # 每次 INCR 后都续期 EXPIRE 7200s
    assert fake_redis.expires == [
        ("gen:seq:7", 7200),
        ("gen:seq:7", 7200),
        ("gen:seq:8", 7200),
    ]


def test_redis_error_never_propagates(monkeypatch):
    monkeypatch.setattr(event_publisher, "_get_client", lambda: FakeRedis(fail=True))
    # 事件是尽力而为通知：Redis 故障只记日志，绝不影响主流程
    event_publisher.publish_event(42, "research_done", {"evidenceCount": 3})


def test_missing_itinerary_id_never_touches_redis(monkeypatch):
    calls: list[str] = []

    def _spy_client():
        calls.append("created")
        return FakeRedis()

    monkeypatch.setattr(event_publisher, "_get_client", _spy_client)
    event_publisher.publish_event(None, "research_start", {"domains": []})
    event_publisher.publish_event(0, "research_start", {"domains": []})
    # 无有效 itineraryId 时不创建客户端、不发出任何命令
    assert calls == []


def test_high_level_helpers_shape(fake_redis):
    event_publisher.publish_research_start(5, ["attraction", "hotel", "food"])
    event_publisher.publish_research_done(
        5,
        9,
        True,
        [{"domain": "attraction", "count": 4}, {"domain": "food", "count": 3}, {"domain": "hotel", "count": 2}],
    )
    event_publisher.publish_degraded(5, "research", "研究失败：x", "以现有证据继续生成")

    types = [json.loads(p)["type"] for _c, p in fake_redis.published]
    assert types == ["research_start", "research_done", "degraded"]
    done = json.loads(fake_redis.published[1][1])
    # Python 负责的事件 data 契约：camelCase 键 + 各域计数
    assert done["data"] == {
        "evidenceCount": 9,
        "degraded": True,
        "domains": [
            {"domain": "attraction", "count": 4},
            {"domain": "food", "count": 3},
            {"domain": "hotel", "count": 2},
        ],
    }
    degraded = json.loads(fake_redis.published[2][1])
    assert degraded["data"] == {"scope": "research", "reason": "研究失败：x", "fallback": "以现有证据继续生成"}


def test_publish_event_with_run_id_merges_run_id_and_records_trace(fake_redis, monkeypatch):
    """M5 三向关联：run_id 并入 data 自定义区（runId），并以 scene=stream 落当前 trace。"""
    recorded: list[tuple] = []
    monkeypatch.setattr(
        "app.agent.runtime.trace.record_event", lambda kind, name, **kwargs: recorded.append((kind, name, kwargs))
    )
    data = {"domains": ["attraction"]}
    event_publisher.publish_event(9, "research_start", data, run_id="run-abc")

    envelope = json.loads(fake_redis.published[0][1])
    # 信封仍是五键：runId 只进 data 自定义区，不破坏协议，也不污染调用方 dict
    assert set(envelope) == {"type", "itineraryId", "seq", "ts", "data"}
    assert envelope["data"] == {"domains": ["attraction"], "runId": "run-abc"}
    assert data == {"domains": ["attraction"]}
    # 同一次发布落当前 trace：kind=stream、name=事件类型，元数据带行程与轨迹 ID
    assert recorded == [
        (
            "stream",
            "research_start",
            {
                "metadata": {"event": "research_start", "itinerary_id": 9, "run_id": "run-abc"},
            },
        )
    ]


def test_publish_event_without_run_id_keeps_legacy_shape(fake_redis, monkeypatch):
    """无 run_id 时行为不变：data 无 runId，也不产生 stream 轨迹事件。"""
    recorded: list[tuple] = []
    monkeypatch.setattr("app.agent.runtime.trace.record_event", lambda *a, **k: recorded.append((a, k)))
    event_publisher.publish_event(9, "research_start", {"domains": ["attraction"]})

    envelope = json.loads(fake_redis.published[0][1])
    assert envelope["data"] == {"domains": ["attraction"]}
    assert "runId" not in envelope["data"]
    assert recorded == []


def test_research_helpers_forward_run_id(fake_redis):
    """便捷函数把 run_id 原样透传给 publish_event（M5 day_stream 调用形态）。"""
    event_publisher.publish_research_start(5, ["attraction"], run_id="run-1")
    event_publisher.publish_research_done(5, 1, False, [{"domain": "attraction", "count": 1}], run_id="run-1")
    event_publisher.publish_degraded(5, "research", "r", "f", run_id="run-1")

    assert [json.loads(p)["data"]["runId"] for _c, p in fake_redis.published] == ["run-1", "run-1", "run-1"]


def test_client_lazy_singleton_uses_settings_url(monkeypatch):
    # 用真实工厂验证：from_url 不发起连接，按 settings.redis_url 惰性建单例
    monkeypatch.setattr(settings, "redis_url", "redis://cache-host:6380/2")
    event_publisher.reset_event_publisher()
    client = event_publisher._get_client()
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["host"] == "cache-host"
    assert kwargs["port"] == 6380
    assert kwargs["db"] == 2
    # decode_responses=True：JSON 字符串直接以 str 收发
    assert kwargs["decode_responses"] is True
    # 单例：二次获取返回同一实例
    assert event_publisher._get_client() is client


def test_breaker_open_skips_redis_publish_but_local_hub_still_delivers(monkeypatch, caplog):
    """熔断窗口：死信通道跳过+计数，进程内 SSE 订阅者照常收到事件（2026-09-30 评审遗留）。

    评审曾把「Redis 熔断期 publish failed 告警」当成 research_start 丢失。本测钉住
    真实语义：event_hub 广播先于 Redis 执行，本地订阅者不受熔断影响；Redis 侧
    （gen:events 自 Java 退役后无订阅方）跳过留痕即可，不需要恢复后补发。
    """
    from app.common import event_hub, redis_client

    monkeypatch.setattr(redis_client, "is_down", lambda: True)
    touched: list[str] = []
    monkeypatch.setattr(event_publisher, "_get_client", lambda: touched.append("called"))

    async def scenario():
        sub, rejected = event_hub.subscribe(77, "{}")
        assert not rejected
        event_publisher.publish_research_start(77, ["attraction", "food", "hotel"])
        return await sub.take(1.0)

    with caplog.at_level(logging.WARNING, logger="app.common.event_publisher"):
        frame = asyncio.run(scenario())

    envelope = json.loads(frame)
    assert envelope["type"] == "research_start"
    assert envelope["itineraryId"] == 77
    assert touched == [], "熔断窗口内 seq 与发布两条路径都不得尝试创建 Redis 客户端"
    assert event_publisher._redis_skip_publish_count == 1, "跳过必须计数留痕"
    assert caplog.text == "", "窗口内是预期降级，不得刷 warning"


def test_breaker_recovered_publishes_again_and_skip_count_isolated(monkeypatch):
    """窗口关闭后恢复发布；autouse 夹具把跳过计数清零（用例间互不串状态）。"""
    from app.common import redis_client

    fake = FakeRedis()
    monkeypatch.setattr(event_publisher, "_get_client", lambda: fake)
    monkeypatch.setattr(redis_client, "is_down", lambda: False)

    event_publisher.publish_event(9, "research_start", {"domains": ["attraction"]})

    assert len(fake.published) == 1
    assert event_publisher._redis_skip_publish_count == 0


def test_terminal_event_closes_subscriptions_only_after_drain():
    """R3-F4：终态帧（complete/error）广播后服务端要收尾订阅——但必须先让已入队
    帧（含终态帧本身）排空。旧实现终态后无限心跳，5 个连接槽被死会话占到达客户端断开。"""
    from app.common import event_hub

    async def scenario():
        sub, rejected = event_hub.subscribe(42, "{}")
        assert not rejected
        event_publisher.publish_event(42, "day_done", {"dayNo": 1})
        event_publisher.publish_event(42, "error", {"code": "AGENT_ERROR", "message": "boom", "retryable": True})

        # 已入队两帧先排空（终态帧不被掐断）
        first = await sub.take(timeout=1.0)
        second = await sub.take(timeout=1.0)
        assert "day_done" in first
        assert '"error"' in second
        # 队列排空 + open 已被终态收尾置 False → CLOSED，不再无限心跳
        assert await sub.take(timeout=0.05) is event_hub.CLOSED

    asyncio.run(scenario())
    # 消费端 finally 退订后注册表干净（这里手动走一次同一收尾路径）
    for sinks in list(event_hub._subscribers.values()):
        for sub in list(sinks):
            event_hub.unsubscribe(sub)
    assert event_hub.subscriber_count(42) == 0


def test_non_terminal_events_keep_subscriptions_open():
    """非终态事件（day_*/degraded/…）不关流：生成进行中的连接必须继续收帧。"""
    from app.common import event_hub

    async def scenario():
        sub, rejected = event_hub.subscribe(43, "{}")
        assert not rejected
        event_publisher.publish_event(43, "degraded", {"scope": "research", "reason": "r", "fallback": "f"})
        frame = await sub.take(timeout=1.0)
        assert "degraded" in frame
        assert sub.open is True
        # 心跳周期内没有终态帧：take 空转返回 None（连接保持），不是 CLOSED
        assert await sub.take(timeout=0.05) is None

    asyncio.run(scenario())


def test_seq_floor_survives_table_overflow():
    """R2-F5 回归：地板值表到量按最近活跃淘汰，窗口内的热键 seq 不得回退。

    旧实现溢出 `.clear()` 全表——预置热键地板 80、随后灌满护栏容量，热键下一帧
    退到 1（会诊官纯内存复现 80→1），活跃行程直接回退 INV-5 单调。
    """
    with event_publisher._last_seq_lock:
        event_publisher._last_seq.clear()
        event_publisher._last_seq[42] = 80
    for cold in range(event_publisher._LAST_SEQ_MAX - 1):
        assert event_publisher._advance_seq(10_000 + cold, 1) == 1
    # 热键与最近灌入的冷键都在"近 4096"窗口内：地板全保住
    assert event_publisher._advance_seq(42, 1) == 81, "窗口内热键地板被清 = seq 回退"
    assert event_publisher._advance_seq(10_000 + event_publisher._LAST_SEQ_MAX - 2, 1) == 2
    # 表长有界（不再无界增长）
    assert len(event_publisher._last_seq) <= event_publisher._LAST_SEQ_MAX
