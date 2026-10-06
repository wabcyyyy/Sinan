"""OTM 通道纪律单测（L1 补充 1 + PR-11 验收「降级链事件可见」）。

- 三客户端显式限速与重试登记（geo/radius 0.35s 不重试、detail 1.0s 重试 1 次、
  Nominatim 1.1s 重试 1 次——政策口径直接钉在字段上）；
- 请求必须带可联系 UA（OTM 同 Nominatim 先例）；
- fake clock 断言相邻外呼 ≥ min_interval；
- 供应商连挂 → 熔断开窗（warning 日志可见）→ 检索降级为空池且零外呼。
"""

from __future__ import annotations

import httpx
import pytest

from app.agent.data import places
from app.common.config import settings
from app.common.http_client import configure_clients

pytest_plugins = ["_fake_clock"]


def test_otm_clients_declare_rate_limits_and_retry_policy():
    """限速与重试是政策，不是实现细节：直接钉字段值（L1 补充 1）。"""
    assert places._otm_geo_client.min_interval_seconds == pytest.approx(0.35)
    assert places._otm_radius_client.min_interval_seconds == pytest.approx(0.35)
    assert places._otm_detail_client.min_interval_seconds == pytest.approx(1.0)
    assert places._nominatim_client.min_interval_seconds == pytest.approx(1.1), "Nominatim 公共实例 1 rps 政策"
    # 重试只上幂等 GET：detail（enrich 高频）、Nominatim（公共实例抖动）与 radius
    # （P2-8 评估后从 0 抬到 1：免费源、池空会直接表现为 draft_only）各 1 次；geo 不重试
    assert places._otm_detail_client.retry_attempts == 1
    assert places._nominatim_client.retry_attempts == 1
    assert places._otm_geo_client.retry_attempts == 0
    assert places._otm_radius_client.retry_attempts == 1


def test_otm_requests_carry_contact_user_agent(monkeypatch):
    monkeypatch.setattr(settings, "otm_api_key", "test-key")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("User-Agent", "")
        return httpx.Response(200, json={"name": "Paris", "country": "France", "lat": 48.85, "lon": 2.35})

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        row = places.resolve_city_center("Paris")
    finally:
        configure_clients(api=None)
    assert row is not None
    assert seen["ua"].startswith("TravelAssistantDemo/1.0")
    assert "contact=" in seen["ua"], "UA 必须带联系方式（免费源礼貌纪律）"


def test_otm_throttle_enforces_min_interval(fake_clock, monkeypatch):
    """fake clock 断言相邻外呼间隔 ≥ min_interval（PR-11 验收口径）。"""
    places._otm_geo_client.reset_runtime_state()  # 清掉此前用例留下的真实时钟车道戳
    monkeypatch.setattr(settings, "otm_api_key", "test-key")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"name": "x", "country": "y", "lat": 1.0, "lon": 2.0})

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert places.resolve_city_center("Paris") is not None
        assert places.resolve_city_center("Lyon") is not None
        assert len(requests) == 2
        # 第二次调用必须预约到 +0.35s 的时间槽（假钟把 sleep 换成时钟推进）
        assert any(s == pytest.approx(0.35, abs=1e-6) for s in fake_clock.sleeps), (
            f"相邻外呼必须间隔 0.35s，实际 sleeps={fake_clock.sleeps}"
        )
    finally:
        configure_clients(api=None)


def test_provider_outage_trips_breaker_and_degrades_pool_visibly(fake_clock, monkeypatch, caplog):
    """供应商连挂 → 熔断开窗有 warning 日志（降级不静默），检索降级为空池且零外呼。

    对应 PR-11 验收「供应商连挂 → 降级链（OTM→联网→LLM 世界知识）事件可见」
    的第一环：OTM 池降级为空后，联网补池与 LLM 世界知识照既有 research 链路
    接手（EvidencePack.gaps 如实记录），本测钉住的是"OTM 这一环的缺席可见"。

    重试与熔断的交互（P2-8 改 radius 为 1 次重试后显式钉住）：
    - 熔断按**逻辑调用**记失败，不按重试次数（`call()` 里 `_record_outcome` 只调一次），
      所以阈值仍是"5 次连挂"，不会被重试提前触发；
    - 每次逻辑调用真的外呼 `1 + retry_attempts` 次（免费源、重试重新预约车道槽），
      所以真实请求数 = 5 × (1 + retry)。
    """
    places._otm_radius_client.reset_runtime_state()  # 清掉此前用例留下的真实时钟车道戳
    monkeypatch.setattr(settings, "otm_api_key", "test-key")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        raise httpx.ConnectError("provider down")

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    per_call_attempts = 1 + places._otm_radius_client.retry_attempts
    try:
        with caplog.at_level("WARNING", logger="app.common.external_client"):
            # radius 客户端熔断阈值 5：连挂 5 次后开窗
            for i in range(5):
                assert places.search_places_near(48.85 + i, 2.35, city="Paris") == []
            assert "breaker OPEN" in caplog.text, "熔断开窗必须留 warning 日志"
            assert len(requests) == 5 * per_call_attempts, "5 次逻辑调用（各含重试）后熔断开窗"
            # 窗口内：零外呼、快速降级为空池
            assert places.search_places_near(99.0, 99.0, city="Nowhere") == []
            assert len(requests) == 5 * per_call_attempts, "熔断窗口内不得再打外部"
    finally:
        configure_clients(api=None)


def test_enrich_preserves_order_and_merges_details(monkeypatch):
    """并行 enrich 保序：输出顺序与串行版一致（eval/快照零 diff 的前提）。"""
    monkeypatch.setattr(settings, "otm_api_key", "k")  # 无 key 时 enrich 按纪律早退
    rows = [
        {"xid": f"x{i}", "name": f"spot{i}", "point": {"lat": 48.0 + i, "lon": 2.0}, "rate": 9 - i} for i in range(4)
    ]

    def fake_detail(xid: str) -> dict | None:
        return {"xid": xid, "image": f"img-{xid}"} if xid == "x1" else None

    monkeypatch.setattr(places, "place_detail", fake_detail)
    out = places.enrich_with_details(rows, top=3)
    assert [p["name"] for p in out] == ["spot0", "spot1", "spot2", "spot3"], "顺序必须与输入一致"
    assert out[1]["image"] == "img-x1", "详情字段仍按 xid 对号入座"
    assert "image" not in out[0] and "image" not in out[2], "无详情的行原地保留"


def test_enrich_handles_empty_pool():
    assert places.enrich_with_details([]) == []


def test_otm_kinds_mapping_only_contains_verified_categories():
    """kinds 合法集合锁死（2026-09-30 对真实 API 逐值实测，成都 30.66,104.06）。

    住宿类上游拼 **accomodations**（单 m）：按正确英语 accommodations 发送必 400
    "Unknown category name"，整个酒店域 OTM 池静默归零——这个拼写曾在代码里
    存活近月、被误记成"免费 key 不支持"。往映射里加/改 kinds 前必须先用真实
    key 复测（OTM 无 kinds 列表端点，只能实测），并同步扩充本集合。
    """
    from app.agent.tools import impl as tools

    verified = {
        "interesting_places",
        "foods",
        "accomodations",
        "theatres_and_entertainments",
        "amusements",
        "cultural",
    }
    used = {kind for kinds in tools._OTM_KINDS_BY_CATEGORY.values() for kind in kinds}
    assert used <= verified, f"未经实测的 kinds: {sorted(used - verified)}"
    assert tools._OTM_KINDS_BY_CATEGORY["hotel"] == ("accomodations",), "上游单 m 拼写，双 m 必 400"


# ---------- R3-F2 共享限速组 / R3-F5 error 负缓存（2026-10-05） ----------


def test_otm_rate_group_shares_timeline_across_clients(fake_clock, monkeypatch):
    """R3-F2：geo 与 radius 同组（rate_group="otm"）共用车道时间线。

    per-instance 节流下 geo+radius+detail 三车道叠加 ≈6.8 rps 破 5 rps 自设约束；
    共享后 geo 的下一个 radius 外呼必须预约 +0.35s 槽——它读到的是 geo 刚写进
    组级共享表的最近时间戳（per-instance 表则两表各自从 0 起步、并行打出去）。
    """
    places._otm_geo_client.reset_runtime_state()
    monkeypatch.setattr(settings, "otm_api_key", "test-key")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if "geoname" in str(request.url):
            return httpx.Response(200, json={"name": "x", "country": "y", "lat": 1.0, "lon": 2.0})
        return httpx.Response(200, json=[])

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert places.resolve_city_center("Paris") is not None
        assert places.search_places_near(1.0, 2.0, limit=3) == []
        assert len(requests) == 2
        assert any(s == pytest.approx(0.35, abs=1e-6) for s in fake_clock.sleeps), (
            f"radius 必须接在 geo 的共享时间线之后（+0.35s），实际 sleeps={fake_clock.sleeps}"
        )
    finally:
        configure_clients(api=None)


def test_otm_error_body_is_negative_cached_and_counts_toward_breaker(fake_clock, monkeypatch, caplog):
    """R3-F5：「HTTP 200 + {"error": …}」不得当成功正缓存 6-24h。

    旧实现 error 解包在缓存之后——error dict 正缓存（geo 24h）且 `_record_outcome(False)`
    让熔断计数永不推进。现在 loader 内识别 error 就地抛出：走负缓存（短 TTL）+ 熔断计数，
    连挂到阈值（5）开窗有 warning。
    """
    places._otm_geo_client.reset_runtime_state()
    monkeypatch.setattr(settings, "otm_api_key", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "daily quota exceeded"})

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        assert places.resolve_city_center("Paris") is None
        assert places.resolve_city_center("Paris") is None, "error 结果必须负缓存：窗口内不得重发外呼"
        for city in ("Rome", "Oslo", "Cairo", "Lima"):
            assert places.resolve_city_center(city) is None
        assert "breaker OPEN" in caplog.text, "error dict 必须计入熔断计数（5 连挂开窗）"
    finally:
        configure_clients(api=None)
