"""Normalize volatile proof IDs while preserving references between snapshot fields."""

import re
from typing import Any


def normalize_evidence_ids(payload: Any) -> Any:
    ids: dict[str, str] = {}

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: ids.setdefault(child, f"evidence-{len(ids) + 1}")
                if key in ("source_ref", "sourceRef")
                and isinstance(child, str)
                and re.fullmatch(r"[a-f0-9]{32}", child)
                else visit(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [visit(child) for child in value]
        return value

    return visit(payload)
