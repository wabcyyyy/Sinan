"""P1 有界来源探针：五个命名地点各查一次，遵守既有 Nominatim 缓存/1rps。

仅免费公共地点信息与资源/配置检查，不调用模型或托管路线、不部署/下载路网。
留出城市只检查数据可取得性，不读取/调试任何留出规划结果。
"""

import argparse
import json
import platform
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.data.places import geocode_place_rows
from app.common.config import settings
from app.common.http_client import api_client

CASES = (
    ("development", "杭州", "西湖", "CN", "Asia/Shanghai", "CNY"),
    ("development", "京都", "清水寺", "JP", "Asia/Tokyo", "JPY"),
    ("development", "Paris", "Musée du Louvre", "FR", "Europe/Paris", "EUR"),
    ("holdout_availability_only", "Lisboa", "Mosteiro dos Jerónimos", "PT", "Europe/Lisbon", "EUR"),
    ("holdout_availability_only", "Bangkok", "Wat Phra Kaew", "TH", "Asia/Bangkok", "THB"),
)


def probe_places():
    records = []
    for split, city, name, country, zone, currency in CASES:
        started = datetime.now(UTC)
        rows = geocode_place_rows(name, city, namedetails=True, country_code=country)
        records.append(
            {
                "split": split,
                "city": city,
                "query_name": name,
                "country_code": country,
                "time_zone": zone,
                "currency": currency,
                "locale_source": "probe scenario, not inferred from geocoder",
                "local_date": started.astimezone(ZoneInfo(zone)).date().isoformat(),
                "retrieved_utc": started.isoformat(),
                "lookup_status": "unavailable" if rows is None else "found" if rows else "not_found",
                "country_match_count": sum(row.get("country_code") == country for row in rows or []),
                "rows": rows,
                "price_coverage": "unknown",
                "opening_coverage": "unknown",
            }
        )
    return records


def local_ors_probe():
    base = settings.ors_base_url.strip()
    if not base:
        return {"configured": False, "status": "not_available", "reason": "ORS_BASE_URL is empty"}
    if urlparse(base).hostname not in ("127.0.0.1", "localhost", "::1"):
        return {"configured": True, "status": "not_probed", "reason": "hosted route quota/budget not authorized"}
    try:
        response = api_client().get(f"{base.rstrip('/')}/v2/health", timeout=3)
        return {
            "configured": True,
            "status": "available" if response.status_code == 200 else "unavailable",
            "http_status": response.status_code,
            "region": settings.ors_region or None,
        }
    except Exception as exc:
        return {"configured": True, "status": "unavailable", "error_type": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="执行最多五个免费命名地点查询及区域文件 HEAD")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    disk = shutil.disk_usage(Path.cwd())
    resource = {
        "platform": platform.platform(),
        "disk_free_bytes": disk.free,
        "docker_cli_present": shutil.which("docker") is not None,
    }
    regional = {
        "region": "ile-de-france",
        "data_url": "https://download.geofabrik.de/europe/france/ile-de-france-latest.osm.pbf",
        "status": "not_requested",
        "downloaded": False,
    }
    if args.online:
        try:
            response = api_client().head(regional["data_url"], timeout=10)
            regional.update(
                http_status=response.status_code,
                content_length=response.headers.get("content-length"),
                last_modified=response.headers.get("last-modified"),
                status="headers_observed",
            )
        except Exception as exc:
            regional.update(status="unavailable", error_type=type(exc).__name__)
    payload = {
        "task": "SINAN-UPGRADE-P1",
        "created_utc": datetime.now(UTC).isoformat(),
        "resource": resource,
        "ors": local_ors_probe(),
        "regional_data": regional,
        "places": probe_places() if args.online else [],
        "model_calls": 0,
        "hosted_route_calls": 0,
        "attribution": "© OpenStreetMap contributors, ODbL; named place lookups only, no bulk POI extraction",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "ors": payload["ors"],
                "places": [
                    {"city": row["city"], "status": row["lookup_status"], "matches": row["country_match_count"]}
                    for row in payload["places"]
                ],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
