"""Container readiness: a recent successful worker polling cycle, with no HTTP server."""

import time
from pathlib import Path

from app.common.config import settings


def clear() -> None:
    if settings.planning_health_path:
        Path(settings.planning_health_path).unlink(missing_ok=True)


def record() -> None:
    if settings.planning_health_path:
        Path(settings.planning_health_path).write_text("ready\n", encoding="ascii")


def healthy() -> bool:
    if not settings.planning_health_path:
        return False
    try:
        age = time.time() - Path(settings.planning_health_path).stat().st_mtime
        # Checkpoint recovery can occupy the polling thread within the task's deadline.
        return 0 <= age <= max(30, settings.itinerary_deadline_seconds + settings.planning_live_seconds)
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
