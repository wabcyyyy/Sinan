"""收费存在性数据源的留白实现（高德 / Google Places）。

形状先留、key 后补（2026-09-18 裁决 D2）：两家都在 `available()` 里先判 key，
**没配 key 就一次外呼都不发**；接上 key 后把名字加进
`existence_provider_order` 即可生效。它们的 `authoritative_negative=True`
意味着"查了说没有"可以作为证伪——免费源不具备这个资格（见 `existence` 模块
docstring 的量测依据）。

依赖：existence（结论类型与候选挑选）、common（配置/外呼基类）。
"""

from __future__ import annotations

from typing import Any

from app.agent.core.poi_identity import strip_name_annotation
from app.agent.grounding.existence import NOT_FOUND, ResolveResult, pick_row
from app.common.config import settings
from app.common.external_client import ExternalClient, fetch_json
from app.common.http_client import api_client

# 密钥不进缓存键：失败日志会把缓存键原样打出来。
_amap_client = ExternalClient(
    name="existence_amap", ttl_seconds=24 * 3600, negative_ttl_seconds=300, timeout_seconds=8, max_entries=2048
)
_places_client = ExternalClient(
    name="existence_places",
    ttl_seconds=24 * 3600,
    negative_ttl_seconds=300,
    timeout_seconds=8,
    max_entries=2048,
)


class AmapProvider:
    """高德开放平台（留白能力，2026-09-18 裁决保留形状）：无 key 时不发请求。

    接法：`v3/place/text` 关键字检索，`pois` 为空且 status=1 才算否证。
    """

    name = "amap"
    authoritative_negative = True  # 国内 POI 覆盖显著优于 OSM，其空结果可作为否证

    def available(self) -> bool:
        return bool(str(settings.amap_web_key or "").strip())

    def resolve(self, name: str, city: str) -> ResolveResult:
        key = str(settings.amap_web_key or "").strip()
        query = strip_name_annotation(name)[:60]
        payload = _amap_client.call(
            f"text:{city}:{query}",  # 密钥不进缓存键：失败日志会原样打出缓存键
            lambda: fetch_json(
                _amap_client,
                api_client(),
                "https://restapi.amap.com/v3/place/text",
                params={
                    "keywords": query,
                    "city": str(city or "").strip()[:40],
                    "citylimit": "true",
                    "output": "JSON",
                    "key": key,
                },
            ),
        )
        return _from_pois(
            payload,
            name,
            city,
            provider=self.name,
            key="pois",
            spellings_of=_amap_spellings,
            authoritative_negative=self.authoritative_negative,
            negative_requires=_amap_query_ok,
        )


def _amap_spellings(poi: dict[str, Any]) -> list[str]:
    branches = str(poi.get("branch") or "").strip()
    base = str(poi.get("name") or "").strip()
    return [value for value in (base, f"{base}{branches}" if branches else "") if value]


def _amap_query_ok(payload: dict[str, Any]) -> bool:
    """高德 v3 的应答级成功闸：docstring 承诺的「pois 为空**且 status=1** 才算否证」（R3-F6）。

    status 为字符串 "1"（v3 线级形状）。status!=1 / 缺失 = 这次查询本身没成功，
    空 pois 只是"没问到"——绝不能产出 authoritative_negative 去删用户点位。
    """
    return str(payload.get("status") or "").strip() == "1"


class GooglePlacesProvider:
    """Google Places（留白能力）：`places:searchText`，无 key 时不发请求。"""

    name = "google_places"
    authoritative_negative = True

    def available(self) -> bool:
        return bool(str(settings.google_places_api_key or "").strip())

    def resolve(self, name: str, city: str) -> ResolveResult:
        key = str(settings.google_places_api_key or "").strip()
        query = f"{strip_name_annotation(name)} {city}".strip()[:120]
        payload = _places_client.call(
            f"text:{query}",
            lambda: fetch_json(
                _places_client,
                api_client(),
                "https://places.googleapis.com/v1/places:searchText",
                headers={
                    "X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": "places.id,places.displayName,places.formattedAddress,places.location",
                },
                json_body={"textQuery": query, "maxResultCount": 5},
            ),
        )
        return _from_pois(
            payload,
            name,
            city,
            provider=self.name,
            key="places",
            spellings_of=_gps_spellings,
            authoritative_negative=self.authoritative_negative,
        )


def _gps_spellings(place: dict[str, Any]) -> list[str]:
    return [str((place.get("displayName") or {}).get("text") or "").strip()]


def _from_pois(
    payload: Any,
    name: str,
    city: str,
    *,
    provider: str,
    key: str,
    spellings_of: Any,
    authoritative_negative: bool,
    negative_requires: Any = None,
) -> ResolveResult:
    """两家商业源的共同形状：列表空 = 明确没有；缺字段/形状异常 = 未判定。

    `negative_requires`（R3-F6）：可选的"应答整体成功"谓词——空列表要升级成否证前
    先过这道闸（高德 status=1）；不过闸 = 宁可不否证，不可误删（deletable 会让
    `landing.drop_refuted_items` 删掉用户的点位）。
    """
    if not isinstance(payload, dict):
        return ResolveResult.unknown("provider_unavailable")
    rows = payload.get(key)
    if not isinstance(rows, list):
        # 缺键 / 形状不对：这是"没问到"，不是"没有"——更不能用它删用户的点
        return ResolveResult.unknown("provider_schema_unexpected")
    normalized = [
        row for row in (_normalize_external_row(item, spellings_of) for item in rows if isinstance(item, dict)) if row
    ]
    if not rows:
        if negative_requires is not None and not negative_requires(payload):
            # 空列表但应答级没成功（如高德 status=0）：这是"没问到"，不是"没有"
            return ResolveResult.unknown("provider_status_not_ok")
        # 只有"源回了一个空列表"才是否证（authoritative_negative 的唯一正当来源）
        return ResolveResult(
            state=NOT_FOUND, provider=provider, reason="provider_empty", authoritative_negative=authoritative_negative
        )
    if not normalized:
        # 有行但一条都没解析成功 = 我们没读懂它的形状，不是"这个地方不存在"。
        # 与本函数 docstring 的"缺字段/形状异常 = 未判定"同一条线：落到 NOT_FOUND
        # 会让 deletable=True，`landing.drop_refuted_items` 直接删掉用户的点位
        # （上游改字段名 / 少给一个键的代价是行程少一个点）。
        return ResolveResult.unknown("provider_row_unparseable")
    return pick_row(
        normalized, name, city, provider=provider, spellings_of=lambda row: list(row.get("spellings") or [])
    )


def _normalize_external_row(row: dict[str, Any], spellings_of: Any) -> dict[str, Any] | None:
    """外部行 → 判定所需的最小字段；坐标取各家不同形状（amap 是 "lng,lat" 串）。"""
    location = row.get("location")
    lng: Any = row.get("longitude")
    lat: Any = row.get("latitude")
    if isinstance(location, dict):
        lng, lat = location.get("longitude") or location.get("lng"), location.get("latitude") or location.get("lat")
    elif isinstance(location, str) and "," in location:
        lng_text, _, lat_text = location.partition(",")
        lng, lat = lng_text.strip(), lat_text.strip()
    try:
        spellings = [text for text in spellings_of(row) if text]
        return {
            # 名称取各家写法（Google 在 displayName.text、amap 在 name）：
            # 归一化必须把"这个实体叫什么"一起带下去，否则 pick_row 拿不到名字。
            "name": spellings[0] if spellings else str(row.get("name") or "").strip(),
            "spellings": spellings,
            "latitude": float(lat),  # type: ignore[arg-type]
            "longitude": float(lng),  # type: ignore[arg-type]
            "display_name": str(row.get("address") or row.get("formattedAddress") or "") or None,
            "external_id": str(row.get("id") or row.get("place_id") or "") or None,
        }
    except (TypeError, ValueError):
        return None
