"""核心接口轻量压测（替代 JMeter 的可复现方案，asyncio+httpx）。

用法：
  uv run python tests/perf/load_test.py --duration 15 --workers 20 [--endpoint itinerary:detail]

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

import httpx

# M7-b 切流量：压测目标已是 FastAPI（业务端点与 agent 同进程）；与 tests/api 共用 API_BASE_URL
BASE = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
OUT_DIR = Path(__file__).parent / "report"

LOGIN_PAYLOAD = {"username": "dev", "password": "dev123"}


async def prepare(client: httpx.AsyncClient) -> tuple[dict[str, str], int, int | None, dict[str, str]]:
    """登录（必要时先注册）并备好一个行程与导出任务。

    认证口径：会话走 HttpOnly Cookie，**响应 body 不再回传 token**
    （`data` 里只有 user）。httpx 客户端自带 cookie jar，登录后的请求自动带上会话，
    所以这里不再构造 Authorization 头——旧脚本找 `data.token` 会直接
    `AttributeError: 'NoneType'`，这正是本脚本"切到 FastAPI 后从未真跑过"的原因之一。
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
    # 会话 Cookie 必须显式带出去：run() 每轮新建 client，Cookie 不会自动跟过去
    # （旧 bearer 流程靠 Authorization 头传递，所以没暴露这个耦合）。
    return headers, itin_id, task_id, dict(client.cookies)


async def worker(
    client: httpx.AsyncClient,
    endpoint: str,
    results: list,
    stop: asyncio.Event,
    headers: dict,
    itin_id: int | None,
    task_id: int | None = None,
):
    while not stop.is_set():
        t0 = time.perf_counter()
        # outcome: "ok" | "limited" | "error"。429 单列一类：它是**设计内的限流**，
        # 不是故障。最典型的是 poi:local —— `/api/pois` 会过 enforce_llm_budget
        # （pois.py:39，按用户 6 次/分钟），20 并发下窗口必然打满；把它算成错误，
        # 等于在压限流器却报成"接口挂了"。
        outcome = "error"
        try:
            if endpoint == "itinerary:list":
                r = await client.get("/api/itinerary", headers=headers)
            elif endpoint == "itinerary:detail":
                r = await client.get(f"/api/itinerary/{itin_id}", headers=headers)
            elif endpoint == "poi:local":
                r = await client.get("/api/pois", params={"keywords": "故宫", "city": "北京"}, headers=headers)
            elif endpoint == "export:status":
                r = await client.get(f"/api/export/tasks/{task_id}", headers=headers)
            else:
                raise ValueError(endpoint)
            if r.status_code == 429 or r.json().get("code") == 429:
                outcome = "limited"
            elif r.status_code == 200 and r.json().get("code") == 200:
                outcome = "ok"
        except Exception:
            outcome = "error"
        elapsed = (time.perf_counter() - t0) * 1000
        results.append((elapsed, outcome))


async def run(
    endpoint: str,
    duration: int,
    workers: int,
    itin_id: int | None,
    headers: dict,
    task_id: int | None = None,
    cookies: dict | None = None,
) -> dict:
    results: list = []
    stop = asyncio.Event()
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0, cookies=cookies or {}) as client:
        tasks = [
            asyncio.create_task(worker(client, endpoint, results, stop, headers, itin_id, task_id))
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
    # errorRate 只算真故障（5xx/异常）：429 是限流器的正常输出，单列 rateLimitedRate
    return {
        "endpoint": endpoint,
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

    限流是设计内的容量事实（如 poi:local 的 6 次/分钟用户窗），把它当失败会让
    「压测」变成「证明限流器在工作」。限流率高说明该端点不能按这个并发压——
    这要写进报告让人看见，而不是断言失败。
    """
    for c in summary["cases"]:
        assert c["errorRate"] < 0.01, f"{c['endpoint']} 真故障率异常: {c['errorRate']}"
        assert c["requests"] > 0, f"{c['endpoint']} 无请求完成"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=15)
    ap.add_argument("--workers", type=int, default=20)
    ap.add_argument(
        "--endpoint",
        default="itinerary:detail",
        choices=["all", "itinerary:list", "itinerary:detail", "poi:local", "export:status"],
    )
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
        headers, itin_id, task_id, cookies = await prepare(client)

    print(f"压测 {args.endpoint}  duration={args.duration}s workers={args.workers}")

    if args.endpoint == "all":
        cases = []
        for ep in ["itinerary:list", "poi:local", "itinerary:detail", "export:status"]:
            cases.append(await run(ep, args.duration, args.workers, itin_id, headers, task_id, cookies))
        summary = {"cases": cases}
    elif args.endpoint == "itinerary:detail":
        print("— 预热 + 冷缓存基线 —")
        async with httpx.AsyncClient(base_url=BASE, timeout=30.0, cookies=cookies) as client:
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
        cold = await run(args.endpoint, args.duration, args.workers, itin_id, headers, task_id, cookies)
        await asyncio.sleep(2)
        hot = await run(args.endpoint, args.duration, args.workers, itin_id, headers, task_id, cookies)
        hot["cachePhase"] = "HOT(命中Redis)"
        cold["cachePhase"] = "COLD(缓存清空)"
        summary = {"cases": [cold, hot]}
    else:
        single = await run(args.endpoint, args.duration, args.workers, itin_id, headers, task_id, cookies)
        summary = {"cases": [single]}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    check_report(summary)
    (OUT_DIR / "load_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 核心接口性能压测报告",
        "",
        f"- 压测方式：asyncio + httpx 并发（{args.workers} workers × {args.duration}s）",
        f"- 服务端：FastAPI {BASE}（travel_assistant 库）",
        "- 口径：**真故障率**只含 5xx/异常；429 单列「限流率」——它是设计内的容量事实，"
        "不是故障。`poi:local`（`/api/pois`）会过按用户 LLM 配额（6 次/分钟），"
        "20 并发下必然高限流率，属预期，不代表接口不可用。",
        "",
        "| 阶段 | 接口 | QPS | 平均 | p50 | p95 | p99 | 真故障率 | 限流率 | 请求数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in summary["cases"]:
        phase = c.get("cachePhase", "单轮")
        lines.append(
            f"| {phase} | {c['endpoint']} | {c['qps']} | {c['avgMs']}ms | "
            f"{c['p50Ms']}ms | {c['p95Ms']}ms | {c['p99Ms']}ms | {c['errorRate']:.2%} | "
            f"{c['rateLimitedRate']:.2%} | {c['requests']} |"
        )
    (OUT_DIR / "load_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已生成：{OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
