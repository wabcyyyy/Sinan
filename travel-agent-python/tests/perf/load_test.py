"""核心接口轻量压测（替代 JMeter 的可复现方案，asyncio+httpx）。

用法：
  uv run python tests/perf/load_test.py --duration 15 --workers 20 [--endpoint itinerary:detail]

数据集（SCENARIOS，26 条 ≥20）：全部**只读**场景，三层构成——
  ① 本地纯读主体（DB/缓存/框架，无 LLM、无外网）：行程列表各视图、详情、偏好、
     聊天史、版本、成员/邀请/消费、atlas 各 scope、BYOK 配置列表、导出任务状态等；
  ② 设计内限流场景：poi:local 过按用户 LLM 配额、share:view 过按 IP 匿名窗——
     429 记 limited（容量事实），不算故障；
  ③ SSE 并发场景 sse:progress（`GET /api/itinerary/{id}/events`）：终态行程走库读
     补帧即收尾、非终态靠 15s 心跳证明流活着（event_hub.HEARTBEAT_SECONDS），
     全程不依赖真实 LLM。依赖真实 LLM 的流式端点（/api/agent/v1/**、
     chat-edit/stream）不进数据集。

口径「×2」：每个场景跑 ROUNDS=2 轮，两轮结果全部入报告（看轮间稳定性）；
`--endpoint itinerary:detail` 保留特例——冷/热缓存两阶段对比（同样是 ×2）。
`--endpoint all` = 26 场景 × 2 轮，默认 --duration 15 下全程约 13~15 分钟。

指标：QPS、p50/p95/p99 延迟、错误率；行程详情接口对比 Redis 缓存前后。
输出：tests/perf/report/load_report.json + load_report.md
"""

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import time
from pathlib import Path
from typing import Any

import httpx

# M7-b 切流量：压测目标已是 FastAPI（业务端点与 agent 同进程）；与 tests/api 共用 API_BASE_URL
BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
OUT_DIR = Path(__file__).parent / "report"

LOGIN_PAYLOAD = {"username": "dev", "password": "dev123"}

# 「≥20 用例 ×2」的 ×2：每个场景两轮，两轮都进报告（round1/round2 对照着看轮间稳定性）。
ROUNDS = 2
# SSE 静默判定线：健康事件流空闲 15s 必来一帧心跳（app/common/event_hub.py 的
# HEARTBEAT_SECONDS=15.0），取 20s——超过它仍 0 帧 = 事件面真故障，而不是「暂时没事件」。
SSE_QUIET_DEADLINE_SECONDS = 20.0

# ---- 压测数据集（全部只读；字段：name/path/params/auth/sse/needs/desc）----
# 路径模板占位符：{itinId}=prepare 备好的行程、{taskId}=导出任务、{shareToken}=分享令牌。
# needs 声明场景依赖的 fixture：prepare 备不齐时该场景跳过并打印说明，不让单点失备拖垮全量。
SCENARIOS: list[dict[str, Any]] = [
    # ① 框架与会话
    {"name": "probe:hello", "path": "/api/test/hello", "desc": "框架空转基线（permitAll 探针）"},
    {"name": "user:info", "path": "/api/user/info", "desc": "会话鉴权 + 用户信息读"},
    # ② 行程列表：同一端点按视图/过滤拆变体（_LIST_VIEWS 白名单口径）
    {"name": "itinerary:list", "path": "/api/itinerary", "desc": "列表全量（id 倒序无分页）"},
    {"name": "itinerary:list:active", "path": "/api/itinerary", "params": {"view": "active"}},
    {"name": "itinerary:list:done", "path": "/api/itinerary", "params": {"view": "done"}},
    {"name": "itinerary:list:favorite", "path": "/api/itinerary", "params": {"view": "favorite"}},
    {"name": "itinerary:list:archived", "path": "/api/itinerary", "params": {"view": "archived"}},
    {"name": "itinerary:list:q", "path": "/api/itinerary", "params": {"q": "北京"}, "desc": "标题/城市模糊过滤"},
    # ③ 行程读模型
    {"name": "itinerary:detail", "path": "/api/itinerary/{itinId}", "desc": "详情（Redis 缓存读）"},
    {"name": "itinerary:supported-cities", "path": "/api/itinerary/supported-cities", "desc": "city_geo 字典全量"},
    {"name": "itinerary:preferences", "path": "/api/itinerary/preferences", "desc": "高频偏好 top5"},
    {"name": "itinerary:preference-signals", "path": "/api/itinerary/preferences/signals", "desc": "偏好信号流水"},
    {"name": "itinerary:chat-history", "path": "/api/itinerary/{itinId}/chat-history"},
    {"name": "itinerary:versions", "path": "/api/itinerary/{itinId}/versions"},
    {
        "name": "itinerary:share-meta",
        "path": "/api/itinerary/{itinId}/share",
        "desc": "owner 视角分享信息（未分享也 200）",
    },
    {"name": "itinerary:members", "path": "/api/itinerary/{itinId}/members"},
    {"name": "itinerary:invitations", "path": "/api/itinerary/{itinId}/invitations"},
    {"name": "itinerary:expenses", "path": "/api/itinerary/{itinId}/expenses"},
    # ④ 聚合与其他域
    {"name": "atlas:all", "path": "/api/atlas", "params": {"scope": "all"}},
    {"name": "atlas:planned", "path": "/api/atlas", "params": {"scope": "planned"}},
    {"name": "atlas:visited", "path": "/api/atlas", "params": {"scope": "visited"}},
    {"name": "llm-gateway:list", "path": "/api/llm-gateway", "desc": "BYOK 配置列表（库读）"},
    {"name": "export:status", "path": "/api/export/tasks/{taskId}", "needs": "taskId"},
    # ⑤ 设计内限流（429 记 limited，不算故障；不压阈值本身，阈值是业务数值禁区）
    {
        "name": "poi:local",
        "path": "/api/pois",
        "params": {"keywords": "故宫", "city": "北京"},
        "desc": "过按用户 LLM 配额（6 次/分钟），20 并发下窗口必然打满",
    },
    {
        "name": "share:view",
        "path": "/api/share/{shareToken}",
        "auth": "anonymous",
        "needs": "shareToken",
        "desc": "匿名分享读，过按 IP 分钟窗限流",
    },
    # ⑥ SSE 并发（终态补帧/心跳兜底，不依赖真实 LLM）
    {
        "name": "sse:progress",
        "path": "/api/itinerary/{itinId}/events",
        "sse": True,
        "desc": "生成进度事件流：同一行程 5 连接上限，超限发 TOO_MANY_CONNECTIONS 后收尾",
    },
]


async def prepare(client: httpx.AsyncClient) -> tuple[dict[str, str], int, int | None, dict[str, str], str | None]:
    """登录（必要时先注册）并备好压测 fixture：行程、导出任务、分享令牌。

    认证口径：会话走 HttpOnly Cookie，**响应 body 不再回传 token**
    （`data` 里只有 user）。httpx 客户端自带 cookie jar，登录后的请求自动带上会话，
    所以这里不再构造 Authorization 头——旧脚本找 `data.token` 会直接
    `AttributeError: 'NoneType'`，这正是本脚本"切到 FastAPI 后从未真跑过"的原因之一。

    分享令牌是尽力而为：创建失败（如行程异常态）不抛，返回 None 让 share:view
    场景被跳过——fixture 备不齐只裁剪数据集，不拖垮整场压测。
    """
    r = await client.post("/api/auth/login", json=LOGIN_PAYLOAD)
    if r.json().get("code") != 200:
        reg = await client.post(
            "/api/auth/register", json={"username": "dev", "password": "dev123", "nickname": "压测"}
        )
        assert reg.json().get("code") == 200, reg.text
        r = await client.post("/api/auth/login", json=LOGIN_PAYLOAD)
        assert r.json().get("code") == 200, r.text
    headers: dict = {}
    lst = await client.get("/api/itinerary", headers=headers)
    items = lst.json().get("data") or []
    if not items:
        gen = await client.post(
            "/api/itinerary/generate",
            json={"city": "北京", "days": 2, "persons": 2, "budget": 3000, "preferences": ["人文"]},
            headers=headers,
        )
        assert gen.json().get("code") == 200, gen.text
        lst = await client.get("/api/itinerary", headers=headers)
        items = lst.json().get("data") or []
    itin_id = items[0]["id"]
    exp = await client.post(f"/api/export/pdf/{itin_id}", headers=headers)
    task_id = exp.json().get("data", {}).get("id")
    if not task_id:
        exp = await client.post(f"/api/export/pdf/{itin_id}", headers=headers)
        task_id = exp.json().get("data", {}).get("id")
    # 分享令牌：create_share 对同一行程是「轮换」语义（旧链接立即失效），prepare 一次
    # 只换一次，代价可控；列表默认视图排除归档行，items[0] 不会踩「归档不能分享」。
    share = await client.post(f"/api/itinerary/{itin_id}/share", json={"expireDays": 30}, headers=headers)
    share_token: str | None = None
    if share.json().get("code") == 200:
        share_token = (share.json().get("data") or {}).get("shareToken")
    # 会话 Cookie 必须显式带出去：run() 每轮新建 client，Cookie 不会自动跟过去
    # （旧 bearer 流程靠 Authorization 头传递，所以没暴露这个耦合）。
    return headers, itin_id, task_id, dict(client.cookies), share_token


def render_path(scenario: dict[str, Any], fixtures: dict[str, Any]) -> str:
    """把路径模板里的占位符替换成 fixture 值（replace 而非 format：模板里有裸 {}/冒号）。"""
    path = str(scenario["path"])
    for key in ("itinId", "taskId", "shareToken"):
        if f"{{{key}}}" in path:
            path = path.replace(f"{{{key}}}", str(fixtures.get(key)))
    return path


async def sse_probe(client: httpx.AsyncClient, path: str, headers: dict) -> str:
    """SSE 并发探针：开流读帧，返回 ok | limited | error。

    - ok：读到 ≥1 帧（终态补帧、生成事件或 15s 心跳都算——心跳本身就是流活着的证明）；
    - limited：收到 TOO_MANY_CONNECTIONS 信封——同一行程 5 连接上限是设计内容量事实，
      与 429 同哲学，不算故障；
    - error：连不上、非 200、或开流后 SSE_QUIET_DEADLINE_SECONDS 内 0 帧
      （健康流 15s 必有心跳，静默超线 = 事件面真故障）。
    """
    async with client.stream("GET", path, headers=headers) as resp:
        if resp.status_code == 429:
            return "limited"
        if resp.status_code != 200:
            return "error"
        lines = resp.aiter_lines()
        deadline = time.monotonic() + SSE_QUIET_DEADLINE_SECONDS
        frames = 0
        while True:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                line = await asyncio.wait_for(lines.__anext__(), timeout)
            except StopAsyncIteration:
                break  # 服务端收尾（终态补帧后立即关流）
            except TimeoutError:
                break  # 静默超预算：连接还开着但一帧都没有，按故障计
            if line.startswith("data:"):
                frames += 1
                if '"TOO_MANY_CONNECTIONS"' in line:
                    return "limited"
        return "ok" if frames else "error"


async def worker(
    client: httpx.AsyncClient,
    scenario: dict[str, Any],
    path: str,
    results: list,
    stop: asyncio.Event,
    headers: dict,
):
    params = scenario.get("params") or {}
    is_sse = bool(scenario.get("sse"))
    while not stop.is_set():
        t0 = time.perf_counter()
        # outcome: "ok" | "limited" | "error"。429 单列一类：它是**设计内的限流**，
        # 不是故障。最典型的是 poi:local —— `/api/pois` 会过 enforce_llm_budget
        # （pois.py:39，按用户 6 次/分钟），20 并发下窗口必然打满；把它算成错误，
        # 等于在压限流器却报成"接口挂了"。
        outcome = "error"
        try:
            if is_sse:
                outcome = await sse_probe(client, path, headers)
            else:
                r = await client.get(path, params=params, headers=headers)
                if r.status_code == 429 or r.json().get("code") == 429:
                    outcome = "limited"
                elif r.status_code == 200 and r.json().get("code") == 200:
                    outcome = "ok"
        except Exception:
            outcome = "error"
        elapsed = (time.perf_counter() - t0) * 1000
        results.append((elapsed, outcome))


async def run(scenario: dict[str, Any], duration: int, workers: int, fixtures: dict[str, Any], phase: str) -> dict:
    headers = fixtures["headers"]
    # share:view 是匿名面：不带会话 Cookie，单独开一个无 cookie 的 client 才是真实路径。
    anonymous = scenario.get("auth") == "anonymous"
    cookies = None if anonymous else (fixtures.get("cookies") or {})
    path = render_path(scenario, fixtures)
    results: list = []
    stop = asyncio.Event()
    # trust_env=False：压的是本机 127.0.0.1，绝不能绕道系统代理。httpx 的 trust_env
    # 只认环境变量 NO_PROXY，不认 Windows 注册表的 ProxyOverride 绕过名单——开着
    # 系统代理（如 127.0.0.1:7900）时全部流量会被送进代理：每请求多一跳、代理逐响应
    # 关连接，突发下还会产生客户端侧假故障（实测 share:view 1.71% 全是代理层连接失败，
    # 服务端访问日志同期只有 200/429）。
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0, cookies=cookies, trust_env=False) as client:
        tasks = [
            asyncio.create_task(worker(client, scenario, path, results, stop, {} if anonymous else headers))
            for _ in range(workers)
        ]
        await asyncio.sleep(duration)
        stop.set()
        await asyncio.gather(*tasks)
    latencies = [r[0] for r in results]
    ok = sum(1 for r in results if r[1] == "ok")
    limited = sum(1 for r in results if r[1] == "limited")
    latencies.sort()

    def pct(p):
        if not latencies:
            return 0.0
        return round(latencies[min(int(len(latencies) * p), len(latencies) - 1)], 2)

    qps = round(len(results) / duration, 1)
    # errorRate 只算真故障（5xx/异常/连不上/流静默）；429 与 TOO_MANY_CONNECTIONS
    # 是限流器的正常输出，单列 rateLimitedRate
    return {
        "endpoint": scenario["name"],
        "phase": phase,
        "durationSec": duration,
        "workers": workers,
        "requests": len(results),
        "success": ok,
        "rateLimited": limited,
        "rateLimitedRate": round(limited / len(results), 4) if results else 0.0,
        "errorRate": round(1 - (ok + limited) / len(results), 4) if results else 1.0,
        "qps": qps,
        "avgMs": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p50Ms": pct(0.50),
        "p95Ms": pct(0.95),
        "p99Ms": pct(0.99),
    }


def check_report(summary: dict) -> None:
    """只对**真故障**判红；限流率单列，不做失败断言。

    限流是设计内的容量事实（如 poi:local 的 6 次/分钟用户窗、share:view 的按 IP 窗、
    sse:progress 的 5 连接上限），把它当失败会让「压测」变成「证明限流器在工作」。
    限流率高说明该端点不能按这个并发压——这要写进报告让人看见，而不是断言失败。
    """
    for c in summary["cases"]:
        assert c["errorRate"] < 0.01, f"{c['endpoint']}({c.get('phase')}) 真故障率异常: {c['errorRate']}"
        assert c["requests"] > 0, f"{c['endpoint']}({c.get('phase')}) 无请求完成"


async def run_scenario_twice(
    scenario: dict[str, Any], duration: int, workers: int, fixtures: dict[str, Any]
) -> list[dict]:
    """一个场景 × ROUNDS 轮；轮次标签进报告，轮间对照看稳定性。"""
    cases = []
    for rnd in range(1, ROUNDS + 1):
        case = await run(scenario, duration, workers, fixtures, f"第{rnd}轮")
        cases.append(case)
        print(
            f"  {scenario['name']} 第{rnd}轮 qps={case['qps']} "
            f"p95={case['p95Ms']}ms error={case['errorRate']:.2%} limited={case['rateLimitedRate']:.2%}"
        )
    return cases


async def run_detail_cold_hot(
    scenario: dict[str, Any], duration: int, workers: int, fixtures: dict[str, Any]
) -> list[dict]:
    """detail 特例：预热 + 清 Redis 缓存，冷/热两阶段各一轮（原有口径保留）。"""
    itin_id = fixtures["itinId"]
    headers = fixtures["headers"]
    print("— 预热 + 冷缓存基线 —")
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0, cookies=fixtures["cookies"], trust_env=False) as client:
        r = await client.get(f"/api/itinerary/{itin_id}", headers=headers)
        assert r.json().get("code") == 200
        # 清掉 Redis 缓存键制造冷启动
        import socket

        k = f"itinerary:detail::{itin_id}".encode()
        s = socket.create_connection(("127.0.0.1", 6380), timeout=5)
        try:
            s.sendall(b"*2\r\n$3\r\nDEL\r\n$" + str(len(k)).encode() + b"\r\n" + k + b"\r\n")
            s.settimeout(0.5)
            with contextlib.suppress(TimeoutError):
                s.recv(1024)
        finally:
            s.close()
    cold = await run(scenario, duration, workers, fixtures, "COLD(缓存清空)")
    await asyncio.sleep(2)
    hot = await run(scenario, duration, workers, fixtures, "HOT(命中Redis)")
    return [cold, hot]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=15)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument(
        "--endpoint",
        default="itinerary:detail",
        choices=["all"] + [s["name"] for s in SCENARIOS],
        help="all=全量数据集（26 场景 ×2 轮）；或单选一个场景名（detail 走冷/热特例）",
    )
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=BASE, timeout=30.0, trust_env=False) as client:
        headers, itin_id, task_id, cookies, share_token = await prepare(client)
    fixtures: dict[str, Any] = {
        "headers": headers,
        "itinId": itin_id,
        "taskId": task_id,
        "cookies": cookies,
        "shareToken": share_token,
    }

    dataset = [s for s in SCENARIOS if args.endpoint == "all" or s["name"] == args.endpoint]
    # fixture 失备的场景裁剪掉（打印说明），不让单点拖垮全量
    runnable: list[dict[str, Any]] = []
    for s in dataset:
        need = s.get("needs")
        if need is not None and not fixtures.get(need):
            print(f"  跳过 {s['name']}：fixture {need} 不可用（prepare 未备齐）")
            continue
        runnable.append(s)

    print(
        f"压测 {args.endpoint}  数据集={len(runnable)} 场景 × {ROUNDS} 轮  "
        f"duration={args.duration}s workers={args.workers}"
    )
    print(f"场景清单：{'、'.join(s['name'] for s in runnable)}")

    cases: list[dict] = []
    if args.endpoint == "itinerary:detail":
        cases = await run_detail_cold_hot(runnable[0], args.duration, args.workers, fixtures)
        for c in cases:
            print(f"  itinerary:detail {c['phase']} qps={c['qps']} p95={c['p95Ms']}ms")
    else:
        for s in runnable:
            if s.get("sse"):
                print(f"  {s['name']}：SSE 并发流，单请求最长约 {SSE_QUIET_DEADLINE_SECONDS:.0f}s（心跳判定线）")
            cases.extend(await run_scenario_twice(s, args.duration, args.workers, fixtures))

    summary = {"dataset": {"scenarios": len(runnable), "rounds": ROUNDS}, "cases": cases}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    check_report(summary)
    (OUT_DIR / "load_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 核心接口性能压测报告",
        "",
        f"- 压测方式：asyncio + httpx 并发（{args.workers} workers × {args.duration}s）",
        f"- 数据集：{len(runnable)} 个只读场景 × {ROUNDS} 轮（round1/round2 看轮间稳定性）",
        f"- 服务端：FastAPI {BASE}（travel_assistant 库）",
        "- 口径：**真故障率**只含 5xx/异常/连不上/SSE 流静默；429 与 SSE TOO_MANY_CONNECTIONS"
        " 单列「限流率」——它们是设计内的容量事实，不是故障。`poi:local`（`/api/pois`）过"
        "按用户 LLM 配额（6 次/分钟）、`share:view` 过按 IP 分钟窗、`sse:progress` 同一行程"
        " 5 连接上限，20 并发下三者必然高限流率，属预期，不代表接口不可用。",
        "",
        "| 阶段 | 接口 | QPS | 平均 | p50 | p95 | p99 | 真故障率 | 限流率 | 请求数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in summary["cases"]:
        lines.append(
            f"| {c.get('phase', '单轮')} | {c['endpoint']} | {c['qps']} | {c['avgMs']}ms | "
            f"{c['p50Ms']}ms | {c['p95Ms']}ms | {c['p99Ms']}ms | {c['errorRate']:.2%} | "
            f"{c['rateLimitedRate']:.2%} | {c['requests']} |"
        )
    (OUT_DIR / "load_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已生成：{OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
