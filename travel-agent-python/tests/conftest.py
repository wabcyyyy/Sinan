"""全局测试夹具：默认关闭内部 Token，避免本地 .env 配置导致 HTTP 测试 401。

需要测鉴权的用例可自行 monkeypatch settings.agent_internal_token。
"""

import os
import tempfile
from pathlib import Path

import pytest

# 会话签名密钥：少数用例用 TestClient(main.app) 会触发 lifespan，而启动守卫要求
# ≥32 字符（切流量后本服务自己签发 TA_AUTH）。带 example 标记，属占位符不是真密钥。
os.environ.setdefault("JWT_SECRET", "example-only-pytest-jwt-signing-material")

# 图检查点库是进程级 SQLite（图编译期绑死，PR-3）：测试进程指向一次性文件，
# 免得 data/checkpoints.sqlite3 里旧 thread 的确定性 id（day-{id}-{n}）跨进程串味。
os.environ.setdefault(
    "CHECKPOINT_DB_PATH",
    str(Path(tempfile.mkdtemp(prefix="pytest-checkpoints-")) / "checkpoints.sqlite3"),
)


@pytest.fixture(autouse=True)
def _default_clear_agent_internal_token(monkeypatch):
    from app.common.config import settings

    monkeypatch.setattr(settings, "agent_internal_token", "")
    yield


@pytest.fixture(autouse=True)
def _neutralize_ip_and_user_limits(monkeypatch):
    """限速/配额在离线套件里默认放到极大值。

    上面的 `_pin_redis_unreachable` 把所有窗口都打到 `state_and_sessions` 的**进程内**
    兜底实现上，而那张表整个 pytest 会话共用：不放开的话，"某个用户在本进程里的第 7 次
    generate"会随机撞进 429，测试之间互相污染。专项用例自行 monkeypatch 回小值
    （见 tests/test_quota_service.py、tests/test_media_endpoints.py 的限速用例）。
    """
    from app.common.config import settings

    monkeypatch.setattr(settings, "user_llm_runs_per_minute", 100_000)
    monkeypatch.setattr(settings, "user_daily_llm_runs", 100_000)
    monkeypatch.setattr(settings, "public_rate_limit_per_minute", 100_000)
    monkeypatch.setattr(settings, "share_rate_limit_per_minute", 100_000)
    # 按需实时价窗口（审查 P1-8 新增）与 agent 面速率桶（同批）：都落在
    # `state_and_sessions` 的进程内兜底表里，整个 pytest 会话共用——不放开
    # 会在无关用例之间累积命中 429。专项用例自行 monkeypatch 回小值。
    monkeypatch.setattr(settings, "user_live_quotes_per_minute", 100_000)
    monkeypatch.setattr(settings, "agent_rate_limit_per_minute", 100_000)
    yield


@pytest.fixture(autouse=True)
def _pin_redis_unreachable(monkeypatch):
    """离线套件的既有假设：默认环境**无可用 Redis**（CI 即如此）。

    本机 6380 若起着常驻 Redis，cache_store 会真连上去：详情缓存这类带 TTL 的键
    会跨 pytest 进程残留（实测：cover unsplash 用例读到上一进程 upload 用例写的
    旧详情——它把 evict 桩掉，无法清键），幂等占位同理。统一钉死到不可达端口，
    强制各模块走进程内降级；专门测 Redis 行为的用例自行 monkeypatch 覆盖本值。
    """
    from app.common.config import settings

    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    yield


@pytest.fixture(autouse=True)
def _disable_weather(monkeypatch):
    """离线套件默认关闭 Open-Meteo 天气（C3.1）：生成链路的 synthesize 会取天气，
    不关会让研究路径打真实外网；天气语义专门由 test_weather.py 显式开启覆盖。"""
    from app.common.config import settings

    monkeypatch.setattr(settings, "weather_enabled", False)
    yield


@pytest.fixture(autouse=True)
def _disable_live_geocoding(monkeypatch):
    """离线套件默认关掉一切外部地理数据源（与上面的天气开关同构）。

    2026-09-18 量测时发现的存量问题：`local_ground` 在单测里会真打公网解析
    「拙政园」，于是同一断言在 CI（无外网）与本机（有外网）走的是不同分支——
    用例"绿"的原因不一致。存在性判定层同理：OTM 有真 key 时，连"删不删点位"
    这种判定都会随本机密钥漂移。

    需要接地/解析语义的用例请显式打桩（`existence.resolve_poi` 或
    `landing.local_ground`），见 test_existence / test_audit_fixes / test_day_stream。
    """
    from app.common.config import settings

    monkeypatch.setattr(settings, "nominatim_enabled", False)
    monkeypatch.setattr(settings, "otm_api_key", "")
    monkeypatch.setattr(settings, "amap_web_key", "")
    monkeypatch.setattr(settings, "google_places_api_key", "")
    # 真实报价数据面（L12 新增，L15 补钉）：token 留空即「零外呼」，但本机 .env
    # 里填过 token 的开发机上，任何走到取价的用例都会真打公网——离线纪律不能
    # 依赖"开发机恰好没配"。这里与上面几个收费源 key 同待遇钉死。
    monkeypatch.setattr(settings, "travelpayouts_token", "")
    monkeypatch.setattr(settings, "serpapi_key", "")
    yield


@pytest.fixture(autouse=True)
def _clear_external_client_caches():
    """G-3.2：外部调用基类带进程内 TTL 缓存，跨用例会互相污染。

    典型症状：前一个用例把「无图」/「搜不到」的负结果存进缓存，后一个用例
    打了桩却拿到缓存的 None，表现为"桩没生效"。这里在用例前后各清一次，
    让每个用例从干净缓存出发（正/负 TTL 的语义由 test_external_client 专门覆盖）。
    """
    from app.agent.data import flight_prices, live_quotes, places, pricing, weather, web_search
    from app.agent.tools import impl as tools

    clients = (
        tools._image_client,
        tools._wiki_client,
        places._otm_geo_client,
        places._otm_radius_client,
        places._otm_detail_client,
        places._nominatim_client,
        pricing._price_client,
        web_search._search_client,
        weather._weather_client,
        flight_prices._flight_client,
        live_quotes._serpapi_client,
    )
    live_quotes.reset_quota_for_tests()
    for client in clients:
        client.clear_cache()
        client.reset_runtime_state()
    yield
    live_quotes.reset_quota_for_tests()
    for client in clients:
        client.clear_cache()
        client.reset_runtime_state()


@pytest.fixture(autouse=True)
def _reset_existence_memo():
    """存在性判定按 (城市, 名字) 记忆化，跨用例复用会把上一个用例的结论漏进来。"""
    from app.agent.grounding.existence import reset_existence_state

    reset_existence_state()
    yield
    reset_existence_state()


@pytest.fixture(autouse=True)
def _reset_addon_state_cache():
    """addons.is_enabled 的进程内 TTL 缓存（5s）同样跨用例污染。

    前序用例在它自己的 settings 补丁下读到的布尔值会被缓存住；后续用例改回
    settings 也拿不到重读——实测：test_research_agents 在 live_price_search=False
    下触发一次 is_enabled("live_price")，紧随其后的 test_format_output_golden
    把开关钉回 True 仍读到缓存的 False，实时价预算归零，golden 快照 10 处 diff
    （只有把两个文件换个顺序或走全量字母序才不红）。每个用例前后各 reset 一次，
    让 is_enabled 在当前用例的 settings 口径下重新解析；专门测缓存/TTL 语义的
    用例（test_addons）都在单用例内自洽，不受影响。
    """
    from app.common import addons

    addons.reset_cache()
    yield
    addons.reset_cache()
