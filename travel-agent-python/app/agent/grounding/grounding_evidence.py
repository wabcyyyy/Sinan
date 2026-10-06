"""证据签发：外部数据层每产出一行真实点位，就发一张"本服务查到过"的票（PLAN-A1 G1）。

为什么需要它：`source="opentripmap"` **只是一个字符串**。/v1/generate-day 的 context
由调用方传入，任何拿到内部令牌的一方都能自称 opentripmap 并附上一对编造的坐标，
让幻觉点位拿到 observed 徽章——那时徽章的含义是"谁写得响"，不是"谁查过"。

本模块把背书判定从"来源字符串在值域内"升级为"能对上本服务签发的一张票"：

- **签发**：外部数据层在拿到数据的当场签（`tools._as_candidate` 覆盖 OTM/Nominatim/
  联网补池三条池路径；`existence.resolve_poi` 覆盖单名解析）；
- **消费**：`grounding_labels.is_trusted_row` 查票；无票的行既不给字段、也不给背书，
  来源改写为 client-context；坐标与票对不上的行同样不采信（防"真 xid + 假坐标"）。

存储走 `cache_store`（进程内有界表 + Redis 双写），多 worker 部署下也能对上。
**降级方向是不背书**：Redis 不可用时票只活在签发它的 worker 内存里，跨 worker 的
后续请求按"无票"处理——宁可少给徽章，不可乱给徽章。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from app.agent.core.poi_identity import haversine_m, norm_poi_key
from app.common import cache_store

NAMESPACE = "grounding_evidence"
# 票的有效期与外部数据层缓存同量级：数据源本身 6-24h 刷新一次。
TICKET_TTL_SECONDS = 24 * 3600
# 票与行的坐标容差：同一实体在不同源的坐标常差几十米（OSM 质心 vs 门址）。
COORD_TOLERANCE_M = 2000.0


@dataclass(frozen=True)
class EvidenceTicket:
    """一次真实外部取数的存证。"""

    provider: str
    external_id: str
    name: str
    latitude: float | None
    longitude: float | None
    city: str


def _keys(name: str, city: str, external_id: str) -> list[str]:
    keys = []
    if external_id:
        keys.append(f"xid:{external_id}")
    normalized = norm_poi_key(name)
    if normalized:
        keys.append(f"name:{str(city or '').strip()}|{normalized}")
    return keys


def issue_evidence(row: Mapping[str, Any]) -> EvidenceTicket | None:
    """把一行外部产出登记成本服务签发的证据票；无名/无 provider 不签。"""
    name = str(row.get("name") or row.get("poi_name") or "").strip()
    provider = str(row.get("source") or "").strip()
    if not name or not provider:
        return None
    city = str(row.get("city") or "").strip()
    external_id = str(row.get("xid") or row.get("id") or row.get("external_id") or "").strip()
    ticket = EvidenceTicket(
        provider=provider,
        external_id=external_id,
        name=name,
        latitude=_as_float(row.get("latitude")),
        longitude=_as_float(row.get("longitude")),
        city=city,
    )
    payload = asdict(ticket)
    for key in _keys(name, city, external_id):
        cache_store.set_json(NAMESPACE, key, payload, TICKET_TTL_SECONDS)
    return ticket


def lookup_ticket(name: str, city: str = "", external_id: str = "") -> EvidenceTicket | None:
    for key in _keys(name, city, external_id):
        raw = cache_store.get_json(NAMESPACE, key)
        if isinstance(raw, dict):
            try:
                return EvidenceTicket(
                    provider=str(raw.get("provider") or ""),
                    external_id=str(raw.get("external_id") or ""),
                    name=str(raw.get("name") or ""),
                    latitude=_as_float(raw.get("latitude")),
                    longitude=_as_float(raw.get("longitude")),
                    city=str(raw.get("city") or ""),
                )
            except (TypeError, ValueError):
                continue
    return None


def verify_evidence(row: Mapping[str, Any]) -> bool:
    """这行是不是本服务真查到过的数据（含坐标一致性校验）。"""
    name = str(row.get("name") or row.get("poi_name") or "").strip()
    if not name:
        return False
    external_id = str(row.get("xid") or row.get("external_id") or row.get("poi_id") or "").strip()
    city = str(row.get("city") or "").strip()
    ticket = lookup_ticket(name, city, external_id)
    if ticket is None:
        return False
    lat, lng = _as_float(row.get("latitude")), _as_float(row.get("longitude"))
    if lat is None or lng is None:
        # 行本身不带位置：票只能证明"这个名字查到过"（联网补池的真实店名）
        return True
    if ticket.latitude is None or ticket.longitude is None:
        return True
    return haversine_m(lat, lng, ticket.latitude, ticket.longitude) <= COORD_TOLERANCE_M


def _as_float(value: Any) -> float | None:
    try:
        converted = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return converted if abs(converted) > 1e-6 else None
