"""C1.3 gaps: HTTP addon gate and optimize -> restore with another populated day.

Reuse the existing SQLite route fixture; Redis and network access are disabled.
Locks are deliberately not asserted: this route has no lock contract (unlike local_replan).
"""

from datetime import time

import pytest
from _optimize_support import _day_ids, _trip_id
from sqlalchemy import select

from app.agent.data import route_service
from app.common import addons as addons_module
from app.common import cache_store
from app.common.config import settings
from app.db import session as db_session
from app.db.models import ItineraryItem, ItineraryVersion
from app.services import itinerary_command

# 共享夹具挂载在非测试模块 `_optimize_support`（见其 docstring：为何不经
# test_* 模块借用——邻居先收集时夹具会注册失败，顺序脆弱）。
pytest_plugins = ["_optimize_support"]


@pytest.fixture(autouse=True)
def offline_dependencies(monkeypatch):
    import socket

    def no_network(*args, **kwargs):
        pytest.fail("C1.3 acceptance tests must not access network services")

    # Leave asyncio's Windows socketpair intact; block outbound client connections.
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(cache_store, "_get_client", lambda: None)
    monkeypatch.setattr(addons_module, "_cache", {})
    monkeypatch.setattr(addons_module, "_TABLE_READY", False)
    monkeypatch.setattr(settings, "schedule_optimizer_enabled", True)
    monkeypatch.setattr(settings, "route_mode", "walking")
    monkeypatch.setattr(route_service, "_route_cache", {})


def test_disabled_optimize_http_is_hidden_without_writes(client, monkeypatch):
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    before = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    monkeypatch.setattr(settings, "schedule_optimizer_enabled", False)

    def must_not_run(*args, **kwargs):
        pytest.fail("Disabled addon must be rejected before command dispatch")

    monkeypatch.setattr(itinerary_command, "optimize_day", must_not_run)
    response = client.post(f"/api/itinerary/{trip_id}/optimize", json={"dayId": day1})
    assert response.status_code == 404
    assert response.json()["code"] == 404
    assert client.get(f"/api/itinerary/{trip_id}").json()["data"] == before
    assert client.get(f"/api/itinerary/{trip_id}/versions").json()["data"] == []


def test_optimize_preserves_other_day_and_before_snapshot_restores_order(client):
    trip_id = _trip_id(client)
    day1, day2 = _day_ids(trip_id)
    with db_session.session_scope() as session:
        rows = session.scalars(select(ItineraryItem).where(ItineraryItem.day_id == day1)).all()
        for row in rows:
            row.start_time = time(9 + row.sort_no)
            row.end_time = time(10 + row.sort_no)
            row.duration_min = 60
        session.add(ItineraryItem(day_id=day2, itinerary_id=trip_id, item_type="food", poi_name="次日餐厅", sort_no=0))
    before = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    response = client.post(f"/api/itinerary/{trip_id}/optimize", json={"dayId": day1})
    assert response.status_code == 200
    after = response.json()["data"]
    assert [item["poiName"] for item in after["dayList"][0]["items"]] == ["甲点", "乙点", "丙点"]
    assert after["dayList"][1] == before["dayList"][1]
    # The endpoint changes sortNo only, not item identity, day membership or times.
    original = {item["id"]: item for item in before["dayList"][0]["items"]}
    assert {item["id"] for item in after["dayList"][0]["items"]} == set(original)
    for item in after["dayList"][0]["items"]:
        assert {**item, "sortNo": original[item["id"]]["sortNo"]} == original[item["id"]]

    with db_session.session_scope() as session:
        versions = session.scalars(select(ItineraryVersion).order_by(ItineraryVersion.version_no)).all()
        assert [version.operation for version in versions] == ["optimize", "optimize"]
        assert versions[1].parent_version_id == versions[0].id
        before_version_id = versions[0].id

    response = client.post(f"/api/itinerary/{trip_id}/versions/{before_version_id}/restore")
    assert response.status_code == 200
    restored = response.json()["data"]
    # Restore rebuilds rows, so compare content rather than database-generated IDs.
    for old_day, restored_day in zip(before["dayList"], restored["dayList"], strict=True):
        assert restored_day["dayNo"] == old_day["dayNo"]
        assert [item["poiName"] for item in restored_day["items"]] == [item["poiName"] for item in old_day["items"]]
        assert [item["startTime"] for item in restored_day["items"]] == [item["startTime"] for item in old_day["items"]]
    versions = client.get(f"/api/itinerary/{trip_id}/versions").json()["data"]
    assert [version["operation"] for version in versions] == ["restore", "optimize", "optimize"]
