"""Isolated MySQL/API/worker load experiment through a local fake model HTTP server.

Run --help for bounded scenarios. Research policy/content are fixed; plan_days,
LLMClient (HTTP + SSE + retry + usage), admission and delivery remain real.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from planning_load_fixture import FakeModel, install_model_fixture
from verify_planning_worker import Processes, day_one_items, delivered, read_sse, skip_enrichment, state, wait_for
from verify_planning_worker import main as isolated_main


def child(role: str, port: int, marker: str) -> None:
    install_model_fixture(marker)
    from app.services import itinerary_generation, planning_worker

    itinerary_generation.enricher_pool.submit = skip_enrichment
    if os.environ.get("SINAN_LOAD_SCHEDULER") == "baseline":
        from app.services import generation_recovery

        def baseline_cycle(recover_at: float) -> float:
            # Historical ordering at 1bf4546; only this experiment can select it.
            planning_worker.poll_once()
            if time.monotonic() >= recover_at and planning_worker.available():
                generation_recovery.recover()
                recover_at = time.monotonic() + generation_recovery.SCAN_INTERVAL_SECONDS
            return recover_at

        planning_worker.run_once = baseline_cycle
    if role == "api":
        import uvicorn

        from app.common import cron
        from main import app

        cron.start_all = lambda: None
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    else:
        from worker import main

        main()


def start_worker(processes: Processes, marker: str = ""):
    health = processes.directory / f"worker-{len(processes.children)}.heartbeat"
    os.environ["PLANNING_HEALTH_PATH"] = str(health)
    process, _ = processes.start("worker", marker)
    wait_for(lambda: health.exists() or (process.poll() is not None and fail("worker exited")), "worker ready")
    return process


def fail(message: str):
    raise AssertionError(message)


def clients_for(urls: list[str], count: int):
    import httpx
    from sqlalchemy import select

    from app.db.models import SysUser
    from app.db.session import session_scope

    with session_scope() as session:
        first = session.get(SysUser, 1)
        assert first is not None
        for index in range(2, count + 1):
            if session.scalar(select(SysUser.id).where(SysUser.id == index)) is None:
                session.add(SysUser(id=index, username=f"load-{index}", password=first.password))

    def connect(index):
        client = httpx.Client(base_url=urls[(index - 1) % len(urls)], timeout=20, trust_env=False)
        response = client.post(
            "/api/auth/login",
            json={"username": "probe" if index == 1 else f"load-{index}", "password": "example-probe-password"},
        )
        assert response.status_code == 200 and response.json()["code"] == 200
        return client

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(connect, range(1, count + 1)))


def submit(client) -> dict:
    started = time.time()
    response = client.post("/api/itinerary/generate", json={"city": "杭州", "days": 2})
    assert response.status_code in (200, 429), response.text
    payload = response.json()
    return {
        "submitted": started,
        "admission_ms": (time.time() - started) * 1000,
        "status": response.status_code,
        "trip": payload["data"]["id"] if response.status_code == 200 else None,
    }


def wait_terminal(rows: list[dict], timeout: float) -> dict[int, float]:
    finished: dict[int, float] = {}

    def poll():
        for row in rows:
            trip = row["trip"]
            if trip not in finished and state(trip)["state"] not in ("QUEUED", "RUNNING"):
                finished[trip] = time.time()
        return len(finished) == len(rows)

    wait_for(poll, "all accepted tasks terminal", timeout)
    return finished


def percentile(values: Sequence[float], fraction: float) -> float:
    return round(sorted(values)[min(len(values) - 1, int((len(values) - 1) * fraction))], 3) if values else 0


def summarize(rows: list[dict], finished: dict[int, float], model: FakeModel) -> dict:
    from sqlalchemy import select

    from app.db.models import ItineraryDay, ItineraryMain, PlanningTask
    from app.db.session import session_scope

    accepted = [row for row in rows if row["status"] == 200]
    ids = [row["trip"] for row in accepted]
    with session_scope() as session:
        mains = session.scalars(select(ItineraryMain).where(ItineraryMain.id.in_(ids))).all()
        tasks = session.scalars(select(PlanningTask).where(PlanningTask.itinerary_id.in_(ids))).all()
        assert len(mains) == len(accepted), "rejected submission left an itinerary shell"
        assert session.scalar(select(ItineraryMain.id).where(ItineraryMain.id.not_in(ids)).limit(1)) is None
        states = {main.gen_state: sum(item.gen_state == main.gen_state for item in mains) for main in mains}
        execution_states = {task.state: sum(item.state == task.state for item in tasks) for task in tasks}
        finished_ids = {task.itinerary_id for task in tasks if task.state == "FINISHED"}
        days = session.scalars(select(ItineraryDay).where(ItineraryDay.itinerary_id.in_(ids))).all()
        complete_ids = {
            main.id
            for main in mains
            if main.gen_state == "COMPLETED"
            and main.id in finished_ids
            and sum(day.itinerary_id == main.id and day.generation_status == "SUCCEEDED" for day in days) == main.days
        }
        reasons = sorted({main.plan_note for main in mains if main.gen_state != "COMPLETED" and main.plan_note})
        codes = {task.result_code: sum(item.result_code == task.result_code for item in tasks) for task in tasks}
        first_calls = {
            task.itinerary_id: min(call["started"] for call in model.calls if call["trip"] == task.itinerary_id)
            for task in tasks
            if any(call["trip"] == task.itinerary_id for call in model.calls)
        }
        waits = [
            max(0, first_calls[task.itinerary_id] - task.submit_at.timestamp())
            for task in tasks
            if task.itinerary_id in first_calls
        ]
    durations = [finished[row["trip"]] - row["submitted"] for row in accepted]
    return {
        "submitted": len(rows),
        "accepted": len(accepted),
        "rejected_429": len(rows) - len(accepted),
        "delivery_states": states,
        "execution_states": execution_states,
        "complete_deliveries": len(complete_ids),
        "result_codes": codes,
        "failure_notes": reasons,
        "complete_rate": len(complete_ids) / len(accepted),
        "admission_p95_ms": percentile([row["admission_ms"] for row in rows], 0.95),
        "queue_to_first_http_p95_seconds": percentile(waits, 0.95),
        "never_started_http": len(accepted) - len(first_calls),
        "delivery_p50_seconds": percentile(durations, 0.5),
        "delivery_p95_seconds": percentile(durations, 0.95),
        "model_http_requests": len(model.calls),
        "model_http_429": sum(call["status"] == 429 for call in model.calls),
        "peak_model_http_connections": model.peak,
        "rejected_shells": 0,
    }


def burst(processes: Processes, directory: Path, model: FakeModel, args) -> dict:
    _, url1 = processes.start("api")
    _, url2 = processes.start("api")
    start_worker(processes)
    clients = clients_for([url1, url2], args.requests)
    model.gate.clear()  # Hold model calls until admission finishes: reproducible overflow.
    try:
        with ThreadPoolExecutor(max_workers=args.requests) as pool:
            rows = list(pool.map(submit, clients))
        assert sum(row["status"] == 200 for row in rows) == min(args.capacity, args.requests)
        model.gate.set()
        finished = wait_terminal([row for row in rows if row["status"] == 200], args.deadline + 15)
        result = summarize(rows, finished, model)
        return result
    finally:
        model.gate.set()
        for client in clients:
            client.close()


def recovery(processes: Processes, directory: Path, model: FakeModel, args) -> dict:
    _, url1 = processes.start("api")
    _, url2 = processes.start("api")
    clients = clients_for([url1, url2], 7)
    marker = directory / "victim-day-one"
    dying = start_worker(processes, str(marker))
    try:
        victim = submit(clients[0])
        trip = victim["trip"]
        wait_for(marker.exists, "first day persisted")
        before, original_items = state(trip), day_one_items(trip)
        start_worker(processes)
        model.gate.clear()
        backlog = [submit(client) for client in clients[1:]]
        assert all(row["status"] == 200 for row in backlog)
        wait_for(lambda: any(call["trip"] != trip for call in model.calls), "survivor busy")
        processes.kill(dying)
        killed_at = time.time()
        model.gate.set()
        wait_for(lambda: delivered(trip), "victim recovered under backlog", args.deadline + 10)
        recovered_at = time.time()
        after = state(trip)
        assert original_items and day_one_items(trip) == original_items
        assert after["attempt"] == 2 and after["deadlineAt"] == before["deadlineAt"]
        assert after["consumed"]["consumed_llm_calls"] > before["consumed"]["consumed_llm_calls"] > 0
        resumed = min(call["started"] for call in model.calls if call["trip"] == trip and call["started"] > killed_at)
        new_starts = {call["trip"] for call in model.calls if call["trip"] != trip and call["started"] < resumed}
        fair = len(new_starts) < len(backlog)
        if args.scheduler == "current":
            assert fair, "new queue drained before the expired owner got a slot"
        finished = wait_terminal(backlog, args.deadline + 10)
        frames = read_sse(clients[0], url2, trip, 0)
        assert frames[-1][1]["type"] in ("complete", "done")
        detail = clients[0].get(f"{url2}/api/itinerary/{trip}").json()["data"]
        assert detail["genState"] == "COMPLETED" and all(
            day["generationStatus"] == "SUCCEEDED" for day in detail["dayList"]
        )
        return {
            "backlog_jobs": len(backlog),
            "new_jobs_started_before_takeover": len(new_starts),
            "takeover_before_backlog_drained": fair,
            "takeover_wait_seconds": round(resumed - killed_at, 3),
            "recovery_delivery_seconds": round(recovered_at - killed_at, 3),
            "all_backlog_delivered": all(delivered(row["trip"]) for row in backlog),
            "last_backlog_terminal_seconds": round(max(finished.values()) - killed_at, 3),
            "completed_day_preserved": True,
            "deadline_and_usage_preserved": True,
            "cross_api_sse_and_detail": True,
        }
    finally:
        model.gate.set()
        for client in clients:
            client.close()


def experiment(processes: Processes, directory: Path, args) -> dict:
    model = FakeModel(args.delay, args.retry)
    os.environ.update(
        {
            "LLM_BASE_URL": model.url + "/v1",
            "LLM_API_KEY": "example-local-model-only",
            "LLM_MODEL": "probe-model",
            "LLM_MAIN_FALLBACK_PROVIDER": "",
            "PLANNING_QUEUE_CAPACITY": str(args.capacity),
            "PLANNING_WORKER_CONCURRENCY": str(args.concurrency),
            "ITINERARY_DEADLINE_SECONDS": str(args.deadline),
            "SINAN_LOAD_SCHEDULER": args.scheduler,
        }
    )
    try:
        result = (recovery if args.case == "recovery" else burst)(processes, directory, model, args)
        return {
            "case": args.case,
            "scheduler": args.scheduler,
            "concurrency": args.concurrency,
            "capacity": args.capacity,
            "deadline_seconds": args.deadline,
            "fake_day_http_delay_seconds": args.delay,
            "lease_seconds": 3,
            "real_model_calls": 0,
            "scope": "one host; real MySQL/API/worker/LLMClient HTTP; fixed research/content; enrichment disabled",
            **result,
        }
    finally:
        processes.close()
        model.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("burst", "recovery"), default="burst")
    parser.add_argument("--scheduler", choices=("current", "baseline"), default="current")
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--concurrency", type=int, choices=(1, 2, 4), default=4)
    parser.add_argument("--requests", type=int, default=12)
    parser.add_argument("--delay", type=float, default=0.35)
    parser.add_argument("--deadline", type=float, default=12)
    parser.add_argument("--retry", action="store_true")
    parser.add_argument("--require-full", action="store_true")
    parser.add_argument("--expect-expired", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    assert 0 < args.requests <= 40 and 0 < args.capacity <= 40 and 0 < args.deadline <= 60 and 0 <= args.delay <= 3

    def run(processes: Processes, directory: Path) -> dict:
        result = experiment(processes, directory, args)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.case == "burst":
            if args.require_full:
                assert result["complete_rate"] == 1, result
            if args.expect_expired:
                assert result["complete_rate"] < 1, "baseline did not hit its delivery window"
            if args.retry:
                assert result["model_http_429"] > 0 and result["complete_rate"] == 1
        return result

    isolated_main(run, Path(__file__).resolve())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        main()
