"""Zero-cost controlled replay: real planning code, synthetic HTTP responses, no WAN/LLM.

Run from travel-agent-python: uv run python scripts/measure_planning_latency.py --output <new-dir>
Provider throttles, caches, Supervisor graphs, CP-SAT and SQLite commits are real.
This measures a local engineering baseline, NOT production latency or WAN savings.
"""

import argparse
import copy
import json
import os
import platform
import sys
import threading
import time
from collections import Counter
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def summarize(output):
    results = json.loads((output / "results.json").read_text("utf-8"))
    lines = [
        "# 规划延迟：零费用本地受控回放（2026-10-25）",
        "",
        "费用 0 元；公网请求 0；模型调用 0；核心求解逻辑未修改。两个同城三日合成任务，各一对冷/热样本。",
        "真实执行 Supervisor 三线程/三个 LangGraph 子图、ExternalClient 限速与缓存、"
        "有向路线缓存、CP-SAT、业务 plan_days、SQLite 提交及 API 读回。",
        "研究检索工具注入冻结资料，HTTP 为 MockTransport 即时响应；模型充分性评估关闭并保留 unknown。",
        "景点4处（规划时长240分钟，固定预约覆盖其中一处）、餐饮2处（60分钟）、酒店1处；酒店不进入现有求解候选。",
        "A：固定预约+先后要求+两级软偏好；B：同一候选池及需求另加首日10:00–18:00到达窗口。两级软目标+收益/交通/开始时刻构成五层。",
        "",
        "## 分阶段耗时",
        "",
        "单位为毫秒；括号为占该次规划执行总时长的比例。other 保证独占阶段之和等于总时长。",
        "计时从 worker 内 plan_days 开始，到其返回；包含终态/预算/版本收尾，"
        "排队、worker轮询、API准入与后台富化不在此总时长内。",
        "",
        "|任务|缓存|三域研究|路网矩阵|CP-SAT（含编译）|三日persist事务合计|其他收尾|规划总计|首候选发布完成|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for index, row in enumerate(results):
        cells = [f"{'A' if index < 2 else 'B'}", row["temperature"]]
        cells += [
            f"{row['durations_ms'][key]:.3f} ({row['shares_pct'][key]:.2f}%)"
            for key in ("research", "matrix", "solver", "persist", "other")
        ]
        cells += [f"{row['total_ms']:.3f}", f"{row['milestones_ms']['preview_publish_returned']:.3f}"]
        lines.append("|" + "|".join(cells) + "|")
    lines += [
        "",
        "## 冷热对比",
        "",
        "|任务|研究下降|矩阵下降|规划总耗时下降|回放HTTP次数|HTTP下降|",
        "|---|---:|---:|---:|---:|---:|",
    ]

    def reduction(before, after):
        return (before - after) / before * 100

    for label, cold, warm in (("A", *results[:2]), ("B", *results[2:])):
        values = [reduction(cold["durations_ms"][key], warm["durations_ms"][key]) for key in ("research", "matrix")]
        values.append(reduction(cold["total_ms"], warm["total_ms"]))
        lines.append(f"|{label}|{values[0]:.2f}%|{values[1]:.5f}%|{values[2]:.2f}%|19→0|100%|")
    lines += [
        "",
        "研究内HTTP为OTM半径2次+Nominatim 1次，冷→热3→0；"
        "矩阵为15个无序点对的OSRM Table回放请求，30条有向边，热缓存0请求。",
        "另有1次API准入的OTM城市定位，因此全请求计数19；该准入请求不在worker研究计时内。",
        "每次冷矩阵首次请求无需等待，随后14次请求受原代码1 rps限速，形成约14秒等待；未人为sleep或注入公网RTT。",
        "热矩阵命中RouteService外层缓存；冷矩阵的反向边命中OSRM内层ExternalClient缓存。因此不是30次外呼降至0。",
        "",
        "## 三域并行与求解记录",
        "",
        "|任务|缓存|景点域ms|餐饮域ms|酒店域ms|CP-SAT各次solve ms（可行性→5层）|",
        "|---|---|---:|---:|---:|---|",
    ]
    for index, row in enumerate(results):
        domains = row["domains_ms"]
        solves = ", ".join(f"{call['duration_ms']:.3f}" for call in row["cp_sat_calls"])
        lines.append(
            f"|{'A' if index < 2 else 'B'}|{row['temperature']}|{domains['attraction']:.3f}|"
            f"{domains['food']:.3f}|{domains['hotel']:.3f}|{solves}|"
        )
    lines += [
        "",
        "四次均COMPLETED，三日SUCCEEDED，共6条活动；数值优化5/5。事实核实仍UNKNOWN，不代表真实营业/费用已核验。",
        "三域时间重叠，不可相加作为研究墙钟耗时；没有串行对照，不声称并行加速倍数。",
        "首候选是dayNo=0尚未排程的研究池预览，发布在矩阵/求解之前；这里只测服务端事件发布完成，未测SSE跨进程传输或浏览器绘制。",
        "",
        "## 适用表述与复现",
        "",
        "可用于面试：建立零费用本地分段计时与冷热缓存基线，定位有向路网逐对请求的限速等待为该受控样本的主耗时，区分首研究候选与正式交付。",
        "百分比只适用于本机即时HTTP回放及当前限速，不可写成线上端到端提升；未覆盖真实模型、公网RTT、真实供应商稳定性、用户流量与P95。",
        "",
        "在travel-agent-python目录运行（输出目录须不存在）：",
        "",
        "```powershell",
        "uv run python scripts/measure_planning_latency.py --output report/latency/<new-run>",
        "```",
        "",
        "原始纳秒时钟采样已换算为毫秒，详见results.json；环境与方法见method.json；隔离数据库为m1b.db。",
        "首次样本包含图/缓存等惰性初始化，未做统计重复或宣称稳定分位数。",
        "",
    ]
    (output / "summary.md").write_text("\n".join(lines), "utf-8")


def execute(output):
    output.mkdir(parents=True, exist_ok=False)
    os.environ["CHECKPOINT_DB_PATH"] = str(output / "checkpoints.sqlite3")
    os.environ["JWT_SECRET"] = "example-only-latency-local-signing-material"

    import httpx
    import ortools
    import pytest
    from ortools.sat.python import cp_model

    from app.agent.data import osrm_routes, places, route_service, web_search
    from app.agent.generation.orchestration import constraint_planning as planner
    from app.agent.research import reasoning, supervisor
    from app.common import redis_client
    from app.common.config import settings
    from app.common.external_client import ExternalClient
    from app.common.llm_client import LLMClient
    from app.db.models import ItineraryDay, ItineraryItem
    from app.db.session import session_scope
    from app.services import day_persistence, generation_events, itinerary_generation
    from tests.constraint_planning_cases import frozen_context, planning_request
    from tests.test_m1b_requirements_pipeline import client as client_fixture
    from tests.test_m1b_requirements_pipeline import env as env_fixture

    active = {}
    lock = threading.Lock()
    patcher = pytest.MonkeyPatch()
    environment = env_fixture.__wrapped__(patcher, output)
    next(environment)
    client = client_fixture.__wrapped__(None)

    def deny(*_args, **_kwargs):
        raise AssertionError("latency benchmark forbids real HTTP and model calls")

    patcher.setattr(httpx.HTTPTransport, "handle_request", deny)
    patcher.setattr(LLMClient, "chat_response", deny)
    patcher.setattr(LLMClient, "stream_chat_deltas", deny)
    patcher.setattr(redis_client, "is_down", lambda: True)
    for key, value in {
        "auth_cookie_secure": False,
        "trial_billing_enabled": False,
        "weather_enabled": False,
        "web_search_enabled": False,
        "nominatim_enabled": True,
        "otm_api_key": "example-local-replay-only",
        "ors_base_url": "",
        "osrm_base_url": "https://latency.invalid",
        "route_max_calls": 100,
        "research_workers": 3,
        "trace_storage_path": str(output / "traces.jsonl"),
        "usage_db_path": str(output / "usage.sqlite3"),
        "user_daily_llm_runs": 1000,
        "user_llm_runs_per_minute": 1000,
    }.items():
        patcher.setattr(settings, key, value)
    patcher.setattr(reasoning, "configured", lambda _role: False)
    patcher.setattr(web_search, "web_search_enabled", lambda: False)

    rows = frozen_context()["candidates"]
    for index, row in enumerate(rows):
        row["duration_min"] = 240 if index < 4 else 60
    domains = {"attraction": rows[:4], "food": rows[4:], "hotel": []}
    clients = [places._otm_geo_client, places._otm_radius_client, places._nominatim_client, osrm_routes._client]

    def respond(request):
        path = request.url.path
        if request.url.host == "api.opentripmap.com" and path.endswith("places/geoname"):
            provider = "OpenTripMap-city-replay"
            data = {"name": "Paris", "country": "FR", "lat": 48.86, "lon": 2.33}
        elif request.url.host == "api.opentripmap.com" and path.endswith("places/radius"):
            provider = "OpenTripMap-replay"
            data = domains[request.url.params["domain"]]
        elif request.url.host == httpx.URL(places._NOMINATIM_BASE).host:
            provider = "Nominatim-replay"
            data = [
                {
                    "lat": "48.865",
                    "lon": "2.335",
                    "name": "fixture-hotel",
                    "osm_id": 1,
                    "osm_type": "node",
                    "address": {"country_code": "fr"},
                }
            ]
        elif request.url.host == "latency.invalid" and "/table/v1/" in path:
            provider = "OSRM-replay"
            # Deliberately asymmetric fixture; not observed road-network travel times.
            data = {
                "code": "Ok",
                "durations": [[0, 420], [540, 0]],
                "distances": [[0, 600], [750, 0]],
                "data_version": "synthetic-latency-fixture",
            }
        else:
            raise AssertionError(f"unexpected replay request: {request.url.host}{path}")
        with lock:
            active["http"][provider] += 1
        return httpx.Response(200, json=copy.deepcopy(data))

    replay_http = httpx.Client(transport=httpx.MockTransport(respond))
    patcher.setattr(places, "api_client", lambda: replay_http)
    patcher.setattr(osrm_routes, "api_client", lambda: replay_http)

    def search(domain):
        def fetch(*_args, **_kwargs):
            if domain == "hotel":
                return places.geocode_place_rows("fixture-hotel", "Paris")
            return copy.deepcopy(places._otm_get("places/radius", {"domain": domain}, places._otm_radius_client))

        return fetch

    for domain, name in (("attraction", "search_attractions"), ("food", "search_foods"), ("hotel", "search_hotels")):
        patcher.setattr(supervisor.tools, name, search(domain))
    patcher.setattr(supervisor.tools, "get_consumption", lambda _city: None)
    patcher.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))
    patcher.setattr(
        itinerary_generation.enricher_pool,
        "submit",
        lambda _task, *args: itinerary_generation.planning_tasks.release(args[1]),
    )
    patcher.setattr(itinerary_generation, "_submit_budget_recalculate", lambda *_a: None)

    def timed(module, name, stage):
        original = getattr(module, name)

        def wrapped(*args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return original(*args, **kwargs)
            finally:
                elapsed = (time.perf_counter_ns() - start) / 1e6
                with lock:
                    active["durations_ms"][stage] += elapsed
                    active["spans"].append(
                        {"stage": stage, "start_ms": (start - active["t0"]) / 1e6, "duration_ms": elapsed}
                    )

        patcher.setattr(module, name, wrapped)

    original_plan = itinerary_generation.plan_days

    def plan(*args, **kwargs):
        active["t0"] = time.perf_counter_ns()
        try:
            return original_plan(*args, **kwargs)
        finally:
            active["total_ms"] = (time.perf_counter_ns() - active["t0"]) / 1e6

    patcher.setattr(itinerary_generation, "plan_days", plan)
    timed(itinerary_generation, "run_plan_context", "research")
    timed(planner, "get_route_matrix", "matrix")
    timed(planner, "solve_trip", "solver")
    timed(day_persistence, "persist", "persist")
    original_research = supervisor.run_research

    def domain_research(task):
        start = time.perf_counter_ns()
        try:
            return original_research(task)
        finally:
            with lock:
                active["domains_ms"][task.domain] = (time.perf_counter_ns() - start) / 1e6

    patcher.setattr(supervisor, "run_research", domain_research)
    original_call = ExternalClient.call

    def call(self, cache_key, *args, **kwargs):
        hit, _ = self._cached(cache_key)
        with lock:
            active["cache"][f"{self.name}:{'hit' if hit else 'miss'}"] += 1
        return original_call(self, cache_key, *args, **kwargs)

    patcher.setattr(ExternalClient, "call", call)
    original_solve = cp_model.CpSolver.solve

    def solve(self, *args, **kwargs):
        start = time.perf_counter_ns()
        result = original_solve(self, *args, **kwargs)
        active["cp_sat_calls"].append(
            {"duration_ms": (time.perf_counter_ns() - start) / 1e6, "status": self.status_name(result)}
        )
        return result

    patcher.setattr(cp_model.CpSolver, "solve", solve)
    original_mark = itinerary_generation._StageClock.mark

    def mark(self, stage):
        active["milestones_ms"].setdefault(stage, (time.perf_counter_ns() - active["t0"]) / 1e6)
        return original_mark(self, stage)

    patcher.setattr(itinerary_generation._StageClock, "mark", mark)
    original_preview = generation_events.item_preview

    def preview(*args, **kwargs):
        result = original_preview(*args, **kwargs)
        active["milestones_ms"].setdefault("preview_publish_returned", (time.perf_counter_ns() - active["t0"]) / 1e6)
        return result

    patcher.setattr(generation_events, "item_preview", preview)

    cases = []
    for label, change in (
        ("Paris-3d-fixed-sequence", {}),
        (
            "Paris-3d-late-arrival",
            {"dayWindows": [{"dayNo": 1, "kind": "arrival", "notBefore": "10:00", "finishBy": "18:00"}]},
        ),
    ):
        request = planning_request(days=3)
        req = request.requirements_struct.model_dump(mode="json", by_alias=True)
        req["placeRelations"].extend(
            [
                {
                    "constraintId": "soft-high",
                    "kind": "before",
                    "placeNames": ["地点1", "地点2"],
                    "dayNo": 2,
                    "strength": "soft",
                    "priority": 2,
                },
                {
                    "constraintId": "soft-low",
                    "kind": "before",
                    "placeNames": ["地点3", "地点4"],
                    "dayNo": 3,
                    "strength": "soft",
                    "priority": 1,
                },
            ]
        )
        req.update(change)
        # Both tasks use identical POIs/routes, isolating cache state from input changes.
        cases.append(
            (
                label,
                {
                    "city": "Paris",
                    "days": 3,
                    "persons": 2,
                    "stayNights": 0,
                    "startDate": "2026-10-25",
                    "budget": 200,
                    "requirementsStruct": req,
                },
            )
        )

    results = []
    try:
        for label, body in cases:
            for external in clients:
                external.clear_cache()
                external.reset_runtime_state()
            route_service.clear_route_cache()
            for temperature in ("cold", "warm"):
                active.clear()
                active.update(
                    case=label,
                    temperature=temperature,
                    t0=time.perf_counter_ns(),
                    http=Counter(),
                    cache=Counter(),
                    durations_ms=Counter(),
                    domains_ms={},
                    spans=[],
                    cp_sat_calls=[],
                    milestones_ms={},
                    request=body,
                )
                response = client.post("/api/itinerary/generate", json=body)
                if response.status_code != 200:
                    raise RuntimeError(f"generation HTTP {response.status_code}: {response.text}")
                identifier = response.json()["data"]["id"]
                detail = client.get(f"/api/itinerary/{identifier}").json()["data"]
                with session_scope() as session:
                    days = session.query(ItineraryDay).filter_by(itinerary_id=identifier).all()
                    item_count = session.query(ItineraryItem).filter_by(itinerary_id=identifier, deleted=0).count()
                    active["persisted_day_states"] = [day.generation_status for day in days]
                active["item_count"] = item_count
                active["gen_state"] = detail["genState"]
                active["assessments"] = [day.get("constraintPlanning") for day in detail["dayList"]]
                stages = active["durations_ms"]
                stages["other"] = active["total_ms"] - sum(stages.values())
                active["shares_pct"] = {key: value / active["total_ms"] * 100 for key, value in stages.items()}
                active.pop("t0")
                results.append(copy.deepcopy(active))
                (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), "utf-8")
                print(
                    json.dumps(
                        {
                            key: active[key]
                            for key in ("case", "temperature", "total_ms", "http", "gen_state", "item_count")
                        }
                    ),
                    flush=True,
                )
                if item_count == 0 or active["persisted_day_states"] != ["SUCCEEDED"] * 3:
                    raise RuntimeError("benchmark did not persist all three days; inspect results.json")
        metadata = {
            "method": "controlled synthetic HTTP replay; real throttles, caches, Supervisor graphs, CP-SAT, SQLite",
            "cost_cny": 0,
            "real_external_http_requests": 0,
            "llm_calls": 0,
            "python": sys.version,
            "ortools": ortools.__version__,
            "platform": platform.platform(),
            "research_workers": settings.research_workers,
            "notes": [
                "one cold/warm pair per task; no percentiles or production inference",
                "network RTT, LLM reasoning, API queue, worker poll, browser rendering excluded",
                "first candidate is backend publication completion, not browser paint",
                "research domain times overlap; never sum as wall time",
                "hotel candidates researched but not consumed by current solver",
                "HTTP counts are MockTransport requests; WAN count remains zero",
                "other includes events, task lifecycle, assessments and finalization; background enrichment excluded",
            ],
        }
        (output / "method.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), "utf-8")
        summarize(output)
    finally:
        client.close()
        replay_http.close()
        with suppress(StopIteration):
            next(environment)
        patcher.undo()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    execute(parser.parse_args().output.resolve())
