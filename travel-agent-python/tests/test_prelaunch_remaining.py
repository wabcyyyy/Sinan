"""上线前剩余问题：预算读写一致性、权限与日级版本差异。"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.common import cache_store
from app.common.envelope import ApiError
from app.common.task_pool import TaskRejected
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain, ItineraryMember
from app.services import budget_engine, itinerary_chat, itinerary_generation, itinerary_query, itinerary_version


@pytest.fixture
def trip_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'remaining.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    cache_store.reset_for_tests()
    with db_session.session_scope() as session:
        session.add(
            ItineraryMain(id=1, user_id=1, title="杭州2日游", city="杭州", days=2, persons=1, budget=500, status=2)
        )
        session.add(ItineraryMember(itinerary_id=1, user_id=2, role="viewer"))
        session.add_all(
            [
                ItineraryDay(id=1, itinerary_id=1, day_no=1, generation_status="SUCCEEDED"),
                ItineraryDay(id=2, itinerary_id=1, day_no=2, generation_status="SUCCEEDED"),
            ]
        )
        for sort, (kind, cost) in enumerate([("attraction", 100), ("food", 170), ("hotel", 300)]):
            session.add(
                ItineraryItem(
                    itinerary_id=1,
                    day_id=1,
                    item_type=kind,
                    poi_name=kind,
                    cost=cost,
                    sort_no=sort,
                    review_requirement="none",
                    freshness_status="fresh",
                )
            )
        session.add(
            ItineraryItem(
                itinerary_id=1,
                day_id=1,
                item_type="hotel",
                poi_name="软删旧酒店",
                cost=10000,
                deleted=1,
            )
        )
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()
    engine.dispose()


def test_budget_recompute_evicts_all_readers_and_excludes_deleted(trip_db):
    assert itinerary_query.detail(1, 1)["totalAmount"] == 0
    assert itinerary_query.detail(2, 1)["totalAmount"] == 0
    budgets = budget_engine.recalculate(1)
    assert sum(row.amount for row in budgets) == Decimal("640.00")
    for user_id in (1, 2):
        data = itinerary_query.detail(user_id, 1)
        assert data["totalAmount"] == 640
        assert data["qualityStatus"] == "READY_WITH_WARNINGS"
        warnings = data["qualityReport"]["warnings"]
        assert [row["code"] for row in warnings] == ["BUDGET_EXCEEDED"]
        assert all(amount in warnings[0]["message"] for amount in ("640.00", "500.00", "140.00"))
        assert len(data["dayList"][0]["items"]) == 3


@pytest.mark.parametrize("budget", [None, 0, 640, 1000])
def test_unset_or_sufficient_budget_has_no_overrun(trip_db, budget):
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, 1)
        assert main is not None
        main.budget = budget
    budget_engine.recalculate(1)
    data = itinerary_query.detail(1, 1)
    assert data["qualityStatus"] == "READY"
    assert data["qualityReport"]["warnings"] == []


def test_budget_warning_survives_draft_status(trip_db):
    with db_session.session_scope() as session:
        day = session.get(ItineraryDay, 2)
        assert day is not None
        day.generation_status = "PENDING"
    budget_engine.recalculate(1)
    data = itinerary_query.detail(1, 1)
    assert data["qualityStatus"] == "DRAFT"
    assert data["qualityReport"]["warnings"][0]["code"] == "BUDGET_EXCEEDED"


def test_final_delivery_has_budget_even_if_enrichment_queue_is_full(trip_db, monkeypatch):
    def reject(*_args):
        raise TaskRejected("fixture queue full")

    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", reject)
    command = itinerary_generation.GenerateCommand(city="杭州", days=2, persons=1, stay_nights=1)
    itinerary_generation._finish(1, 1, command)
    detail = itinerary_query.detail(1, 1)
    assert detail["status"] == 2 and detail["totalAmount"] == 640
    assert detail["qualityReport"]["warnings"][0]["code"] == "BUDGET_EXCEEDED"


@pytest.mark.parametrize("user_id,itinerary_id", [(3, 1), (1, 999)])
def test_chat_history_obeys_same_read_gate(trip_db, user_id, itinerary_id):
    with pytest.raises(ApiError) as raised:
        itinerary_chat.chat_history(user_id, itinerary_id)
    assert raised.value.status == 404


def test_readers_can_read_their_own_empty_history(trip_db):
    assert itinerary_chat.chat_history(1, 1) == []
    assert itinerary_chat.chat_history(2, 1) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("theme", "城南慢游"),
        ("note", "早点出发"),
        ("travelDate", "2026-10-08"),
        ("miniRoute", {"stops": ["西湖"]}),
        ("photoSpots", [{"name": "断桥"}]),
        ("practicalNotes", ["提前预约"]),
        ("backupPlan", [{"if": "雨天", "action": "博物馆"}]),
        ("dayOptions", [{"label": "慢游", "summary": "湖边", "tradeoff": "少走路"}]),
    ],
)
def test_day_content_changes_appear_in_diff(field, value):
    changes = []
    itinerary_version._diff_index(
        changes,
        itinerary_version._day_index([{"dayNo": 1}]),
        itinerary_version._day_index([{"dayNo": 1, field: value}]),
    )
    assert len(changes) == 1
    assert changes[0]["type"] == "updated" and changes[0]["key"] == "day:1"
    assert changes[0]["before"][field] is None
    assert changes[0]["after"][field] == value


def test_diff_ignores_day_ids_and_generation_status():
    first = itinerary_version._day_index([{"dayNo": 1, "dayId": 4, "generationStatus": "RUNNING"}])
    second = itinerary_version._day_index([{"dayNo": 1, "dayId": 7, "generationStatus": "SUCCEEDED"}])
    assert first == second


def test_day_addition_removal_and_top_fields_in_snapshot_diff(trip_db):
    first = itinerary_version.create_snapshot(1, 1, "snapshot", "之前")
    with db_session.session_scope() as session:
        day = session.get(ItineraryDay, 1)
        assert day is not None
        day.metadata_json = '{"theme":"城南慢游"}'
        day.travel_date = date(2026, 10, 8)
        main = session.get(ItineraryMain, 1)
        assert main is not None
        main.title = "新标题"
    second = itinerary_version.create_snapshot(1, 1, "snapshot", "之后")
    result = itinerary_version.diff(1, 1, first["id"], second["id"])
    assert {row["key"] for row in result["changes"]} == {"title", "day:1"}
    changed_day = next(row for row in result["changes"] if row["key"] == "day:1")
    assert changed_day["after"]["theme"] == "城南慢游"
    changes = []
    itinerary_version._diff_index(changes, {"day:1": {}}, {"day:2": {}})
    assert [(row["type"], row["key"]) for row in changes] == [("removed", "day:1"), ("added", "day:2")]
