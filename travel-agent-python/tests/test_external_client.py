"""ExternalClient 基类测试（G-3.2）。

覆盖卡要求的三条路径 + 另两项能力 + L1 可靠性三件套：
- 缓存命中（含负结果短 TTL：正/负 TTL 不同，验证负结果更快过期）；
- 超时（loader 抛异常 → 降级 None，不打断调用方）；
- 响应超限（clamp_bytes 拒收，不做"截断后当成功"）；
- 自节流（双车道：后台车道超 max_wait 放弃本次调用）；
- key 解析链（env → 实例 → 调用方；全空返回 None，绝不借用他人 key）；
- 重试（只对显式开启的幂等通道；退避后重新预约车道槽）；
- 熔断（连续失败开固定冷却窗，窗口内零外呼，过期放行探测，成功关闭）；
- 预约槽节流（并发调用间隔严格错开——持锁 sleep 消除后的行为回归）。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from _fake_clock import assert_min_spacing

from app.common.external_client import BACKGROUND, INTERACTIVE, ExternalClient

pytest_plugins = ["_fake_clock"]


def test_cache_hit_avoids_second_call():
    client = ExternalClient(name="t", ttl_seconds=60, max_wait_seconds=0)
    calls = []

    def loader():
        calls.append(1)
        return {"v": 1}

    assert client.call("k", loader) == {"v": 1}
    assert client.call("k", loader) == {"v": 1}
    assert len(calls) == 1, "第二次必须命中缓存，不再打外部"


def test_negative_result_uses_shorter_ttl(monkeypatch):
    """负结果走 negative_ttl：正结果 TTL 更短时不影响负结果，反之亦然。"""
    client = ExternalClient(name="t", ttl_seconds=3600, negative_ttl_seconds=10, max_wait_seconds=0)
    calls = []

    def missing():
        calls.append("miss")
        return None

    assert client.call("n", missing) is None
    assert client.call("n", missing) is None
    assert len(calls) == 1, "负结果同样进缓存（避免反复打空）"

    # 时间推进超过负 TTL → 允许重试
    import time

    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 11)
    assert client.call("n", missing) is None
    assert len(calls) == 2, "负 TTL 过期后必须能再试（否则一次失败锁死整天）"


def test_positive_and_negative_ttl_are_independent(monkeypatch):
    """正结果 TTL=0（禁用缓存）时，负结果仍按自己的 TTL 缓存。"""
    client = ExternalClient(name="t", ttl_seconds=0, negative_ttl_seconds=60, max_wait_seconds=0)
    calls = []

    def flaky():
        calls.append(1)
        return None if len(calls) == 1 else {"ok": True}

    assert client.call("k", flaky) is None
    assert client.call("k", flaky) is None, "负结果仍在缓存期内"
    assert len(calls) == 1


def test_loader_exception_degrades_to_none():
    """超时/网络异常一律降级 None：外部依赖失败不得打断生成（INV-9/降级纪律）。"""
    client = ExternalClient(name="t", max_wait_seconds=0)

    def boom():
        raise TimeoutError("read timeout")

    assert client.call("k", boom) is None


def test_clamp_bytes_rejects_oversized_response():
    client = ExternalClient(name="t", max_response_bytes=16)
    assert client.clamp_bytes(b"small") == b"small"
    assert client.clamp_bytes("x" * 100) is None, "超限必须拒收，不能截断后当成功"
    assert client.clamp_bytes(None) is None


def test_background_lane_throttles_and_gives_up():
    """后台车道最小间隔 1s；超过 max_wait 即放弃（返回 None 且不打外部）。"""
    client = ExternalClient(name="t", max_wait_seconds=0.01)
    calls = []

    def loader():
        calls.append(1)
        return "v"

    assert client.call("a", loader, lane=BACKGROUND) == "v"
    # 紧接的第二次：需要等 ~1s 但只允许等 0.01s → 放弃
    assert client.call("b", loader, lane=BACKGROUND) is None
    assert len(calls) == 1, "被节流放弃时不得执行 loader"


def test_interactive_lane_waits_within_budget():
    """前台车道间隔短（0.2s），在 max_wait 内应等待并通过。"""
    client = ExternalClient(name="t", max_wait_seconds=1.0)
    calls = []

    def loader():
        calls.append(1)
        return "v"

    assert client.call("a", loader, lane=INTERACTIVE) == "v"
    assert client.call("b", loader, lane=INTERACTIVE) == "v"
    assert len(calls) == 2, "前台可在等待预算内完成，而非被放弃"


def test_resolve_key_chain_never_borrows_others():
    assert ExternalClient.resolve_key(None, "  ", "caller") == "caller"
    assert ExternalClient.resolve_key("env", "instance", "caller") == "env", "env 优先"
    assert ExternalClient.resolve_key(None, "instance", "caller") == "instance"
    assert ExternalClient.resolve_key(None, None, None) is None, "全空即无 key，不回落到他人凭据"


def test_clear_cache_allows_refetch():
    client = ExternalClient(name="t", ttl_seconds=3600, max_wait_seconds=1.0)
    calls = []

    def loader():
        calls.append(1)
        return {"v": 1}

    client.call("k", loader)
    client.clear_cache()
    client.call("k", loader)
    assert len(calls) == 2


@pytest.mark.parametrize("lane", [INTERACTIVE, BACKGROUND])
def test_lane_names_are_registered(lane):
    """车道名必须在表内（防拼错导致永远走默认间隔）。"""
    from app.common.external_client import _LANE_MIN_INTERVAL

    assert lane in _LANE_MIN_INTERVAL


# ---- L1：重试 / 熔断 / 预约槽 -------------------------------------------------


def test_retry_succeeds_on_second_attempt(fake_clock):
    """瞬时失败重试 1 次后成功：loader 共执行 2 次，退避被假钟吸收。"""
    client = ExternalClient(name="t", max_wait_seconds=5.0, retry_attempts=1, retry_backoff_seconds=0.3)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("transient")
        return {"v": 1}

    assert client.call("k", flaky) == {"v": 1}
    assert len(calls) == 2, "重试通道必须真的再试一次"
    assert fake_clock.sleeps, "重试前必须有退避睡眠（防止贴脸重打）"


def test_retry_gives_up_after_attempts_and_trips_breaker(fake_clock):
    """重试耗尽按 1 次失败计熔断计数；达到阈值后不再外呼。"""
    client = ExternalClient(
        name="t",
        max_wait_seconds=5.0,
        retry_attempts=1,
        circuit_failure_threshold=1,
        circuit_cooldown_seconds=30,
    )
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("down")

    assert client.call("k", boom) is None
    assert len(calls) == 2, "重试 1 次 = loader 共执行 2 次"
    assert client.call("fresh", boom) is None
    assert len(calls) == 2, "熔断开窗后零外呼（高基数键负缓存挡不住，必须靠熔断）"


def test_breaker_opens_after_consecutive_failures_and_half_closes(fake_clock):
    client = ExternalClient(name="t", max_wait_seconds=5.0, circuit_failure_threshold=3, circuit_cooldown_seconds=30)
    calls = []

    def boom():
        calls.append("fail")
        raise RuntimeError("upstream down")

    for _ in range(3):
        assert client.call(f"k{len(calls)}", boom) is None
    assert len(calls) == 3
    assert client.call("during-window", boom) is None
    assert len(calls) == 3, "冷却窗内不得执行 loader"

    fake_clock.advance(31)
    calls.clear()

    def ok():
        calls.append("ok")
        return {"v": 1}

    assert client.call("after-window", ok) == {"v": 1}
    assert client.call("after-recovery", ok) == {"v": 1}
    assert calls == ["ok", "ok"], "窗口过期放行探测，探测成功即关闭"


def test_valid_negative_results_do_not_trip_breaker(fake_clock):
    """供应商答"没有"（合法空结果）不是故障——熔断只为异常/超时开窗。"""
    client = ExternalClient(
        name="t",
        max_wait_seconds=5.0,
        ttl_seconds=0,
        negative_ttl_seconds=0,
        circuit_failure_threshold=2,
    )
    calls = []

    def empty():
        calls.append(1)
        return []

    for i in range(5):
        assert client.call(f"k{i}", empty) == []
    assert len(calls) == 5, "合法空结果永不触发熔断"


def test_retry_disabled_by_default_keeps_single_call():
    """默认 retry_attempts=0 = 旧行为：异常一次即降级 None（存量通道零变化）。"""
    client = ExternalClient(name="t", max_wait_seconds=5.0)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("down")

    assert client.call("k", boom) is None
    assert len(calls) == 1


def test_reset_runtime_state_reopens_circuit_for_tests(fake_clock):
    client = ExternalClient(name="t", max_wait_seconds=5.0, circuit_failure_threshold=1)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("down")

    assert client.call("k", boom) is None
    assert client.call("k2", boom) is None, "阈值 1：首次失败即开窗"
    assert len(calls) == 1
    client.reset_runtime_state()
    assert client.call("k3", boom) is None
    assert len(calls) == 2, "reset_runtime_state 后恢复外呼"


def test_concurrent_calls_are_spaced_by_interval():
    """预约槽回归（L1 补充 2）：并发同车道调用的真实外呼时刻必须错开 ≥ 间隔。

    旧实现持锁 sleep：并发调用算出同一个 wait、同时醒来一齐打出去（间隔≈0）。
    新实现锁内只预约时间槽，天然严格错峰。
    """
    client = ExternalClient(name="t", max_wait_seconds=5.0, min_interval_seconds=0.15)
    stamps: list[float] = []
    lock = threading.Lock()

    def loader():
        with lock:
            stamps.append(time.monotonic())
        return "v"

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(client.call, f"k{i}", loader) for i in range(4)]
        assert all(f.result(timeout=10) == "v" for f in futures)
    assert len(stamps) == 4
    assert_min_spacing(stamps, interval=0.15, tolerance=0.05)


def test_exception_logs_redact_url_secrets(caplog):
    """L12：token 走 URL query 的通道（Hotellook/SerpApi/OTM apikey 同理），
    httpx 异常文本内嵌完整 URL——进日志前必须脱敏。"""
    client = ExternalClient(name="t", max_wait_seconds=5.0)
    request = httpx.Request("GET", "https://api.example.com/v1/cache.json?location=BCN&token=super-secret-token")

    def boom():
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError(
            f"Server error '500 Internal Server Error' for url '{request.url}'", request=request, response=response
        )

    with caplog.at_level("WARNING", logger="app.common.external_client"):
        assert client.call("k", boom) is None
    assert "super-secret-token" not in caplog.text, "密钥不得泄漏进日志"
    assert "token=<redacted>" in caplog.text, "脱敏后保留参数名，便于排障"


def test_exception_logs_redact_client_id(caplog):
    """P1-5：Unsplash 的 key 走 client_id 查询参数，异常文本同样内嵌它。"""
    client = ExternalClient(name="t", max_wait_seconds=5.0)
    request = httpx.Request("GET", "https://api.unsplash.com/search/photos?query=lake&client_id=unsplash-secret")

    def boom():
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError(
            f"Client error '403 Forbidden' for url '{request.url}'", request=request, response=response
        )

    with caplog.at_level("WARNING", logger="app.common.external_client"):
        assert client.call("k", boom) is None
    assert "unsplash-secret" not in caplog.text, "client_id 密钥不得泄漏进日志"
    assert "client_id=<redacted>" in caplog.text, "脱敏后保留参数名，便于排障"
