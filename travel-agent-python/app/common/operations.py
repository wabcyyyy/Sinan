"""现有依赖探针与生成失败接入同一告警出口。无 webhook 时仍记录事件日志。"""

import logging
from datetime import timedelta

from sqlalchemy import func, select

from app.common import deep_health
from app.common.config import settings
from app.common.external_client import redact_secrets
from app.common.http_client import api_client
from app.db.models import ItineraryMain
from app.db.session import session_scope

logger = logging.getLogger(__name__)
_dependency_status = ""
_seen_failures: set[int] = set()


def notify(event: str, detail: dict) -> None:
    logger.warning("operations %s: %s", event, detail)
    if settings.llm_alert_webhook_url:
        response = api_client().post(settings.llm_alert_webhook_url, json={"event": event, "detail": detail})
        response.raise_for_status()


def check() -> None:
    global _dependency_status, _seen_failures
    state = deep_health.snapshot()
    fingerprint = str((state["mysql"], state["redis"]))
    if fingerprint != _dependency_status:
        if _dependency_status or state["status"] != "up":
            notify("dependencies." + state["status"], state)
        _dependency_status = fingerprint
    if state["mysql"] != "up":
        return
    try:
        with session_scope() as session:
            # 与 updated_at 同用数据库时钟；本机开发时 Windows 与 MySQL 可不同时区。
            cutoff = session.execute(select(func.current_timestamp())).scalar_one() - timedelta(minutes=5)
            failed = set(
                session.scalars(
                    select(ItineraryMain.id).where(
                        ItineraryMain.gen_state.in_(("FAILED", "PARTIAL")),
                        ItineraryMain.updated_at >= cutoff,
                    )
                ).all()
            )
        if failed - _seen_failures:
            notify("generation.failed", {"itineraryIds": sorted(failed - _seen_failures)})
        _seen_failures = failed
    except Exception as exc:
        logger.warning("operations monitor failed: %s", redact_secrets(exc))
