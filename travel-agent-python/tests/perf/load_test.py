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


async def prepare(client: httpx.AsyncClient) -> dict:
    r = await client.post("/api/auth/login", json=LOGIN_PAYLOAD)
    body = r.json()
    token = body.get("data", {}).get("token")
    if not token:
        reg = await client.post(
            "/api/auth/register", json={"username": "dev", "password": "dev123", "nickname": "压测"}
        )
        assert reg.json().get("code") == 200, reg.text
        r = await client.post("/api/auth/login", json=LOGIN_PAYLOAD)
        token = r.json()["data"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
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
    return headers, itin_id, task_id


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
            ok = r.status_code == 200 and r.json().get("code") == 200
        except Exception:
            ok = False
        elapsed = (time.perf_counter() - t0) * 1000
        results.append((elapsed, ok))


async def run(
    endpoint: str, duration: int, workers: int, itin_id: int | None, headers: dict, task_id: int | None = None
) -> dict:
    results: list = []
    stop = asyncio.Event()
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
        tasks = [
            asyncio.create_task(worker(client, endpoint, results, stop, headers, itin_id, task_id))
            for _ in range(workers)
        ]
        await asyncio.sleep(duration)
        stop.set()
        await asyncio.gather(*tasks)
    latencies = [r[0] for r in results]
    ok = sum(1 for r in results if r[1])
    latencies.sort()

    def pct(p):
        if not latencies:
            return 0.0
        return round(latencies[min(int(len(latencies) * p), len(latencies) - 1)], 2)

    qps = round(len(results) / duration, 1)
    return {
        "endpoint": endpoint,
        "durationSec": duration,
        "workers": workers,
        "requests": len(results),
        "success": ok,
        "errorRate": round(1 - ok / len(results), 4) if results else 1.0,
        "qps": qps,
        "avgMs": round(statistics.mean(latencies), 2) if latencies else 0.0,
        "p50Ms": pct(0.50),
        "p95Ms": pct(0.95),
        "p99Ms": pct(0.99),
    }


def check_report(summary: dict) -> None:
    for c in summary["cases"]:
        assert c["errorRate"] < 0.01, f"{c['endpoint']} 错误率异常: {c['errorRate']}"
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
        headers, itin_id, task_id = await prepare(client)

    print(f"压测 {args.endpoint}  duration={args.duration}s workers={args.workers}")

    if args.endpoint == "all":
        cases = []
        for ep in ["itinerary:list", "poi:local", "itinerary:detail", "export:status"]:
            cases.append(await run(ep, args.duration, args.workers, itin_id, headers, task_id))
        summary = {"cases": cases}
    elif args.endpoint == "itinerary:detail":
        print("— 预热 + 冷缓存基线 —")
        async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
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
        cold = await run(args.endpoint, args.duration, args.workers, itin_id, headers, task_id)
        await asyncio.sleep(2)
        hot = await run(args.endpoint, args.duration, args.workers, itin_id, headers, task_id)
        hot["cachePhase"] = "HOT(命中Redis)"
        cold["cachePhase"] = "COLD(缓存清空)"
        summary = {"cases": [cold, hot]}
    else:
        single = await run(args.endpoint, args.duration, args.workers, itin_id, headers, task_id)
        summary = {"cases": [single]}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    check_report(summary)
    (OUT_DIR / "load_report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# 核心接口性能压测报告",
        "",
        f"- 压测方式：asyncio + httpx 并发（{args.workers} workers × {args.duration}s）",
        f"- 服务端：FastAPI {BASE}（travel_assistant 库）",
        "",
        "| 阶段 | 接口 | QPS | 平均 | p50 | p95 | p99 | 错误率 | 请求数 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in summary["cases"]:
        phase = c.get("cachePhase", "单轮")
        lines.append(
            f"| {phase} | {c['endpoint']} | {c['qps']} | {c['avgMs']}ms | "
            f"{c['p50Ms']}ms | {c['p95Ms']}ms | {c['p99Ms']}ms | {c['errorRate']:.2%} | {c['requests']} |"
        )
    (OUT_DIR / "load_report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已生成：{OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
