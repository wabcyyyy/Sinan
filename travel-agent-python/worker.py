"""Run with `uv run python worker.py`; uses the same DB/config/code as the API."""

import logging
import signal
import threading
import time

from app.common import model_registry
from app.common.config import settings
from app.services import cost_ledger, generation_recovery, itinerary_generation, planning_worker

logger = logging.getLogger(__name__)


def main() -> None:
    settings.validate_boot()
    model_registry.validate()
    # API 是唯一启动迁移者；worker 不能与首次建库并发执行 DDL。
    from sqlalchemy import select

    from app.db.models import PlanningTask
    from app.db.session import session_scope

    with session_scope() as session:
        session.execute(select(PlanningTask.request_json).limit(1))
    cost_ledger.install_backend()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    recover_at = 0.0
    try:
        while not stop.is_set():
            try:
                planning_worker.poll_once()
                if time.monotonic() >= recover_at and planning_worker.available():
                    generation_recovery.recover()
                    recover_at = time.monotonic() + generation_recovery.SCAN_INTERVAL_SECONDS
            except Exception:
                logger.exception("planning worker poll failed; retrying")
            stop.wait(settings.planning_poll_seconds)
    finally:
        # 停止领取，已有任务沿用绝对截止；硬退出由下一 worker 在租约过期后接管。
        itinerary_generation.generation_pool.shutdown()
        itinerary_generation.enricher_pool.shutdown()
        from app.services import planning_tasks

        planning_tasks.reset()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
