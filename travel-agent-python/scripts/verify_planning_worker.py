"""Process-failure probe against an isolated MySQL database; model/tool outputs are fixtures.

Uses DB_HOST/PORT/USER/PASSWORD only to create a fresh sinan_worker_probe_* database.
Never migrates the configured application database. Logs/checkpoints stay in a temp folder.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture_plan(day: int) -> dict:
    return {
        "dayNo": day,
        "items": [
            {
                "itemType": "attraction",
                "poiName": ["西湖", "灵隐寺"][day - 1],
                "cost": 45,
                "startTime": "09:00",
                "endTime": "11:30",
            },
            {
                "itemType": "food",
                "poiName": ["湖畔餐厅", "山间面馆"][day - 1],
                "cost": 40,
                "startTime": "11:30",
                "endTime": "13:00",
            },
        ],
    }


def child(role: str, port: int, marker: str) -> None:
    from app.agent import current_limits
    from app.schemas.trip import DailyPlan
    from app.services import itinerary_generation as generation
    from app.services import planning_tasks

    def context(*_args, **_kwargs):
        limits = current_limits()
        assert limits is not None
        limits.reserve_llm(10, 20)  # synthetic accounting, not a paid model request
        return {"candidates": [], "foods": [], "hotels": [], "consumption": None}

    def stream(*_args, **_kwargs):
        for day in (1, 2):
            yield {"type": "day", "plan": fixture_plan(day)}
            if day == 1 and marker:
                Path(marker).write_text("first day persisted", encoding="utf-8")
                time.sleep(120)
        yield {
            "type": "done",
            "daysExpected": 2,
            "daysEmitted": [1, 2],
            "complete": True,
            "tripTheme": None,
            "message": None,
        }

    generation.destination_problem = lambda *_: None
    generation.run_plan_context = context
    generation.run_generate_trip_stream = stream
    generation.run_generate_day = lambda request: DailyPlan.model_validate(fixture_plan(request.day_no))
    generation.enricher_pool.submit = lambda _task, *args: planning_tasks.release(args[1]) if len(args) > 1 else None
    # Outbound HTTP is forbidden, including accidentally enabled lookup paths.
    import httpx

    def deny_http(*_args, **_kwargs):
        raise AssertionError("fault probe attempted outbound HTTP")

    httpx.Client.send = deny_http
    if role == "api":
        from app.common import cron

        cron.start_all = lambda: None  # maintenance is outside this isolated failure probe
        import uvicorn

        from main import app

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    else:
        from worker import main

        main()


def wait_for(predicate, label: str, timeout: float = 35):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError(f"timed out: {label}")


class Processes:
    def __init__(self, directory: Path):
        self.directory = directory
        self.children: list[subprocess.Popen] = []
        self.logs = []

    def start(self, role: str, marker: str = "") -> tuple[subprocess.Popen, str]:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        log = (self.directory / f"{role}-{len(self.children)}.log").open("w", encoding="utf-8")
        self.logs.append(log)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--child", role, str(port), marker],
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.children.append(process)
        url = f"http://127.0.0.1:{port}"
        if role == "api":
            import httpx

            def ready():
                if process.poll() is not None:
                    raise AssertionError(f"API exited; inspect {log.name}")
                try:
                    return httpx.get(url + "/api/agent/hello", timeout=0.5).status_code == 200
                except httpx.RequestError:
                    return False

            wait_for(ready, "API ready")
        return process, url

    def kill(self, process: subprocess.Popen) -> None:
        assert process in self.children
        if process.poll() is None:
            if os.name == "nt":
                # Windows venv redirectors can have a child interpreter; kill the actual tree.
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                process.kill()
        process.wait(timeout=10)

    def close(self) -> None:
        for process in self.children:
            self.kill(process)
        for log in self.logs:
            log.close()


def state(trip: int):
    from app.services import planning_tasks

    return planning_tasks.state_of(trip)


def delivered(trip: int) -> bool:
    return state(trip)["state"] == "FINISHED"


def login(client, url: str) -> None:
    response = client.post(url + "/api/auth/login", json={"username": "probe", "password": "example-probe-password"})
    assert response.status_code == 200 and response.json()["code"] == 200


def create(client, url: str) -> int:
    response = client.post(url + "/api/itinerary/generate", json={"city": "杭州", "days": 2})
    assert response.status_code == 200, response.text
    return int(response.json()["data"]["id"])


def read_sse(client, url: str, trip: int, cursor: int, *, stop_early: bool = False) -> list[tuple[int, dict]]:
    frames = []
    with client.stream(
        "GET", f"{url}/api/itinerary/{trip}/events", headers={"Last-Event-ID": str(cursor)}, timeout=20
    ) as response:
        assert response.status_code == 200
        current = 0
        for line in response.iter_lines():
            if line.startswith("id:"):
                current = int(line[3:])
            if line.startswith("data:"):
                payload = json.loads(line[5:])
                frames.append((current, payload))
                if stop_early or payload["type"] in ("complete", "done", "error"):
                    break
    return frames


def day_one_items(trip: int) -> list[int]:
    from sqlalchemy import select

    from app.db.models import ItineraryDay, ItineraryItem
    from app.db.session import session_scope

    with session_scope() as session:
        day = session.scalar(select(ItineraryDay.id).where(ItineraryDay.itinerary_id == trip, ItineraryDay.day_no == 1))
        return list(
            session.scalars(select(ItineraryItem.id).where(ItineraryItem.day_id == day).order_by(ItineraryItem.id))
        )


def exercise(processes: Processes, directory: Path) -> dict:
    import httpx
    from sqlalchemy import select, update

    from app.db.models import GenerationEvent, PlanningTask
    from app.db.session import session_scope

    api1, url1 = processes.start("api")
    _, url2 = processes.start("api")
    with httpx.Client() as client:
        login(client, url1)
        trip = create(client, url1)
        assert state(trip)["state"] == "QUEUED" and state(trip)["workerId"] == ""
        processes.kill(api1)
        first_worker, _ = processes.start("worker")
        wait_for(lambda: delivered(trip), "accepted task after API crash")
        processes.kill(first_worker)
        login(client, url2)
        trip = create(client, url2)
        marker = directory / "day-one-persisted"
        dying_worker, _ = processes.start("worker", str(marker))
        wait_for(marker.exists, "first day commit before worker kill")
        before = state(trip)
        original_items = day_one_items(trip)
        first_frames = read_sse(client, url2, trip, 0, stop_early=True)
        cursor = first_frames[-1][0]
        assert cursor > 0 and original_items
        processes.kill(dying_worker)
        started = time.monotonic()
        processes.start("worker")
        processes.start("worker")  # real competing processes on MySQL row locks
        wait_for(lambda: delivered(trip), "worker takeover without five-minute idle")
        recovery_seconds = round(time.monotonic() - started, 3)
        after = state(trip)
        assert after["attempt"] == 2 and after["deadlineAt"] == before["deadlineAt"]
        assert after["consumed"]["consumed_llm_calls"] >= before["consumed"]["consumed_llm_calls"] > 0
        assert day_one_items(trip) == original_items
        with session_scope() as session:
            rejected = session.execute(
                update(PlanningTask)
                .where(
                    PlanningTask.itinerary_id == trip,
                    PlanningTask.task_id == before["taskId"],
                    PlanningTask.lease_owner == before["workerId"],
                )
                .values(heartbeat_at=None)
            )
            assert rejected.rowcount == 0
            events = session.scalars(select(GenerationEvent).where(GenerationEvent.itinerary_id == trip)).all()
            assert sum(json.loads(event.envelope)["type"] == "complete" for event in events) == 1
        _, new_api = processes.start("api")
        login(client, new_api)
        frames = read_sse(client, new_api, trip, cursor)
        ids = [number for number, _payload in frames if number]
        assert ids and ids == sorted(set(ids)) and min(ids) > cursor
        assert frames[-1][1]["type"] == "complete"
    return {
        "api_exit_after_accept": "passed",
        "worker_kill_after_day_one": "passed",
        "two_workers_single_takeover": "passed",
        "completed_day_preserved": "passed",
        "old_owner_update_rejected": "passed",
        "cross_instance_sse_replay": "passed",
        "deadline_and_consumed_budget_preserved": "passed",
        "recovery_seconds": recovery_seconds,
        "lease_seconds": 3,
        "scan_seconds": 10,
        "real_model_calls": 0,
        "scope": "MySQL; real API/worker processes; synthetic model outputs; one host",
    }


def main() -> None:
    from sqlalchemy import URL, create_engine

    from app.common.config import settings

    admin = create_engine(
        URL.create(
            "mysql+pymysql",
            username=settings.db_user,
            password=settings.db_password,
            host=settings.db_host,
            port=settings.db_port,
            database="mysql",
        )
    )
    database = "sinan_worker_probe_" + secrets.token_hex(6)
    assert re.fullmatch(r"sinan_worker_probe_[0-9a-f]{12}", database)
    with tempfile.TemporaryDirectory(prefix="sinan-worker-probe-") as temporary:
        processes = Processes(Path(temporary))
        try:
            with admin.begin() as connection:
                connection.exec_driver_sql(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4")
            configure(database, Path(temporary))
            from app.db import migrate
            from app.db.models import SysUser
            from app.db.session import session_scope
            from app.services.user_service import hash_password

            migrate.ensure_schema()
            with session_scope() as session:
                session.add(SysUser(id=1, username="probe", password=hash_password("example-probe-password")))
            print(json.dumps(exercise(processes, Path(temporary)), ensure_ascii=False, indent=2))
        except BaseException:
            for log in Path(temporary).glob("*.log"):
                print(log.name, log.read_text(encoding="utf-8", errors="replace")[-6000:], file=sys.stderr)
            raise
        finally:
            processes.close()
            from app.db.session import get_engine, init_engine

            get_engine().dispose()
            init_engine(None)
            with admin.begin() as connection:
                connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{database}`")
            admin.dispose()


def configure(database: str, directory: Path) -> None:
    from app.common.config import settings

    values = {
        "DB_NAME": database,
        "JWT_SECRET": secrets.token_hex(32),
        "AUTH_COOKIE_SECURE": "false",
        "AGENT_HOST": "127.0.0.1",
        "AGENT_INTERNAL_TOKEN": "",
        "LLM_API_KEY": "",
        "LLM_PROVIDERS_JSON": "{}",
        "TRIAL_BILLING_ENABLED": "false",
        "SERVE_FRONTEND": "false",
        "PLANNING_LIVE_SECONDS": "3",
        "PLANNING_HEARTBEAT_SECONDS": "1",
        "PLANNING_POLL_SECONDS": "0.1",
        "ITINERARY_DEADLINE_SECONDS": "90",
        "REDIS_URL": "redis://127.0.0.1:1/0",
        "NOMINATIM_ENABLED": "false",
        "OVERPASS_ENABLED": "false",
        "WEATHER_ENABLED": "false",
        "CHECKPOINT_DB_PATH": str(directory / "checkpoints.sqlite3"),
        "TRACE_STORAGE_PATH": str(directory / "traces.jsonl"),
        "USAGE_DB_PATH": str(directory / "usage.db"),
        "EXPORT_DIR": str(directory / "export"),
        "UPLOADS_DIR": str(directory / "uploads"),
    }
    for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"):
        values[key] = str(getattr(settings, key.lower()))
    for role in ("MAIN", "FAST", "SEARCH", "JUDGE", "VISION", "STT"):
        values[f"LLM_ROLE_{role}"] = ""
    os.environ.update(values)
    settings.db_name = database
    from app.db.session import init_engine

    init_engine(None)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(sys.argv[2], int(sys.argv[3]), sys.argv[4])
    else:
        main()
