"""Current live NL + named public sources through formal local pages, worker and SQLite.

Only one current model client and the original cumulative ledger; no deployment or historical response replay.
"""

import argparse
import threading
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx

from scripts.run_p4_acceptance import save
from scripts.serve_screenshot_browser import build_app as base_app
from scripts.sinan_nl_budget import NLBudget

SOURCES = (
    ("Musée du Louvre", "卢浮宫", "https://www.louvre.fr/zh-hans/canguanfuwu"),
    ("Musée d'Orsay", "奥赛博物馆", "https://www.musee-orsay.fr/en/visit"),
)


class PageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def build_app(run_dir):
    original_http = httpx.HTTPTransport.handle_request
    app = base_app(run_dir, inline_worker=False)
    patcher = app.state.p3_patcher
    from app.agent.data import places
    from app.agent.editing import clarify
    from app.common.config import settings
    from app.common.model_registry import binding
    from app.services import itinerary_city, itinerary_generation, planning_worker
    from scripts.eval_sinan_nl import ArmClient

    root = Path(__file__).resolve().parents[2]
    ledger_path = root / "docs/benchmarks/sinan-nl-01-20261025/budget.json"
    ledger = NLBudget(ledger_path, approved=True)
    client = ArmClient(ledger, run_dir / "models", binding("fast"), "night-live-" + run_dir.name)
    settings.registration_invite_required = False
    settings.nominatim_enabled = True
    settings.nominatim_base_url = "https://nominatim.openstreetmap.org/search"
    settings.osrm_base_url = "https://routing.openstreetmap.de"
    settings.osrm_timeout_seconds = 5
    settings.route_mode = "walking"
    settings.route_service_enabled = True
    settings.llm_enable_thinking = False
    places._nominatim_client.retry_attempts = 0
    network = []
    lock = threading.Lock()
    limits = {"model": 3, "place": 3, "route": 2, "official": 2}

    def transport(channel, request):
        host, path = request.url.host, request.url.path
        if host == "api.deepseek.com" and path == "/chat/completions" and request.method == "POST":
            kind = "model"  # ArmClient reserves before reaching transport.
        elif host == "nominatim.openstreetmap.org" and path == "/search" and request.method == "GET":
            kind = "place"
        elif host == "routing.openstreetmap.de" and path.startswith("/routed-foot/table/v1/foot/"):
            kind = "route"
        elif str(request.url) in {row[2] for row in SOURCES} and request.method == "GET":
            kind = "official"
        else:
            raise ValueError("outside night live public-source scope")
        if request.url.scheme != "https" or datetime.now(UTC) >= datetime(2026, 10, 26, tzinfo=UTC):
            raise ValueError("HTTPS scope/deadline")
        with lock:
            if sum(row["kind"] == kind for row in network) >= limits[kind]:
                raise ValueError("night live request count exhausted")
            record: dict[str, object] = {"kind": kind, "url": str(request.url), "at": datetime.now(UTC).isoformat()}
            network.append(record)
            save(run_dir / "network.json", network)
        try:
            response = original_http(channel, request)
            response.read()
            record["http_status"] = response.status_code
            (run_dir / f"http-{len(network)}-response.data").write_bytes(response.content)
            return response
        except Exception as error:
            record["error"] = type(error).__name__
            raise
        finally:
            save(run_dir / "network.json", network)

    patcher.setattr(httpx.HTTPTransport, "handle_request", transport)
    patcher.setattr(clarify, "get_llm_client", lambda *_a, **_kw: client)
    patcher.setattr(itinerary_city, "run_clarify", clarify.run_clarify)
    context = None

    def research(_request, **_kwargs):
        nonlocal context
        if context is not None:
            return context
        pool = []
        for name, alias, source in SOURCES:
            rows = places.geocode_place_rows(name, "Paris", country_code="FR", namedetails=True)
            matching = [r for r in rows or [] if r.get("country_code") == "FR" and r.get("type") == "museum"]
            if len(matching) != 1:
                save(run_dir / f"place-unresolved-{len(pool)}.json", {"query": name, "rows": rows})
                raise ValueError("current public named query not unique; no historical fallback")
            fetched = datetime.now(UTC).isoformat()
            with httpx.Client(timeout=10, follow_redirects=False) as http:
                response = http.get(source)
            page = PageText()
            if response.status_code == 200:
                page.feed(response.text)
            observation = {
                "url": source,
                "retrieved_at": fetched,
                "http_status": response.status_code,
                "text": " ".join(page.parts)[:50000],
            }
            save(run_dir / f"public-source-{len(pool)}.json", {"selected": matching[0], "observation": observation})
            pool.append(
                {
                    **matching[0],
                    "name": name,
                    "aliases": list(dict.fromkeys([alias, matching[0]["name"], *(matching[0].get("aliases") or [])])),
                    "duration_min": 90,
                    "remark": "90分钟为用户计划；票价/开放未完成字段核实。" + observation["text"][:4000],
                }
            )
        context = {"candidates": pool, "foods": [], "hotels": [], "consumption": {}, "weather": None}
        save(
            run_dir / "research.json",
            {"method": "current deterministic named public retrieval, no research LLM", "context": context},
        )
        return context

    patcher.setattr(itinerary_generation, "run_plan_context", research)
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                planning_worker.poll_once()
            except Exception as error:
                save(run_dir / "worker-error.json", {"type": type(error).__name__, "error": str(error)})
                stop.set()
            stop.wait(0.1)

    app.state.night_live = {"worker": threading.Thread(target=poll, daemon=True), "stop": stop}
    save(
        run_dir / "scope.json",
        {
            "model": "current real clarify",
            "source": "current public named retrieval",
            "ledger": str(ledger_path),
            "limits": limits,
            "unknown": ["price", "opening", "unavailable routes"],
            "worker": "one actual queue worker in same process; isolated SQLite",
        },
    )
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    application = build_app(args.run_dir.resolve())
    application.state.night_live["worker"].start()
    try:
        uvicorn.run(application, host="127.0.0.1", port=18078, lifespan="off", log_level="warning")
    finally:
        application.state.night_live["stop"].set()
