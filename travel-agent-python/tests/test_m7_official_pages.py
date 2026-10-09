"""M7（spec 2026-10-08 §13.1）官方页面字段证据测试：fixture 否决旧票价 + 抓取守卫。

三块覆盖：
- 灵隐寺离线 fixture（采集日期 2026-10-08，见 fixture 文件头注释）→ 旧收费话术被
  冲突证据否决：值撤回为 unknown、核验状态降级待复核；坐标证据不传染票价（E14 前半）；
- 官方请求失败/守卫拒绝 → 字段保持原状（unknown 不被误判成免费/不营业/不用预约，
  E14 后半）；
- 抓取守卫单测：私网/回环地址、非 http(s)、超大响应、多跳重定向一律拒绝（全离线：
  私网用例只用字面 IP 与 localhost，不发生真实外呼；公网路径用 httpx.MockTransport 注入）。
- Nominatim 合规（spec §13.1 引官方使用政策）：1 rps 限制器是**应用级单例**
  （模块级一个 ExternalClient，所有线程共享一条 1.1s 时间线）、可识别 User-Agent、
  同一点位缓存复用不重复请求。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from app.agent.data import places
from app.agent.grounding.official_pages import (
    OFFICIAL_PAGE_CLIENT,
    OfficialPageError,
    OfficialPageEvidence,
    apply_evidence_conflicts,
    collect_field_evidence,
    extract_evidence,
    fetch_official_page,
)
from app.common.http_client import configure_clients, contact_user_agent
from app.schemas.trip import FactEvidence

FIXTURE_URL = "https://www.lingyinsi.org/detail_57_19455.html"
OBSERVED_AT = "2026-10-08T10:00:00+00:00"
FIXTURE_HTML = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "lingyinsi_detail_57_19455.html"
).read_text(encoding="utf-8")


def _evidence() -> OfficialPageEvidence:
    evidence = extract_evidence(FIXTURE_HTML, fetched_at=OBSERVED_AT, url=FIXTURE_URL)
    assert evidence is not None
    return evidence


# ---- fixture 否决旧票价（E14 前半） -------------------------------------------


def test_fixture_extracts_free_ticket_and_reservation() -> None:
    """灵隐寺 fixture（2025-12-01 起免票 + 实名预约分时）确定性提取出两个字段证据。"""
    evidence = _evidence()
    assert evidence is not None, "登记过的官方页面要点必须能被 adapter 识别"
    assert evidence.url == FIXTURE_URL
    assert evidence.published_at == "2025-11-20", "页面写明的发布时间被记录"
    assert evidence.fields == {"ticket": "free", "reservation": "required"}
    assert "免费开放" in evidence.excerpt and "实名预约分时" in evidence.excerpt, "摘录定位证据出处"


def test_conflicting_stale_ticket_is_vetoed_not_auto_flipped() -> None:
    """旧收费话术（80 元 estimated）与新证据冲突：记冲突 + 值撤回为 None + 降级待复核。

    不自动改成"免费"：日期适用性/页面时效未经二次核实，冲突的字段进入 unknown
    待确认（"旧收费话术被冲突证据否决"的保守语义）。
    """
    values: dict = {"ticket": 80.0, "reservation": False}
    fields: dict = {"ticket": FactEvidence(value_kind="estimated", verification_status="unverified")}
    conflicts = apply_evidence_conflicts(values, fields, _evidence())
    assert [c.field for c in conflicts] == ["ticket", "reservation"]
    ticket_conflict = conflicts[0]
    assert ticket_conflict.current_value == 80.0
    assert ticket_conflict.evidence_url == FIXTURE_URL
    assert ticket_conflict.observed_at == OBSERVED_AT
    assert values["ticket"] is None, "80 元的旧话术被否决（撤回），不是被改成 0"
    fact = fields["ticket"]
    assert fact.verification_status == "unverified"
    assert fact.review_requirement == "before_departure"
    assert fact.source_url is None, "字段值已 unknown：不挂官方引用冒充已核实"
    assert values["reservation"] is None


def test_matching_or_missing_fields_adopt_evidence_with_citation() -> None:
    """无当前值/值一致：采信官方证据并挂引用（observed + partially_verified + 时间）。"""
    values: dict = {}
    fields: dict = {}
    conflicts = apply_evidence_conflicts(values, fields, _evidence())
    assert conflicts == []
    assert values["ticket"] == 0.0, "费用 0 有官方证据支持（spec：费用 0 必须有证据）"
    assert values["reservation"] is True
    fact = fields["ticket"]
    assert fact.source_url == FIXTURE_URL
    assert fact.retrieved_at == OBSERVED_AT
    assert fact.provider == "official-page"
    assert fact.verification_status == "partially_verified", "页面快照无时效担保，不冒充 verified"
    assert fact.value_kind == "observed"


def test_coordinate_evidence_never_leaks_into_ticket() -> None:
    """坐标已核实不得传染票价（E14）：official_pages 只触碰证据声明的字段键。"""
    values: dict = {"ticket": 45.0}
    coords = FactEvidence(verification_status="verified", value_kind="observed")
    fields: dict = {"coordinates": coords, "ticket": FactEvidence(value_kind="estimated")}
    apply_evidence_conflicts(values, fields, _evidence())
    assert coords.verification_status == "verified" and coords.value_kind == "observed"
    assert fields["ticket"].verification_status == "unverified"


def test_unrecognizable_page_yields_no_evidence() -> None:
    """认不出的页面 → None（unknown），宁缺毋假：不产出似是而非的字段值。"""
    assert extract_evidence("<html><body>今日闭园</body></html>", fetched_at=OBSERVED_AT) is None
    assert extract_evidence("", fetched_at=OBSERVED_AT) is None


# ---- 请求失败 → 字段保持 unknown（E14 后半） -----------------------------------


def test_fetch_failure_keeps_fields_unknown(monkeypatch) -> None:
    """官方请求失败：零冲突、零升级——不把失败误判成免费/不营业/不用预约。"""
    from app.agent.grounding import official_pages

    def boom(url: str, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(official_pages, "fetch_official_page", boom)
    values: dict = {"ticket": 80.0}
    fields: dict = {"ticket": FactEvidence(value_kind="estimated")}
    conflicts = collect_field_evidence(values, fields, FIXTURE_URL)
    assert conflicts == []
    assert values["ticket"] == 80.0, "失败不撤回旧估算（它仍是当前最好的估算口径）"
    fact = fields["ticket"]
    assert fact.verification_status == "unverified" and fact.value_kind == "estimated"
    assert fact.source_url is None, "失败不产生引用支持"


def test_unparseable_fetch_result_keeps_fields_unknown(monkeypatch) -> None:
    """抓到了但解析不出（None 证据）：同样零改动。"""
    from app.agent.grounding import official_pages

    monkeypatch.setattr(official_pages, "fetch_official_page", lambda url, **kwargs: None)
    values: dict = {}
    fields: dict = {}
    assert collect_field_evidence(values, fields, FIXTURE_URL) == []
    assert values == {} and fields == {}


# ---- 抓取守卫 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/detail.html",
        "http://10.1.2.3/detail.html",
        "http://172.16.0.9/detail.html",
        "http://192.168.1.1/detail.html",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/detail.html",
        "http://localhost/detail.html",
        "ftp://www.lingyinsi.org/detail_57_19455.html",
        "file:///etc/hosts",
        "https://nonexistent-host-for-m7-guard-test.invalid/detail.html",
    ],
)
def test_guard_rejects_private_and_non_http_targets(url: str) -> None:
    """私网/回环/链路本地/非 http(s)/不可解析主机一律拒绝（fail closed）。"""
    with pytest.raises(OfficialPageError):
        fetch_official_page(url, http_client=httpx.Client())


def test_guard_rejects_redirect_to_private_and_multi_hop(monkeypatch, caplog) -> None:
    """单跳重定向允许（公网目标）；重定向到私网、二跳重定向一律拒绝（降级 None）。

    公网域名经 _host_addresses 接缝离线解析（不触真实 DNS）；私网目标走真实
    字面 IP 判定。"私网目标从未被 HTTP 请求到"证明守卫在网络层之前拦下。
    """
    import logging

    from app.agent.grounding import official_pages

    real_addresses = official_pages._host_addresses
    monkeypatch.setattr(
        official_pages,
        "_host_addresses",
        lambda host: ["93.184.216.34"] if host.endswith("example.com") else real_addresses(host),
    )
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "public.example.com":
            if request.url.path == "/jump":
                return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})
            if request.url.path == "/chain":
                return httpx.Response(302, headers={"Location": "https://hop.example.com/next"})
            if request.url.path == "/ok":
                return httpx.Response(200, text=FIXTURE_HTML)
        if request.url.host == "hop.example.com":
            return httpx.Response(302, headers={"Location": "https://public.example.com/ok"})
        raise AssertionError(f"守卫不应放行该请求：{request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        assert fetch_official_page("https://public.example.com/jump", http_client=client) is None
        assert not any("127.0.0.1" in url for url in requested), "私网重定向目标没有被请求到"
        assert fetch_official_page("https://public.example.com/chain", http_client=client) is None
        assert sum("public.example.com/ok" in url for url in requested) == 0, "二跳重定向未跟随"
        evidence = fetch_official_page("https://public.example.com/ok", http_client=client)
    assert evidence is not None and evidence.fields == {"ticket": "free", "reservation": "required"}


def test_guard_rejects_oversized_response(monkeypatch, caplog) -> None:
    """响应超过 2MB 上限：拒收降级 None（不截断后当成功）。"""
    import logging

    from app.agent.grounding import official_pages

    real_addresses = official_pages._host_addresses
    monkeypatch.setattr(
        official_pages,
        "_host_addresses",
        lambda host: ["93.184.216.34"] if host.endswith("example.com") else real_addresses(host),
    )
    big = "x" * (OFFICIAL_PAGE_CLIENT.max_response_bytes + 1)
    handler = lambda request: httpx.Response(200, text=big)  # noqa: E731
    client = httpx.Client(transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.WARNING):
        assert fetch_official_page("https://public.example.com/big", http_client=client) is None
    assert any("response too large" in record.message for record in caplog.records), "超限拒收留痕"


def test_guard_sends_identifiable_user_agent(monkeypatch) -> None:
    """外呼带可识别 UA（与全仓 contact_user_agent 同源）。"""
    from app.agent.grounding import official_pages

    real_addresses = official_pages._host_addresses
    monkeypatch.setattr(
        official_pages,
        "_host_addresses",
        lambda host: ["93.184.216.34"] if host.endswith("example.com") else real_addresses(host),
    )
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent")
        return httpx.Response(200, text=FIXTURE_HTML)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetch_official_page("https://public.example.com/ua-probe", http_client=client)
    assert seen["ua"] == contact_user_agent()
    assert "TravelAssistantDemo" in seen["ua"]


# ---- Nominatim 合规（spec §13.1：官方使用政策） ---------------------------------


def test_nominatim_rate_limit_is_application_level_singleton() -> None:
    """1 rps 是**整应用**的约束：Nominatim 客户端是模块级单例、车道间隔 ≥1 秒。

    不能把限制当每个线程各 1rps——ExternalClient 的车道时间槽记在单例实例上，
    所有线程预约同一条时间线（生成跑在 worker 线程，本断言钉住这一点不回退）。
    """
    client = places._nominatim_client
    assert client.min_interval_seconds is not None and client.min_interval_seconds >= 1.0
    assert places.geocode_place_rows.__globals__["_nominatim_client"] is client, "点名兜底走同一个限流单例"


def test_nominatim_reuses_cache_for_same_query(monkeypatch) -> None:
    """同一点位（同 query）缓存复用：第二次调用零外呼（署名与缓存的政策要求）。"""
    places._nominatim_client.clear_cache()
    calls: list[dict] = []

    def fake_fetch_json(client, http, url, *, params=None, headers=None, **kwargs):
        calls.append({"params": dict(params or {}), "headers": dict(headers or {})})
        return [
            {
                "name": "灵隐寺",
                "display_name": "灵隐寺, 杭州, 浙江, 中国",
                "lat": "30.2408",
                "lon": "120.0972",
                "namedetails": {"name": "灵隐寺", "name:zh-Hans": "灵隐寺"},
            }
        ]

    monkeypatch.setattr(places, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(places.settings, "nominatim_enabled", True)
    first = places.geocode_place_rows("灵隐寺", "杭州", namedetails=True)
    second = places.geocode_place_rows("灵隐寺", "杭州", namedetails=True)
    assert first is not None and first and second is not None and second
    assert len(calls) == 1, "同 query 第二次命中缓存，不重复请求"
    assert calls[0]["headers"]["User-Agent"] == contact_user_agent(), "可识别 User-Agent（政策要求）"


def test_nominatim_lane_timeline_is_shared_across_threads(monkeypatch) -> None:
    """1 rps 落在**单例实例**的车道时间槽上：任何线程预约都排同一条时间线。

    两次预约（模拟两个 worker 线程先后到达）：第二个槽必须严格排在
    第一个槽 + 间隔之后——若限流是每线程各一条时间线，第二个槽会落在当下。
    """
    from app.common.external_client import INTERACTIVE

    client = places._nominatim_client
    monkeypatch.setattr(client, "min_interval_seconds", 0.05)
    client.reset_runtime_state()
    try:
        assert client._acquire_slot(INTERACTIVE) is True
        first = client._lane_last[INTERACTIVE]
        assert client._acquire_slot(INTERACTIVE) is True, "第二个线程的预约在 max_wait 内拿到槽"
        second = client._lane_last[INTERACTIVE]
        assert second >= first + 0.05 - 1e-6, "并发调用共享一条时间线，不是每线程各 1rps"
    finally:
        client.reset_runtime_state()


# ---- 离线纪律哨兵：真实抓取入口在本套件内不许被无守卫调用 -----------------------


def test_offline_suite_never_reaches_real_network(monkeypatch) -> None:
    """api_client 的真实 GET 若被触达立即失败：守卫与提取测试全部离线完成。"""
    configure_clients(image=None, api=None)
    import httpx as _httpx

    def forbidden(*args, **kwargs):
        raise AssertionError("本套件不允许真实网络调用")

    monkeypatch.setattr(_httpx.Client, "get", forbidden)
    with pytest.raises(OfficialPageError):
        # 不可解析主机在守卫处就被拒绝，根本走不到 httpx
        fetch_official_page("https://another-invalid-host.m7test/page")
