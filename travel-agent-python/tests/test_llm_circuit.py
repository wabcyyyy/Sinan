"""LLM 通道熔断 / main 备选链 / 告警 webhook / boot 警告（审计 §3.1.1、§3.1.6、§3.2.2、§4.1 P0-2）。

背景：2026-10-05 主网关全量失败 2 小时靠人肉翻日志发现——无熔断、无切换、无告警
（llm_client.py 模块注释自记该事故）。本文件钉住四件事：
1. 熔断：连续通道级失败开**固定窗**（窗内零外呼）、半开探测（成功关窗/失败一次
   重开）、成功清零计数、按调用而非按尝试计数；业务 4xx 与内容审查（空 choices）
   不计数；
2. 备选链：LLM_MAIN_FALLBACK_PROVIDER 配置且主通道开窗时 main 解析切走、冷却窗
   过后切回；未配置（默认）时解析与单例行为和现在完全一致；
3. 告警：熔断开/恢复与滑动窗错误率超阈时 POST 一条 JSON 到 webhook、同一事件
   冷却期内去重、未配置不发、发送失败不影响主流程；
4. boot 警告（§3.1.6）：非回环绑定且 main 无 key 时 validate() 打显式 warning。

全部离线：httpx 层打桩（_ScriptedClient/_StreamStub），webhook 经 monkeypatch
llm_breaker.api_client 捕获，时钟用 tests/_fake_clock 的 FakeClock。
"""

from __future__ import annotations

import time

import httpx
import pytest
from _fake_clock import FakeClock

import app.agent  # noqa: F401  先完成 agent 门面导入：存量导入环 llm_client→facade→web_search→llm_client（同 test_llm_backoff.py 的口径）。
from app.common import llm_breaker, llm_client, model_registry
from app.common.config import settings

MAIN_URL = "https://main.example/v1"
FALLBACK_URL = "https://dashscope.example/v1"


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """同 _fake_clock 的插件版（本文件自带，避免重复注册插件）：假钟驱动冷却窗推进。"""
    clock = FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


# ---------- 打桩 ----------


def _request() -> httpx.Request:
    return httpx.Request("POST", f"{MAIN_URL}/chat/completions")


def _ok_response() -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "usage": {}}, request=_request())


def _empty_choices_response() -> httpx.Response:
    """200 但 choices 为空（内容审查/风控形态）：是"这条请求的问题"，不是通道故障。"""
    return httpx.Response(200, json={"choices": [], "usage": {}}, request=_request())


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = _request()
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status} for tests", request=request, response=response)


class _ScriptedClient:
    """替换 llm_client._get_http_client 的阻塞桩：按脚本逐次返回/抛错，记录外呼 URL。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.urls: list[str] = []

    def post(self, url: str, **_kwargs) -> httpx.Response:
        self.urls.append(url)
        step = self.script.pop(0) if self.script else _ok_response()
        if isinstance(step, Exception):
            raise step
        return step

    def is_closed(self) -> bool:
        return False


class _StreamStub:
    """流式桩：进入 with 即抛预设异常（覆盖计数口径），记录外呼次数。"""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def is_closed(self) -> bool:
        return False

    def stream(self, *_args, **_kwargs) -> _StreamStub:
        self.calls += 1
        return self

    def __enter__(self) -> _StreamStub:
        raise self.exc

    def __exit__(self, *_args) -> bool:
        return False


class _AlertSink:
    """捕获 _fire_alert 的 POST；可注入抛错以测告警失败不伤主流程。"""

    def __init__(self, exc: Exception | None = None) -> None:
        self.posts: list[dict] = []
        self.exc = exc

    def post(self, url: str, json=None, **_kwargs) -> httpx.Response:
        if self.exc is not None:
            raise self.exc
        self.posts.append({"url": url, "payload": json})
        return httpx.Response(200)


def _client(url: str = MAIN_URL) -> llm_client.LLMClient:
    """显式指向测试通道、单次尝试（熔断计数按"调用"口径，不掺重试噪声）。"""
    return llm_client.LLMClient(base_url=url, api_key="test-key", model="test-model", max_attempts=1)


def _messages() -> list[dict]:
    return [{"role": "user", "content": "ping"}]


def _open_main(monkeypatch: pytest.MonkeyPatch, url: str = MAIN_URL) -> _ScriptedClient:
    """把 url 通道打到熔断开窗（阈值 5 次连续通道级失败），返回打桩 client。"""
    stub = _ScriptedClient([httpx.ConnectTimeout("down")] * llm_breaker.CIRCUIT_FAILURE_THRESHOLD)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client(url)
    for _ in range(llm_breaker.CIRCUIT_FAILURE_THRESHOLD):
        with pytest.raises(httpx.ConnectTimeout):
            client.chat_response(_messages())
    assert llm_breaker.is_open(url)
    return stub


# ---------- 1. 熔断：开窗 / 快速失败 / 半开 / 恢复 ----------


def test_consecutive_channel_failures_open_fixed_window(monkeypatch):
    """连续 5 次连接层失败开窗；窗内下一次调用快速失败、零外呼（不重付超时）。"""
    stub = _ScriptedClient([httpx.ConnectTimeout("down")] * 5)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for _ in range(5):
        with pytest.raises(httpx.ConnectTimeout):
            client.chat_response(_messages())
    assert len(stub.urls) == 5
    assert llm_breaker.is_open(MAIN_URL) is True
    with pytest.raises(llm_breaker.LLMCircuitOpen, match="熔断开窗"):
        client.chat_response(_messages())
    assert len(stub.urls) == 5, "开窗期内必须快速失败，不得再外呼"


def test_retryable_status_counts_as_channel_failure(monkeypatch):
    """可重试白名单状态（503）与连接层错误同待遇：计入熔断。"""
    stub = _ScriptedClient([_status_error(503)] * 5)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for _ in range(5):
        with pytest.raises(httpx.HTTPStatusError):
            client.chat_response(_messages())
    assert llm_breaker.is_open(MAIN_URL) is True


def test_business_4xx_never_trips_the_breaker(monkeypatch):
    """业务 4xx 是"这条请求的问题"：连发 8 次 400 也不得开窗。"""
    stub = _ScriptedClient([_status_error(400)] * 8)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for _ in range(8):
        with pytest.raises(httpx.HTTPStatusError):
            client.chat_response(_messages())
    assert len(stub.urls) == 8, "业务错误不受熔断拦截，每次都真实外呼"
    assert llm_breaker.is_open(MAIN_URL) is False


def test_content_moderation_empty_choices_not_channel_failure(monkeypatch):
    """空 choices（内容审查）走 ValueError 语义化路径：通道本身是健康的，不计数。"""
    stub = _ScriptedClient([_empty_choices_response()] * 8)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for _ in range(8):
        with pytest.raises(ValueError, match="LLM 未返回任何候选"):
            client.chat_response(_messages())
    assert llm_breaker.is_open(MAIN_URL) is False


def test_success_resets_consecutive_count(monkeypatch):
    """4 败 1 成 4 败：始终凑不满连续 5 次，永不开窗（抖动容忍）。"""
    down = httpx.ConnectTimeout("down")
    script = [down] * 4 + [_ok_response()] + [down] * 4 + [_ok_response()]
    stub = _ScriptedClient(script)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for index in range(10):
        if index in (4, 9):
            assert client.chat_response(_messages())["message"]["content"] == "ok"
        else:
            with pytest.raises(httpx.ConnectTimeout):
                client.chat_response(_messages())
    assert len(stub.urls) == 10
    assert llm_breaker.is_open(MAIN_URL) is False


def test_failure_counted_per_call_not_per_attempt(monkeypatch, fake_clock):
    """重试耗尽的整次调用只记 1 次通道失败（external_client 口径）：5 次调用=10 次尝试才开窗。"""
    stub = _ScriptedClient([httpx.ConnectTimeout("down")] * 10)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = llm_client.LLMClient(base_url=MAIN_URL, api_key="k", model="m")  # max_attempts=2（默认）
    for _ in range(5):
        with pytest.raises(httpx.ConnectTimeout):
            client.chat_response(_messages())
    assert len(stub.urls) == 10
    assert llm_breaker.is_open(MAIN_URL) is True


def test_half_open_probe_success_closes_window(monkeypatch, fake_clock):
    """冷却窗过后放行一次半开探测：成功即关窗（后续调用照常外呼）。"""
    stub = _open_main(monkeypatch)
    fake_clock.advance(llm_breaker.CIRCUIT_COOLDOWN_SECONDS + 1)
    assert llm_breaker.is_open(MAIN_URL) is False, "冷却窗已过：放行半开探测"
    client = _client()
    assert client.chat_response(_messages())["message"]["content"] == "ok"
    assert len(stub.urls) == 6
    assert llm_breaker.is_open(MAIN_URL) is False
    assert client.chat_response(_messages())["message"]["content"] == "ok"
    assert len(stub.urls) == 7, "关窗后不再快速失败"


def test_half_open_probe_failure_reopens_immediately(monkeypatch, fake_clock):
    """半开探测失败一次即重开窗（标准熔断语义）：不会重付 5 次失败的代价。"""
    stub = _open_main(monkeypatch)
    fake_clock.advance(llm_breaker.CIRCUIT_COOLDOWN_SECONDS + 1)
    stub.script.append(httpx.ConnectTimeout("still down"))
    client = _client()
    with pytest.raises(httpx.ConnectTimeout):
        client.chat_response(_messages())
    assert llm_breaker.is_open(MAIN_URL) is True, "半开探测失败必须立刻重开窗"
    with pytest.raises(llm_breaker.LLMCircuitOpen):
        client.chat_response(_messages())
    assert len(stub.urls) == 6, "重开窗后不再外呼"


def test_open_window_not_extended_by_inflight_failures(monkeypatch, fake_clock):
    """固定窗不续期：开窗时刻起算，窗内的在途失败（竞态）不推迟半开放行。"""
    _open_main(monkeypatch)
    # 直接经 note_failure 模拟"开窗前已放行、开窗后才失败"的在途调用
    llm_breaker.note_failure(MAIN_URL, retryable=True)
    fake_clock.advance(llm_breaker.CIRCUIT_COOLDOWN_SECONDS + 1)
    assert llm_breaker.is_open(MAIN_URL) is False, "在途失败不得续期冷却窗（否则持续流量下永远探不到恢复）"


def test_stream_channel_failure_counts_and_fast_fails(monkeypatch):
    """流式路径同样计数与快速失败：5 次连接层失败开窗，第 6 次零外呼。"""
    stub = _StreamStub(httpx.ConnectError("reset by peer"))
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for _ in range(5):
        with pytest.raises(httpx.ConnectError):
            client.stream_chat(_messages())
    assert llm_breaker.is_open(MAIN_URL) is True
    with pytest.raises(llm_breaker.LLMCircuitOpen):
        client.stream_chat(_messages())
    assert stub.calls == 5, "开窗期内流式调用必须快速失败"


def test_stream_cancel_is_not_channel_failure(monkeypatch):
    """客户端断开（StreamCancelled）不是服务故障：连发 10 次也不计数。"""
    stub = _StreamStub(llm_client.StreamCancelled("client gone"))
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for _ in range(10):
        with pytest.raises(llm_client.StreamCancelled):
            client.stream_chat(_messages())
    assert stub.calls == 10
    assert llm_breaker.is_open(MAIN_URL) is False


# ---------- 2. main 备选链 ----------


@pytest.fixture
def fallback_env(monkeypatch):
    """主通道在 MAIN_URL、备选配 dashscope:qwen-plus（百炼地址打桩到测试域）。"""
    monkeypatch.setattr(settings, "llm_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_api_key", "main-key")
    monkeypatch.setattr(settings, "llm_model", "main-model")
    monkeypatch.setattr(settings, "llm_role_main", "")
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", FALLBACK_URL)
    monkeypatch.setattr(settings, "llm_provider_dashscope_api_key", "fallback-key")
    monkeypatch.setattr(settings, "llm_main_fallback_provider", "dashscope:qwen-plus")


def test_fallback_binding_none_when_unset():
    """默认（键留空）：无备选——这是默认部署形态，必须与改造前完全一致。"""
    assert settings.llm_main_fallback_provider == ""
    assert model_registry.fallback_binding() is None


def test_fallback_binding_parses_provider_and_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_main_fallback_provider", "dashscope:qwen-plus")
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", FALLBACK_URL)
    bound = model_registry.fallback_binding()
    assert bound is not None
    assert bound.provider.name == "dashscope" and bound.base_url == FALLBACK_URL and bound.model == "qwen-plus"


def test_fallback_binding_bare_model_goes_default_provider(monkeypatch):
    """裸模型名 = default provider（与角色键同构）。"""
    monkeypatch.setattr(settings, "llm_main_fallback_provider", "backup-model")
    monkeypatch.setattr(settings, "llm_base_url", MAIN_URL)
    bound = model_registry.fallback_binding()
    assert bound is not None
    assert bound.provider.name == "default" and bound.model == "backup-model"


def test_fallback_binding_missing_model_raises(monkeypatch):
    monkeypatch.setattr(settings, "llm_main_fallback_provider", "dashscope:")
    with pytest.raises(ValueError, match="LLM_MAIN_FALLBACK_PROVIDER 缺模型名"):
        model_registry.fallback_binding()


def test_validate_rejects_fallback_provider_without_base_url(monkeypatch):
    monkeypatch.setattr(settings, "llm_main_fallback_provider", "dashscope:qwen-plus")
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", "")
    with pytest.raises(ValueError, match=r"LLM_MAIN_FALLBACK_PROVIDER.*没配 base_url"):
        model_registry.validate()


def test_validate_accepts_well_formed_fallback(fallback_env):
    model_registry.validate()  # 不抛即通过


def test_no_fallback_configured_keeps_resolution_on_open_channel(monkeypatch):
    """未配置备选（默认）：主通道开窗时解析仍是 main、调用照旧快速失败——零行为差异。"""
    monkeypatch.setattr(settings, "llm_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_main_fallback_provider", "")
    _open_main(monkeypatch)
    assert llm_client._resolve_main_binding().base_url == MAIN_URL
    with pytest.raises(llm_breaker.LLMCircuitOpen):
        llm_client.LLMClient().chat_response(_messages())


def test_fallback_engaged_while_main_open(monkeypatch, fallback_env):
    """主通道开窗：LLMClient() 默认解析切到备选，请求真发到备选地址。"""
    _open_main(monkeypatch)
    stub = _ScriptedClient([_ok_response()])
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = llm_client.LLMClient()  # 不传显式参数：走 main 解析（含备选链）
    assert client.base_url == FALLBACK_URL
    assert client._model == "qwen-plus"
    assert client.chat(_messages()) == "ok"
    assert len(stub.urls) == 1 and stub.urls[0].startswith(FALLBACK_URL)


def test_fallback_switch_back_after_cooldown(monkeypatch, fallback_env, fake_clock):
    """冷却窗过后：main 解析自然切回主通道（备选只是开窗期的临时落点）。"""
    _open_main(monkeypatch)
    assert llm_client.LLMClient().base_url == FALLBACK_URL
    fake_clock.advance(llm_breaker.CIRCUIT_COOLDOWN_SECONDS + 1)
    assert llm_client.LLMClient().base_url == MAIN_URL


def test_role_client_main_follows_fallback(monkeypatch, fallback_env):
    """get_role_client("main") 同样经备选链解析（其余角色不受影响）。"""
    _open_main(monkeypatch)
    assert llm_client.get_role_client("main").base_url == FALLBACK_URL


def test_byok_route_never_switches_to_deployment_fallback(monkeypatch, fallback_env):
    """BYOK 优先：用户网关开窗也不得静默切到部署备选（不烧运营方 key，失败响亮）。"""
    from app.common.llm_route import LLMRoute, use_route

    byok = LLMRoute(base_url="https://byok.example/v1", api_key="user-key", model="user-model", label="probe")
    with use_route(byok):
        # 把用户网关通道打到开窗（同一把通道级熔断对 BYOK 网关同样生效）
        stub = _ScriptedClient([httpx.ConnectTimeout("down")] * llm_breaker.CIRCUIT_FAILURE_THRESHOLD)
        monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
        routed = llm_client.LLMClient(base_url=byok.base_url, api_key=byok.api_key, model=byok.model, max_attempts=1)
        for _ in range(llm_breaker.CIRCUIT_FAILURE_THRESHOLD):
            with pytest.raises(httpx.ConnectTimeout):
                routed.chat_response(_messages())
        assert llm_breaker.is_open(byok.base_url)
        # 路由上下文内的 main 解析仍指向用户网关：开窗 = 快速失败，而不是切备选
        assert llm_client._resolve_main_binding().base_url == byok.base_url
        with pytest.raises(llm_breaker.LLMCircuitOpen):
            llm_client.LLMClient().chat_response(_messages())
        assert stub.urls, "备选通道一次都没被打到（URL 仍全是用户网关）"
        assert all(url.startswith("https://byok.example") for url in stub.urls)


def test_get_llm_client_singleton_rebuilds_on_switch(monkeypatch, fallback_env, fake_clock):
    """单例按解析键重建：健康=main；开窗=备选；冷却后=切回 main；键不变时恒同一实例。"""
    monkeypatch.setattr(llm_client, "_llm_client", None)
    monkeypatch.setattr(llm_client, "_llm_client_key", None)
    healthy = llm_client.get_llm_client()
    assert healthy.base_url == MAIN_URL
    assert llm_client.get_llm_client() is healthy, "键不变时单例身份不变（既有契约）"
    _open_main(monkeypatch)
    switched = llm_client.get_llm_client()
    assert switched is not healthy and switched.base_url == FALLBACK_URL
    assert llm_client.get_llm_client() is switched
    fake_clock.advance(llm_breaker.CIRCUIT_COOLDOWN_SECONDS + 1)
    back = llm_client.get_llm_client()
    assert back is not switched and back.base_url == MAIN_URL


def test_get_llm_client_identity_stable_without_fallback(monkeypatch):
    """未配置备选：主通道即使开窗，单例身份也不变（不需要重建）。"""
    monkeypatch.setattr(settings, "llm_base_url", MAIN_URL)
    monkeypatch.setattr(settings, "llm_main_fallback_provider", "")
    monkeypatch.setattr(llm_client, "_llm_client", None)
    monkeypatch.setattr(llm_client, "_llm_client_key", None)
    _open_main(monkeypatch)
    first = llm_client.get_llm_client()
    assert first.base_url == MAIN_URL
    assert llm_client.get_llm_client() is first


# ---------- 3. 告警 webhook ----------


@pytest.fixture
def alert_webhook(monkeypatch):
    """配置 webhook 并捕获 POST；返回 sink。"""
    monkeypatch.setattr(settings, "llm_alert_webhook_url", "https://alerts.example/hook")
    sink = _AlertSink()
    monkeypatch.setattr(llm_breaker, "api_client", lambda: sink)
    return sink


def _events(sink: _AlertSink) -> list[str]:
    return [post["payload"]["event"] for post in sink.posts]


def test_breaker_open_and_recovered_fire_webhook(monkeypatch, fake_clock, alert_webhook):
    """开窗发 circuit_open；窗内重开不重复发（节流）；探测成功发 circuit_recovered。"""
    stub = _open_main(monkeypatch)
    assert _events(alert_webhook) == ["llm.circuit_open"]
    payload = alert_webhook.posts[0]["payload"]
    assert payload["channel"] == MAIN_URL and payload["event"] == "llm.circuit_open"
    assert alert_webhook.posts[0]["url"] == "https://alerts.example/hook"
    # 半开探测失败 → 重开窗：同一事件在告警冷却期内，不得重复发
    fake_clock.advance(llm_breaker.CIRCUIT_COOLDOWN_SECONDS + 1)
    stub.script.append(httpx.ConnectTimeout("still down"))
    with pytest.raises(httpx.ConnectTimeout):
        _client().chat_response(_messages())
    assert llm_breaker.is_open(MAIN_URL)
    assert _events(alert_webhook) == ["llm.circuit_open"]
    # 再过一个冷却窗：探测成功 → 恢复事件（不同事件键，不受 open 的节流影响）
    fake_clock.advance(llm_breaker.CIRCUIT_COOLDOWN_SECONDS + 1)
    stub.script.append(_ok_response())
    assert _client().chat_response(_messages())["message"]["content"] == "ok"
    assert _events(alert_webhook) == ["llm.circuit_open", "llm.circuit_recovered"]


def test_no_webhook_when_unconfigured(monkeypatch):
    """默认（webhook 留空）：整轮开窗-恢复都不发——只留本地 warning 日志。"""
    assert settings.llm_alert_webhook_url == ""
    sink = _AlertSink()
    monkeypatch.setattr(llm_breaker, "api_client", lambda: sink)
    _open_main(monkeypatch)
    assert sink.posts == []


def test_error_rate_alert_fires_and_throttles(monkeypatch, fake_clock, alert_webhook):
    """滑动窗错误率超阈发 error_rate；窗内再次超阈被同一事件冷却期拦下。"""
    monkeypatch.setattr(llm_breaker, "ALERT_MIN_SAMPLES", 4)
    monkeypatch.setattr(llm_breaker, "ALERT_ERROR_RATE", 0.5)
    stub = _ScriptedClient([httpx.ConnectTimeout("down")] * 3 + [_ok_response()] + [httpx.ConnectTimeout("down")] * 2)
    monkeypatch.setattr(llm_client, "_get_http_client", lambda: stub)
    client = _client()
    for index in range(6):
        if index == 3:
            client.chat_response(_messages())
        else:
            with pytest.raises(httpx.ConnectTimeout):
                client.chat_response(_messages())
    # 3 败 + 1 成（第 4 个样本）时 error_rate=0.75 ≥ 0.5 且样本够 4：恰在此处告警一次
    assert _events(alert_webhook) == ["llm.error_rate"]
    assert alert_webhook.posts[0]["payload"]["detail"]["error_rate"] == 0.75
    # 5/6 样本仍超阈（0.83），但同一 (事件, 通道) 冷却期内不重复发
    assert _events(alert_webhook) == ["llm.error_rate"]


def test_webhook_send_failure_never_breaks_llm_flow(monkeypatch, fake_clock):
    """webhook 抛错只记日志：熔断照常开、后续调用照常快速失败（告警是旁路）。"""
    monkeypatch.setattr(settings, "llm_alert_webhook_url", "https://alerts.example/hook")
    monkeypatch.setattr(llm_breaker, "api_client", lambda: _AlertSink(exc=RuntimeError("hook dead")))
    _open_main(monkeypatch)
    with pytest.raises(llm_breaker.LLMCircuitOpen):
        _client().chat_response(_messages())


# ---------- 4. boot 警告（审计 §3.1.6） ----------


def _registry_warnings(caplog) -> list:
    return [r for r in caplog.records if r.name == "app.common.model_registry" and r.levelno >= 30]


def test_boot_warns_non_loopback_without_main_key(monkeypatch, caplog):
    """非回环绑定 + main 无 key：显式 warning（不是 fail，保有意缺 key 的部署）。

    刻意不钉警告总条数：同一次 validate 里还会响别的启动警告（如 search 角色无联网能力，
    见 test_validate_warns_when_search_role_cannot_search）——钉条数会让两条规则互相绊倒，
    这里只要求「main 缺 key 这一条在」。
    """
    monkeypatch.setattr(settings, "agent_host", "0.0.0.0")
    monkeypatch.setattr(settings, "llm_api_key", "")
    with caplog.at_level("WARNING"):
        model_registry.validate()
    messages = [record.getMessage() for record in _registry_warnings(caplog)]
    assert any("LLM_API_KEY" in message for message in messages), messages


def test_boot_silent_on_loopback_without_key(monkeypatch, caplog):
    """回环绑定（本地开发）缺 key：不警告——哨兵语义保本地开发。"""
    monkeypatch.setattr(settings, "agent_host", "127.0.0.1")
    monkeypatch.setattr(settings, "llm_api_key", "")
    with caplog.at_level("WARNING"):
        model_registry.validate()
    assert _registry_warnings(caplog) == []


def test_boot_silent_non_loopback_with_key(monkeypatch, caplog):
    """非回环 + key 已配 + search 角色真能联网：健康形态，一条警告都没有。

    search 角色也要配到位：只配 main key、search 却落在无联网能力的网关上时，
    validate 会（正确地）警告联网降级——那不是「健康形态」，见
    test_validate_warns_when_search_role_cannot_search。
    """
    monkeypatch.setattr(settings, "agent_host", "0.0.0.0")
    monkeypatch.setattr(settings, "llm_api_key", "sk-configured")
    monkeypatch.setattr(settings, "llm_provider_dashscope_base_url", "https://dashscope.aliyuncs.com")
    monkeypatch.setattr(settings, "llm_role_search", "dashscope:qwen-plus")
    with caplog.at_level("WARNING"):
        model_registry.validate()
    assert _registry_warnings(caplog) == []
