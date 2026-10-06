"""SOURCES.md 机检 + 探针判定逻辑单测（LA1 能进门禁的那一半）。

两件事都在本文件：
1. 机检：`app/agent/data/*.py` 里每个外部端点常量都必须出现在 SOURCES.md——
   方向是单向的"代码 ⊆ 文档"（防代码新增端点绕过登记）；反向不做，文档可以
   描述尚未接入的候选源与已退役源。
2. 探针判定逻辑：classify_status / execute 的分类正确性（401 是活着、404 才是
   死了；连不上 ≠ 死），全部喂 MockTransport 假响应，**不发任何真实请求**——
   探针本体不进 just check，CI 无外网是既有假设。
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from scripts import probe_sources

PKG_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PKG_ROOT / "app" / "agent" / "data"
SOURCES_MD = PKG_ROOT / "SOURCES.md"

# URL 字符集 = RFC3986 常用字符且刻意排除引号/空白/全角标点/花括号：f-string 深链
# （如 f"https://uri.amap.com/marker?position={...}"）在 { 处自然截断，中文行文的
# 全角标点也自然截断。比对用前缀 = scheme://host/path（去 query）：改路径 = 改端点，
# 必须重新登记；只调 query 参数不算端点漂移。
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&+,;=%-]+")


def _code_endpoint_prefixes() -> set[str]:
    """data 域源码里出现过的全部端点前缀（含 docstring 里的重复，集合天然去重）。"""
    prefixes: set[str] = set()
    for path in sorted(DATA_DIR.glob("*.py")):
        for match in _URL_RE.finditer(path.read_text(encoding="utf-8")):
            prefixes.add(match.group(0).split("?", 1)[0])
    return prefixes


# ---- 机检：代码 ⊆ SOURCES.md ------------------------------------------------


def test_every_data_endpoint_is_documented():
    sources = SOURCES_MD.read_text(encoding="utf-8")
    prefixes = _code_endpoint_prefixes()
    missing = sorted(p for p in prefixes if p not in sources)
    assert not missing, f"以下端点常量未登记进 SOURCES.md（方向：代码⊆文档）：{missing}"


def test_extractor_not_silent():
    # 提取器自检：正则一旦空转，上面的机检就形同虚设。模块常量与 f-string 内联深链两类都要能提。
    prefixes = _code_endpoint_prefixes()
    assert "https://serpapi.com/search.json" in prefixes
    assert "https://uri.amap.com/marker" in prefixes
    assert "https://www.google.com/maps/search/" in prefixes


# ---- 探针判定逻辑（MockTransport 假响应，零真实请求）------------------------


def test_classify_status_alive_vs_dead():
    assert probe_sources.classify_status(200) == probe_sources.OK
    assert probe_sources.classify_status(204) == probe_sources.OK
    assert probe_sources.classify_status(401) == probe_sources.AUTH_REQUIRED  # 活着，缺 key
    assert probe_sources.classify_status(403) == probe_sources.AUTH_REQUIRED
    assert probe_sources.classify_status(429) == probe_sources.QUOTA
    assert probe_sources.classify_status(404) == probe_sources.DEAD  # 只有 404 是死
    assert probe_sources.classify_status(500) == probe_sources.ERROR
    # follow_redirects 开着还见到 3xx = 重定向循环之类的异常行为，不该伪装成活/死
    assert probe_sources.classify_status(302) == probe_sources.ERROR


def test_execute_classifies_mock_responses():
    statuses = {"a.test": 200, "b.test": 401, "c.test": 404}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(statuses[request.url.host])

    results = {
        r.probe.name: r
        for r in probe_sources.execute(
            [
                probe_sources.Probe("ok", "https://a.test/x"),
                probe_sources.Probe("auth", "https://b.test/y"),
                probe_sources.Probe("dead", "https://c.test/z"),
            ],
            transport=httpx.MockTransport(handler),
        )
    }
    assert results["ok"].verdict == probe_sources.OK
    assert results["auth"].verdict == probe_sources.AUTH_REQUIRED
    assert results["dead"].verdict == probe_sources.DEAD


def test_execute_marks_transport_error_unreachable():
    # 连不上 ≠ 死：本机代理/SSL 假象必须落在 unreachable，绝不允许判死（Hotellook 教训的另一半）
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    (result,) = probe_sources.execute(
        [probe_sources.Probe("suspect", "https://down.test/")],
        transport=httpx.MockTransport(handler),
    )
    assert result.verdict == probe_sources.UNREACHABLE
    assert result.status is None


def test_execute_records_redirect_chain():
    # 重定向链本身是事实（Hotellook search 302 改嫁 aviasales 这类信号要留在备注里）
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "old.test":
            return httpx.Response(302, headers={"Location": "https://new.test/land"})
        return httpx.Response(200)

    (result,) = probe_sources.execute(
        [probe_sources.Probe("moved", "https://old.test/hotels")],
        transport=httpx.MockTransport(handler),
    )
    assert result.verdict == probe_sources.OK
    assert "old.test -> new.test" in result.note
    assert result.final_url.startswith("https://new.test/")


def test_exit_code_flags_only_unreachable():
    assert probe_sources.exit_code([]) == 0
    dead = probe_sources.ProbeResult(probe_sources.Probe("x", "https://x.test/"), probe_sources.DEAD, 404)
    assert probe_sources.exit_code([dead]) == 0  # 死也是事实，探针成功了
    unreachable = probe_sources.ProbeResult(dead.probe, probe_sources.UNREACHABLE)
    assert probe_sources.exit_code([dead, unreachable]) == 3  # 网络层不可判定，先复核再下结论
