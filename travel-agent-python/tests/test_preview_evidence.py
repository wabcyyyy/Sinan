"""A name ticket cannot endorse fields added or changed by a caller."""

from typing import cast

import pytest

from app.agent.generation.output.facts import apply_item_facts
from app.agent.generation.rules.generation_core import PoiFactRow
from app.agent.grounding.grounding_evidence import evidence_row, issue_evidence
from app.agent.grounding.grounding_labels import label_for_evidence_row
from app.agent.grounding.official_pages import extract_evidence
from app.common import cache_store


@pytest.mark.parametrize(
    "changed",
    [
        {"latitude": 30, "longitude": 120},
        {"ticket_price": 0},
        {"open_time": "24 hours"},
        {"source": "opentripmap"},
        {"category": "hotel"},
        {"dietary_options": ["vegan"]},
    ],
)
def test_name_only_ticket_rejects_added_fields(changed):
    row = {"name": "博物馆", "source": "web.search", "city": "杭州"}
    issue_evidence(row)
    assert evidence_row(row) is not None
    changed_row = {**row, **changed}
    assert evidence_row(changed_row) is None
    item = {"poi_name": row["name"]}
    apply_item_facts(item, cast(PoiFactRow, changed_row), {})
    assert item["verification_status"] == "unverified"
    assert "latitude" not in item and "cost" not in item


def test_storage_unavailable_or_expired_fails_closed(monkeypatch):
    row = {"name": "博物馆", "source": "opentripmap", "latitude": 30, "longitude": 120}
    issue_evidence(row)
    monkeypatch.setattr(cache_store, "get_json", lambda *_args: None)
    assert not label_for_evidence_row(row).endorsed
    assert evidence_row({"name": row["name"], "source": row["source"]}) is None


def test_signed_diet_category_and_alias_survive_json_cache():
    row = {
        "name": "餐厅本店",
        "source": "nominatim",
        "aliases": ["Restaurant"],
        "dietary_options": ("vegetarian",),
        "category_tags": {"amenity": "restaurant"},
        "latitude": 30,
        "longitude": 120,
    }
    issue_evidence(row)
    snapshot = evidence_row({**row, "name": "Restaurant"})
    assert snapshot is not None
    assert snapshot["dietary_options"] == ["vegetarian"]
    assert snapshot["category_tags"] == {"amenity": "restaurant"}
    assert evidence_row({**row, "aliases": ["Other"]}) is None


def test_delivery_check_rejects_unsigned_diet_claim():
    from app.agent.generation.output.facts import field_fact_evidence
    from tests.agent_eval.delivery_metrics import assess_delivery

    row = {"name": "餐厅", "source": "overpass", "dietary_options": ["vegetarian"], "latitude": 30, "longitude": 120}
    issue_evidence(row)
    item = {"poi_name": row["name"], "latitude": 30, "longitude": 120}
    facts = field_fact_evidence(
        item, label_for_evidence_row(row), cost_observed=False, open_time_observed=False, evidence_id=row["evidence_id"]
    )
    wire = {key: value.model_dump(by_alias=True) for key, value in facts.items()}
    detail = {
        "dayList": [
            {
                "dayNo": 1,
                "items": [
                    {"itemType": "food", "poiName": row["name"], "latitude": 30, "longitude": 120, "factEvidence": wire}
                ],
            }
        ]
    }
    assert assess_delivery(detail, None, 0, [])["evidence_overreach"] == 0
    wire["diet:vegan"] = wire["diet:vegetarian"]
    assert assess_delivery(detail, None, 0, [])["evidence_overreach"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "自2026年1月1日起，示例博物馆成人门票40元，儿童免费参观。",
        "自2026年1月1日起，示例博物馆限时免费开放。",
        "自2020年1月1日起，示例博物馆历史门票40元。",
        "自2026年1月1日起，示例博物馆无需预约参观。",
        "自2026年1月1日起，示例博物馆门票40元，门票项目80元。",
        "自2027年1月1日起，示例博物馆免费开放。",
        "示例博物馆免费开放。",
    ],
)
def test_conditional_or_unknown_date_is_not_a_universal_fact(text):
    html = f"<title>示例博物馆公告</title><article><p>发布时间：2026-01-01</p><p>{text}</p></article>"
    assert extract_evidence(html, fetched_at="2026-10-11T00:00:00+00:00") is None
