"""外源健康探针（LA1）：对每个已接外源发一次最小真实请求，产出"死活"事实表。

背景（Hotellook 事故）：外源只在 mock 里存在，CI 无外网——端点死了全部测试仍绿
（Hotellook 下线而三份 eval 与全部单测照样全绿，就是这么发生的）。本脚本把
"外源还活着吗"从人工记忆变成可重复执行的事实产出；**不进 just check**
（CI 无外网是既有假设），手工或夜间跑：

    uv run python scripts/probe_sources.py           # Markdown 表（人读，抄进 SOURCES.md）
    uv run python scripts/probe_sources.py --json    # 机器可读（夜间任务消费）

判定口径（Hotellook 事故的核心教训：**401 是活着，404 才是死了**）：
- 2xx → ok                    服务正常应答；
- 401/403 → auth-required     **活着**，只是缺 key——把"缺 key"误判成"死了"会导致错误的替换决策；
- 429 → quota                 活着，配额/限速耗尽；
- 404 → dead                  端点不存在（engine.hotellook.com 连根路径 404 = 服务下线）；
- 5xx / 其它 → error          到达了上游但行为异常，需人工复核；
- 连接失败/超时 → unreachable  **只说明本机网络层不通，不能据此判死**——本机 DNS 走
  fake-IP 代理，复核方法：DoH（https://dns.google/resolve?name=<host>&type=A）取真实
  IP 后 `curl --resolve <host>:443:<ip> <url>` 直连，排除代理假象再下结论。

边界：探针一律不带 key（401 本身就是"活着"的证据；真实 key 只进本地 .env）；
只报告、不自动改代码；外源清单的单一真源是 SOURCES.md，本脚本的探针清单与之一一
对应，"代码里的端点必须被文档覆盖"的机检见 tests/test_sources_manifest.py。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date

import httpx

# 与 app/agent/data/*.py 各 client 的 _USER_AGENT 同串（Nominatim 政策要求可联系的标识）
USER_AGENT = "TravelAssistantDemo/1.0 (student-project; contact=dev@localhost.invalid)"

# 结论枚举（裸字符串常量而非 Enum：SOURCES.md 表格里直接写同串，肉眼可对照）
OK = "ok"
AUTH_REQUIRED = "auth-required"
QUOTA = "quota"
DEAD = "dead"
ERROR = "error"
UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class Probe:
    """一条最小真实请求：不带 key、不翻页、只问一次。expects 写"活着时的预期"，作判读锚点。"""

    name: str
    url: str
    expects: str = ""
    params: dict[str, str] = field(default_factory=dict)


#: 探针清单与 SOURCES.md 的行一一对应；顺序 = SOURCES.md 行序。每个已接外源一条：
#: 数据源 API + 深链出口（浏览器侧可用性）。DashScope 联网搜索插件不在此列——
#: 它是 LLM 计费通道，探针不做（每次真实生成即验证，死亡在主链路响亮可见）。
PROBES: list[Probe] = [
    # ---- 数据源 API ----
    Probe(
        "open-meteo",
        "https://api.open-meteo.com/v1/forecast",
        "200（免 key）",
        {"latitude": "31.23", "longitude": "121.47", "current": "temperature_2m"},
    ),
    Probe(
        "nominatim",
        "https://nominatim.openstreetmap.org/search",
        "200（免 key，1 rps + UA）",
        {"q": "Beijing", "format": "json", "limit": "1"},
    ),
    Probe(
        "opentripmap",
        "https://api.opentripmap.com/0.1/en/places/radius",
        "401（活着，缺 apikey）",
        {"lat": "55.75", "lon": "37.62", "radius": "1000"},
    ),
    Probe(
        "travelpayouts",
        "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
        "401（活着，缺 token）",
        {"origin": "MOW", "destination": "LED", "currency": "rub", "one_way": "true", "limit": "1"},
    ),
    Probe("serpapi", "https://serpapi.com/search.json", "401（活着，缺 key）", {"engine": "google_flights"}),
    # ---- 深链出口（浏览器侧可用性，非数据 API）----
    # LA2 摘除说明：hotellook cache（404）与 search 深链（302→aviasales 后 403）
    # 已随酒店源改判下架，不再列入探针；历史结论见 SOURCES.md 已退役表。
    Probe("aviasales 深链", "https://www.aviasales.com/", "200"),
    Probe(
        "amap-marker 深链",
        "https://uri.amap.com/marker",
        "200",
        {"position": "116.397,39.909", "name": "demo", "src": "travel-assistant", "callnative": "0"},
    ),
    Probe(
        "amap-search 深链",
        "https://uri.amap.com/search",
        "200",
        {"keyword": "demo", "src": "travel-assistant", "callnative": "0"},
    ),
    Probe(
        "amap-navigation 深链",
        "https://uri.amap.com/navigation",
        "200",
        {
            "from": "116.397,39.909",
            "to": "116.407,39.919",
            "mode": "car",
            "policy": "1",
            "src": "travel-assistant",
            "coordinate": "gaode",
            "callnative": "0",
        },
    ),
    Probe(
        "google-maps-search 深链",
        "https://www.google.com/maps/search/",
        "200（视本机出口而定）",
        {"api": "1", "query": "Eiffel Tower"},
    ),
    Probe(
        "google-maps-dir 深链",
        "https://www.google.com/maps/dir/",
        "200（视本机出口而定）",
        {"api": "1", "origin": "48.8584,2.2945", "destination": "48.8606,2.3376"},
    ),
]


@dataclass(frozen=True)
class ProbeResult:
    probe: Probe
    verdict: str
    status: int | None = None
    note: str = ""
    final_url: str = ""


def classify_status(status: int) -> str:
    """状态码 → 结论枚举。判定口径见模块 docstring；分类正确性由单测锁死。"""
    if 200 <= status < 300:
        return OK
    if status in (401, 403):
        return AUTH_REQUIRED
    if status == 429:
        return QUOTA
    if status == 404:
        return DEAD
    return ERROR


def execute(
    probes: list[Probe],
    *,
    timeout: float = 15.0,
    transport: httpx.BaseTransport | None = None,
) -> list[ProbeResult]:
    """顺序跑完全部探针。transport 仅供测试注入 MockTransport——**单测不发真实请求**。"""
    results: list[ProbeResult] = []
    with httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        timeout=timeout,
        follow_redirects=True,
        transport=transport,
    ) as client:
        for probe in probes:
            results.append(_run_one(client, probe))
    return results


def _run_one(client: httpx.Client, probe: Probe) -> ProbeResult:
    try:
        resp = client.get(probe.url, params=probe.params or None)
    except httpx.TransportError as exc:
        return ProbeResult(probe, UNREACHABLE, note=f"{type(exc).__name__}: {exc}"[:160])
    except Exception as exc:  # 非网络层的意外异常：如实标 error，不让单条探针炸掉整轮
        return ProbeResult(probe, ERROR, note=f"{type(exc).__name__}: {exc}"[:160])
    note = ""
    if resp.history:  # 重定向链本身就是事实（Hotellook search → aviasales 这类改嫁信号）
        note = "redirect: " + " -> ".join(str(r.url.host) for r in (*resp.history, resp))
    return ProbeResult(probe, classify_status(resp.status_code), resp.status_code, note, str(resp.url))


def render_markdown(results: list[ProbeResult]) -> str:
    lines = [
        f"# 外源探针结果（{date.today().isoformat()}）",
        "",
        "判定口径：401/403=活着缺 key；404=死了；429=配额；连不上=本机网络层问题，须 DoH 取真实 IP",
        "后 `curl --resolve` 直连复核再下结论。跑完把结论与当日日期抄进 SOURCES.md。",
        "",
        "| 源 | 端点 | HTTP | 结论 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        status = str(r.status) if r.status is not None else "-"
        notes = [f"预期: {r.probe.expects}"] if r.probe.expects else []
        if r.note:
            notes.append(r.note)
        endpoint = f"`{r.final_url or r.probe.url}`"
        lines.append(f"| {r.probe.name} | {endpoint} | {status} | {r.verdict} | {'; '.join(notes)} |")
    return "\n".join(lines)


def exit_code(results: list[ProbeResult]) -> int:
    """0=全部拿到上游结论（死活都是事实，探针成功了）；3=有 unreachable，网络层不可判定。"""
    return 3 if any(r.verdict == UNREACHABLE for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 而非 Markdown 表")
    parser.add_argument("--timeout", type=float, default=15.0, help="单探针超时秒数")
    args = parser.parse_args(argv)

    results = execute(PROBES, timeout=args.timeout)
    if args.json:
        print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))
    else:
        print(render_markdown(results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
