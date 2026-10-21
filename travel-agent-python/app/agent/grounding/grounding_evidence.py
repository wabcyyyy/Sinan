"""Opaque field snapshots issued only by server-side data adapters.

Names and external identifiers are not credentials. A ticket binds an entity,
provider and the exact original values; unavailable or expired storage fails closed.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any

from app.common import cache_store

NAMESPACE = "grounding_evidence_v2"
TICKET_TTL_SECONDS = 24 * 3600
_FIELDS = (
    "name",
    "source",
    "category",
    "city",
    "id",
    "external_id",
    "xid",
    "latitude",
    "longitude",
    "ticket_price",
    "avg_cost",
    "open_time",
    "opening_hours",
    "duration_min",
    "address",
    "source_updated_at",
    "source_fetched_at",
    "dietary_options",
    "category_tags",
    "source_url",
    "aliases",
)


@dataclass(frozen=True)
class EvidenceTicket:
    evidence_id: str


def issue_evidence(row: MutableMapping[str, Any]) -> EvidenceTicket | None:
    name = str(row.get("name") or row.get("poi_name") or "").strip()
    if not name or not row.get("source"):
        return None
    evidence_id = uuid.uuid4().hex
    snapshot = {key: _snapshot_value(row.get(key)) for key in _FIELDS}
    snapshot["name"] = name
    cache_store.set_json(NAMESPACE, evidence_id, snapshot, TICKET_TTL_SECONDS)
    row["evidence_id"] = evidence_id
    return EvidenceTicket(evidence_id)


def evidence_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    evidence_id = str(row.get("evidence_id") or "")
    if not evidence_id:
        return None
    snapshot = cache_store.get_json(NAMESPACE, evidence_id)
    if not isinstance(snapshot, dict):
        return None
    name = str(row.get("name") or row.get("poi_name") or "").strip()
    if name not in (snapshot.get("name"), *(snapshot.get("aliases") or ())) or row.get("source") != snapshot.get(
        "source"
    ):
        return None
    for key in _FIELDS:
        if key != "name" and key in row and _snapshot_value(row[key]) != snapshot.get(key):
            return None
    return {**snapshot, "evidence_id": evidence_id}


def verify_evidence(row: Mapping[str, Any]) -> bool:
    return evidence_row(row) is not None


def _snapshot_value(value: Any) -> Any:
    """JSON 缓存往返保持序列/字典口径，签发和校验用同一个规范化入口。"""
    if isinstance(value, (tuple, list)):
        return [_snapshot_value(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _snapshot_value(item) for key, item in value.items()}
    return value
