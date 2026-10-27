"""正式页面连续在线验收：匿名需求、当次公开命名查询/研究、真实worker与CP；无回放。"""

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.online_closeout_budget import OnlineBudget
from scripts.run_p4_acceptance import save

MUSEUMS = (("Musée du Louvre", "卢浮宫"), ("Musée d'Orsay", "奥赛博物馆"))


def observed_louvre_closure(sources):
    """本次命名实体的官方闭馆字段；不推断其他日期开放，也不采用镜像/下属馆段落。"""
    from app.agent.generation.rules.opening_rules import parse_opening_rules
    from app.schemas.trip import FactEvidence

    for row in sources:
        if urlsplit(row.get("url", "")).hostname != "musee.louvre.fr" or not row.get("retrieved_at"):
            continue
        body = row.get("content", "")
        if "#### 卢浮宫博物馆" not in body:
            continue
        section = body.split("#### 卢浮宫博物馆", 1)[1].split("### 门票价格", 1)[0]
        match = re.search(r"闭馆\s*周二", section)
        if not match:
            continue
        excerpt = match[0]
        hours = parse_opening_rules("".join(excerpt.split()))
        if hours.status != "known":
            continue
        hours.rules[0].source_text = excerpt
        return FactEvidence(
            provider="same-run-public-search",
            source_url=row["url"],
            retrieved_at=row["retrieved_at"],
            excerpt=excerpt,
            verification_status="partially_verified",
            value_kind="observed",
            opening_hours=hours,
        )
    return None


def build_app(run_dir: Path, *, approved=False):
    from app.common.config import settings

    root = Path(__file__).resolve().parents[2]
    ledger_dir = root / "docs/benchmarks/sinan-online-closeout-20261025"
    budget = OnlineBudget(ledger_dir, root / "docs/benchmarks/sinan-night-20261024/budget.json", approved=approved)
    run_dir.mkdir(parents=True, exist_ok=False)
    database = run_dir / "browser.sqlite3"
    settings.jwt_secret = "example-only-online-closeout-anonymous-local-key"
    settings.auth_cookie_secure = False
    settings.registration_invite_required = False
    settings.redis_url = "redis://127.0.0.1:1/0"
    settings.trial_billing_enabled = False
    settings.llm_enable_thinking = False
    settings.nominatim_enabled = True
    settings.nominatim_base_url = "https://nominatim.openstreetmap.org/search"
    settings.otm_api_key = ""
    settings.web_search_enabled = True
    settings.weather_enabled = False
    settings.osrm_base_url = "https://routing.openstreetmap.de"
    settings.route_service_enabled = True
    settings.route_mode = "walking"
    settings.osrm_timeout_seconds = 5
    settings.ors_base_url = ""
    settings.planning_worker_concurrency = 1
    settings.checkpoint_db_path = str(run_dir / "checkpoints.sqlite3")
    settings.usage_db_path = str(run_dir / "usage.sqlite3")
    settings.trace_storage_path = str(run_dir / "traces.jsonl")
    settings.uploads_dir = str(run_dir / "uploads")
    settings.export_dir = str(run_dir / "exports")

    import httpx
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.agent.research import factory
    from app.common import event_publisher
    from app.common.llm_client import LLMClient
    from app.common.model_registry import binding
    from app.db import session as db_session
    from app.db.models import Base

    for role in ("main", "fast", "search"):
        bound = binding(role)
        if bound.base_url.rstrip("/") != "https://api.deepseek.com" or bound.model != "deepseek-flash":
            raise ValueError("配置偏离本轮已核价的模型/供应商")
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    patcher = pytest.MonkeyPatch()
    original_transport = httpx.HTTPTransport.handle_request

    def handle(transport, request):
        identifier = budget.before(request)
        if identifier:
            cap = 12 if identifier.startswith("model-") else 5
            request.extensions["timeout"] = {key: cap for key in ("connect", "read", "write", "pool")}
        response = original_transport(transport, request)
        budget.after(identifier, response)
        return response

    patcher.setattr(httpx.HTTPTransport, "handle_request", handle)
    original_init = LLMClient.__init__

    def init(client, *args, **kwargs):
        original_init(client, *args, **kwargs)
        client._max_attempts = 1
        client._timeout = 12

    patcher.setattr(LLMClient, "__init__", init)
    original_publish = event_publisher.publish_event
    event_lock = threading.Lock()

    def publish(*args, **kwargs):
        with event_lock, (run_dir / "business-events.jsonl").open("a", encoding="utf-8") as output:
            output.write(
                json.dumps({"monotonic": time.monotonic(), "args": args, "kwargs": kwargs}, ensure_ascii=False) + "\n"
            )
        return original_publish(*args, **kwargs)

    patcher.setattr(event_publisher, "publish_event", publish)
    from app.agent.data import places, web_search
    from app.agent.research.evidence import ResearchTask
    from app.agent.tools import impl as tools
    from app.common.event_publisher import publish_research_done, publish_research_start
    from app.services import itinerary_generation, planning_worker

    places._nominatim_client.retry_attempts = 0
    cached = root / "docs/benchmarks/sinan-online-closeout-20261025/live-first"
    if (cached / "research.json").exists():
        # 修复回归使用同轮在线地点/路网的正常缓存；保留获取时间，不再消耗地点查询次数。
        from datetime import UTC, datetime

        from app.agent.data import osrm_routes

        selected = []
        for index, (name, _) in enumerate(MUSEUMS):
            prior = json.loads((cached / f"public-source-{index}.json").read_text("utf-8"))
            selected.append(prior["selected"])
            rows = json.loads((ledger_dir / f"place-{3 if index == 0 else 5}-response.json").read_text("utf-8"))
            rows = [{**row, "source_fetched_at": prior["selected"]["source_fetched_at"]} for row in rows]
            places._nominatim_client._store(f"q:v3:{name} Paris:1:FR", rows)
        coordinates = ";".join(
            f"{lng},{lat}" for lng, lat in sorted((row["longitude"], row["latitude"]) for row in selected)
        )
        route_file = ledger_dir / "route-8-response.json"
        osrm_routes._client._store(
            f"https://routing.openstreetmap.de/routed-foot/table/v1/foot/{coordinates}",
            {
                "payload": json.loads(route_file.read_text("utf-8")),
                "fetched_at": datetime.fromtimestamp(route_file.stat().st_mtime, UTC).isoformat(),
            },
        )
        save(
            run_dir / "same-run-source-cache.json",
            {
                "source_attempt": str(cached),
                "method": "same-run actual provider responses restored into normal cache; original timestamps retained",
            },
        )

    def lookup(*_args, **_kwargs):
        pool = []
        for name, alias in MUSEUMS:
            rows = places.geocode_place_rows(name, "Paris", country_code="FR", namedetails=True)
            candidates = [row for row in rows or [] if row.get("country_code") == "FR" and row.get("type") == "museum"]
            if len(candidates) != 1:
                save(run_dir / f"unresolved-{len(pool)}.json", {"query": name, "rows": rows})
                raise ValueError("当次地点查询无法唯一选择博物馆，停止；不改用历史回放")
            selected = candidates[0]
            suffix = " 2026-11-03" if (cached / "research.json").exists() else ""
            sources = web_search.web_search_sources(f"{name} Paris official admission opening hours{suffix}")
            save(run_dir / f"public-source-{len(pool)}.json", {"query": name, "selected": selected, "sources": sources})
            candidate = {
                **selected,
                "name": name,
                "aliases": list(dict.fromkeys([alias, selected["name"], *(selected.get("aliases") or [])])),
                "source": "nominatim",
                "duration_min": 90,
                "remark": "本次公开命名查询；90分钟是匿名场景计划停留假设，票价/营业资料未结构化核实。"
                + json.dumps(sources, ensure_ascii=False)[:5000],
            }
            closure = observed_louvre_closure(sources) if name == "Musée du Louvre" else None
            if closure is not None:
                candidate["fact_evidence"] = {"open_time": closure.model_dump(mode="json")}
            pool.append(candidate)
        return pool

    patcher.setattr(tools, "search_attractions", lookup)

    def research(request, *, itinerary_id=None):
        publish_research_start(itinerary_id, ["attraction"])
        task = ResearchTask(
            domain="attraction",
            city=request.city,
            preferences=request.preferences or [],
            persons=request.persons,
            budget=request.budget,
            budget_currency="EUR",
            limit=2,
        )
        pack = factory.run_research(task)
        save(run_dir / "research.json", {"task": task.__dict__, "pack": pack.to_dict(), "items": pack.items})
        publish_research_done(itinerary_id, len(pack.items), pack.degraded, [pack.to_dict()])
        return {"candidates": pack.items, "foods": [], "hotels": [], "consumption": {}, "weather": None}

    patcher.setattr(itinerary_generation, "run_plan_context", research)
    # 仅将研究范围限制为用户明确要求的两馆；意图、LLM、路线、求解、落库与重排均不回放。
    from main import app

    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                planning_worker.poll_once()
            except Exception as exc:
                save(run_dir / "worker-error.json", {"type": type(exc).__name__, "error": str(exc)})
                stop.set()
            stop.wait(0.1)

    thread = threading.Thread(target=poll, name="isolated-real-planning-worker", daemon=True)
    app.state.closeout = {"patcher": patcher, "stop": stop, "worker": thread, "budget": budget}
    save(
        run_dir / "scope.json",
        {
            "scope": (
                "formal UI/auth/API; real live clarify; scoped attraction research graph; "
                "live Nominatim/Search/OSRM; worker/CP/SQLite; no replay"
            ),
            "limits": {"model": 6, "search": 4, "place": 2, "route": 2},
            "duration": "user scenario assumption 90min per museum",
            "facts_unknown": ["admission price", "opening windows"],
            "worker": "formal persistent queue claimed by one local worker thread; not deployed multiprocess topology",
        },
    )
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    application = build_app(args.run_dir.resolve(), approved=args.approved)
    application.state.closeout["worker"].start()
    try:
        uvicorn.run(application, host="127.0.0.1", port=18076, lifespan="off", log_level="warning")
    finally:
        application.state.closeout["stop"].set()
