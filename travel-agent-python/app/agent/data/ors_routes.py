"""ORS 静态路网 adapter，接入既有 RouteService.fetcher，不提供实时交通。"""

import math
from datetime import UTC, datetime

from app.agent.runtime.run_limits import current_limits
from app.common.config import settings
from app.common.http_client import api_client, contact_user_agent

_PROFILES = {"walking": "foot-walking", "driving": "driving-car"}


def fetch_ors_route(first: dict, second: dict, mode: str, departure_time: str | None) -> dict | None:
    base = settings.ors_base_url.strip().rstrip("/")
    profile = _PROFILES.get(mode)
    if not base or profile is None:
        return None
    coords = [[item.get("longitude"), item.get("latitude")] for item in (first, second)]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for row in coords for value in row):
        return None
    limits = current_limits()
    if limits is not None:
        limits.check("tool")
    headers = {"User-Agent": contact_user_agent()}
    if settings.ors_api_key:
        headers["Authorization"] = settings.ors_api_key
    response = api_client().post(
        f"{base}/v2/matrix/{profile}",
        json={"locations": coords, "sources": ["0"], "destinations": ["1"], "metrics": ["duration", "distance"]},
        headers=headers,
        timeout=settings.ors_timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    seconds, distance = payload["durations"][0][0], payload["distances"][0][0]
    if seconds is None or distance is None:
        return None  # 路网不连通，不能伪造“零分钟可达”。
    if not all(
        isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 for value in (seconds, distance)
    ):
        raise ValueError("ORS returned invalid route metrics")
    metadata = payload.get("metadata") or {}
    return {
        "from_poi_id": str(first.get("poi_id") or first.get("id") or first.get("name")),
        "to_poi_id": str(second.get("poi_id") or second.get("id") or second.get("name")),
        "mode": mode,
        "distance_m": distance,
        "duration_min": math.ceil(seconds / 60),
        "source": "openrouteservice",
        "fetched_at": datetime.now(UTC).isoformat(),
        "data_date": (metadata.get("engine") or {}).get("graph_date"),
        "region": settings.ors_region or None,
        "degraded": False,
        "confidence": 0.8,
        "time_dependent": False,
    }
