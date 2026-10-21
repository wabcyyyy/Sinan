import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent.data import deepseek_search, pricing, web_search
from app.common.model_registry import Provider, RoleBinding, _capabilities


@pytest.fixture
def native(monkeypatch):
    bound = RoleBinding(
        "search",
        Provider("default", "https://api.deepseek.com", "example-key", frozenset({"web_search_api"})),
        "deepseek-flash",
    )
    monkeypatch.setattr(web_search, "binding", lambda role: bound)
    monkeypatch.setattr(web_search, "web_search_enabled", lambda: True)
    web_search._search_client.reset_runtime_state()
    monkeypatch.setattr(web_search._search_client, "min_interval_seconds", 0.000001)
    return bound


@pytest.mark.parametrize("url", ["https://api.deepseek.com", "https://api.deepseek.com/v1/"])
def test_official_search_api_capability(url):
    assert "web_search_api" in _capabilities("default", url)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.deepseek.com.evil.test/v1",
        "https://proxy.test/api.deepseek.com",
        "http://api.deepseek.com",
        "https://api.deepseek.com/proxy",
        "https://api.deepseek.com:8443",
        "https://user:pass@api.deepseek.com",
        "https://api.deepseek.com?proxy=true",
    ],
)
def test_native_search_cannot_send_credentials_for_other_hosts(url):
    assert "web_search_api" not in _capabilities("default", url)


@pytest.mark.parametrize(
    "status,results,billable",
    [(200, [{"title": "西湖", "url": "https://example.test/west-lake"}], True), (200, [], False), (503, [], False)],
)
def test_search_request_and_billing_trace(monkeypatch, native, status, results, billable):
    events = []

    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
        assert request.headers["authorization"] == "Bearer example-key"
        assert json.loads(request.content) == {"query": "杭州" * 64, "count": 4, "need_content": True}
        return httpx.Response(status, json={"results": results})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(deepseek_search, "api_client", lambda: client)
        monkeypatch.setattr(deepseek_search, "record_event", lambda *args, **kwargs: events.append(kwargs))
        if status == 200:
            assert len(deepseek_search.search_pages("杭州" * 100, native)) == len(results)
        else:
            with pytest.raises(httpx.HTTPStatusError):
                deepseek_search.search_pages("杭州" * 100, native)
    assert events[0]["metadata"]["billable"] is billable
    assert events[0]["metadata"]["estimated_cny"] == (0.005 if billable else 0)


def test_extraction_retains_only_entities_in_real_pages(monkeypatch, native):
    monkeypatch.setattr(
        web_search, "search_pages", lambda *args: [{"title": "西湖游览", "url": "https://example.test/west-lake"}]
    )

    def complete(prompt, **kwargs):
        assert "https://example.test/west-lake" in prompt
        assert kwargs["enable_search"] is False
        return '{"items":[{"name":"西湖"},{"name":"编造景点"}]}'

    monkeypatch.setattr(web_search, "get_llm_client", lambda role: SimpleNamespace(complete=complete))
    assert web_search.web_search_json("杭州景点", schema_hint="{}") == {
        "items": [{"name": "西湖", "source_url": "https://example.test/west-lake"}]
    }


@pytest.mark.parametrize("failure", [False, True])
def test_no_memory_fallback_when_search_is_empty_or_fails(monkeypatch, native, failure):
    def pages(*args):
        if failure:
            raise httpx.ReadTimeout("search unavailable")
        return []

    monkeypatch.setattr(web_search, "search_pages", pages)
    monkeypatch.setattr(web_search, "get_llm_client", lambda role: pytest.fail("没有搜索资料不能调用模型冒充联网"))
    assert web_search.web_search_json("杭州景点", schema_hint="{}") is None


def test_native_price_stays_unknown_without_field_level_evidence(monkeypatch, native):
    monkeypatch.setattr(pricing, "search_channel_ready", lambda: True)
    monkeypatch.setattr(pricing, "binding", lambda role: native)
    monkeypatch.setattr(pricing, "get_llm_client", lambda role: pytest.fail("价格没有字段级证据时不得调用模型"))
    assert pricing.query_live_price("杭州", "某酒店", None) is None
    assert pricing.query_live_food_price("杭州", "某餐厅") is None


def test_lost_search_response_keeps_billing_unknown(monkeypatch, native):
    events = []

    def handler(request):
        raise httpx.ReadTimeout("response lost")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(deepseek_search, "api_client", lambda: client)
        monkeypatch.setattr(deepseek_search, "record_event", lambda *args, **kwargs: events.append(kwargs))
        with pytest.raises(httpx.ReadTimeout):
            deepseek_search.search_pages("杭州", native)
    assert events[0]["metadata"]["billable"] is None
    assert events[0]["metadata"]["estimated_cny"] is None


@pytest.mark.parametrize("kind", ["json", "text"])
def test_search_budget_counts_only_cache_miss_and_allows_last_request(monkeypatch, native, kind):
    from app.agent.runtime.run_limits import RunLimits, _active_limits

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [{"title": "西湖", "url": "https://example.test/west-lake"}]})

    monkeypatch.setattr(
        web_search,
        "get_llm_client",
        lambda role: SimpleNamespace(complete=lambda *a, **k: '{"items":[{"name":"西湖"}]}'),
    )
    limits = RunLimits(max_retrievals=1)
    token = _active_limits.set(limits)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(deepseek_search, "api_client", lambda: client)
            search = (
                (lambda q: web_search.web_search_json(q, schema_hint="{}"))
                if kind == "json"
                else web_search.web_search_text
            )
            first = search("杭州")
            assert first and len(calls) == 1 and limits.retrievals == 1
            assert search("杭州") == first
            assert not search("另一个城市")
            assert len(calls) == 1 and limits.retrievals == 1
    finally:
        _active_limits.reset(token)


def test_json_cache_respects_schema_and_provider(monkeypatch, native):
    prompts = []
    monkeypatch.setattr(
        web_search, "search_pages", lambda *a: [{"title": "西湖", "url": "https://example.test/west-lake"}]
    )

    def complete(prompt, **kwargs):
        prompts.append(kwargs["system_prompt"])
        return '{"items":[{"name":"西湖"}]}'

    monkeypatch.setattr(web_search, "get_llm_client", lambda role: SimpleNamespace(complete=complete))
    for hint in ("schema-one", "schema-two", "schema-one"):
        assert web_search.web_search_json("杭州", schema_hint=hint)
    assert len(prompts) == 2
    other = RoleBinding("search", native.provider, "another-model")
    monkeypatch.setattr(web_search, "binding", lambda role: other)
    assert web_search.web_search_json("杭州", schema_hint="schema-one")
    assert len(prompts) == 3


def test_raw_sources_use_cache_and_last_reserved_retrieval_without_llm(monkeypatch, native):
    from app.agent.runtime.run_limits import RunLimits, _active_limits

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "杭州测试博物馆",
                        "url": "https://museum.example/visit",
                        "content": "开放时间09:00-17:00",
                    }
                ]
            },
        )

    limits = RunLimits(max_retrievals=1)
    token = _active_limits.set(limits)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(deepseek_search, "api_client", lambda: client)
            monkeypatch.setattr(web_search, "get_llm_client", lambda role: pytest.fail("来源提取不应调用模型"))
            first = web_search.web_search_sources("测试博物馆开放信息")
            assert first and first[0]["retrieved_at"] and first[0]["content"]
            assert web_search.web_search_sources("测试博物馆开放信息") == first
            assert web_search.web_search_sources("另一次取证") == []
            assert limits.retrievals == 1 and len(calls) == 1
    finally:
        _active_limits.reset(token)


def test_plugin_without_original_pages_returns_no_sources(monkeypatch, native):
    plugin = RoleBinding("search", Provider("proxy", "https://proxy.example", "key", frozenset({"web_search"})), "m")
    monkeypatch.setattr(web_search, "binding", lambda role: plugin)
    monkeypatch.setattr(web_search, "search_pages", lambda *args: pytest.fail("非原生通道不能取原始网页"))
    monkeypatch.setattr(web_search, "search_channel_ready", lambda: True)
    assert web_search.web_search_sources("开放时间") == []
