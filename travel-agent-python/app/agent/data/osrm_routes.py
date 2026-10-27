"""显式启用的 FOSSGIS OSRM 接缝；少量免费验收，不作为默认生产供应商。

同一地点对一次 Table 请求保留两个方向，反向查询复用原响应。复用公共取数
客户端的单车道 1rps、缓存、deadline 和错误记录，不查询公交或实时交通。
"""

import math
from datetime import UTC, datetime

from app.agent.runtime.run_limits import RunLimitExceeded, current_limits
from app.common.config import settings
from app.common.external_client import INTERACTIVE, ExternalClient, fetch_json
from app.common.http_client import api_client, contact_user_agent

_PROFILES = {"walking": ("routed-foot", "foot"), "driving": ("routed-car", "car")}
ATTRIBUTION = "Routing: OSRM / FOSSGIS; © OpenStreetMap contributors (ODbL)"
_client: ExternalClient = ExternalClient(
    name="osrm_routes",
    ttl_seconds=settings.route_cache_ttl,
    min_interval_seconds=1.0,
    rate_group="osrm_routes",
    limits_provider=current_limits,
    limit_error=RunLimitExceeded,
)


def _coords(item):
    values = (item.get("longitude"), item.get("latitude"))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
        return None
    return values if -180 <= values[0] <= 180 and -90 <= values[1] <= 90 else None


def fetch_osrm_route(first: dict, second: dict, mode: str, departure_time: str | None) -> dict | None:
    base, profile = settings.osrm_base_url.strip().rstrip("/"), _PROFILES.get(mode)
    first_coords, second_coords = _coords(first), _coords(second)
    if not base or profile is None or first_coords is None or second_coords is None:
        return None
    points = sorted((first_coords, second_coords))
    first_index = points.index(first_coords)
    second_index = points.index(second_coords)
    coordinates = ";".join(f"{lng},{lat}" for lng, lat in points)
    url = f"{base}/{profile[0]}/table/v1/{profile[1]}/{coordinates}"

    def load():
        payload = fetch_json(
            _client,
            api_client(),
            url,
            params={"annotations": "duration,distance", "generate_hints": "false"},
            headers={"User-Agent": contact_user_agent()},
        )
        if payload is None or payload.get("code") in ("NoTable", "NoSegment"):
            return None
        if payload.get("code") != "Ok":
            raise ValueError(f"OSRM table failed: {payload.get('code')}")
        return {"payload": payload, "fetched_at": datetime.now(UTC).isoformat()}

    _client.timeout_seconds = settings.osrm_timeout_seconds
    observed = _client.call(url, load, lane=INTERACTIVE)
    if observed is None:
        return None
    payload = observed["payload"]
    seconds = payload["durations"][first_index][second_index]
    distance = payload["distances"][first_index][second_index]
    if seconds is None or distance is None or [first_index, second_index] in payload.get("fallback_speed_cells", []):
        return None  # 不把断路或供应商的直线估算当作路网事实。
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in (seconds, distance)):
        raise ValueError("OSRM returned invalid route metrics")
    return {
        "mode": mode,
        "distance_m": distance,
        "duration_min": math.ceil(seconds / 60),
        "source": "osrm",
        "source_url": base,
        "attribution": ATTRIBUTION,
        "fetched_at": observed["fetched_at"],
        "data_date": payload.get("data_version"),
        "degraded": False,
        "confidence": 0.8,
        "time_dependent": False,
    }
