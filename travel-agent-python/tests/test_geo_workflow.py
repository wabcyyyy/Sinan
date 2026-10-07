from app.agent.core.geo import haversine_meters, nearest_neighbor_order
from app.agent.generation.orchestration.workflow import needs_fix
from app.agent.generation.rules.transfer_time import parse_time
from app.agent.research.agent_state import UnifiedAgentState


def test_haversine_known_distance():
    d = haversine_meters(39.9042, 116.4074, 31.2304, 121.4737)
    assert 1060000 < d < 1080000


def test_haversine_same_point():
    assert haversine_meters(39.9, 116.4, 39.9, 116.4) == 0.0


def test_nearest_neighbor_orders_by_proximity():
    pois = [
        {"name": "起点", "latitude": 39.9, "longitude": 116.4},
        {"name": "远点", "latitude": 40.9, "longitude": 117.4},
        {"name": "近点", "latitude": 39.905, "longitude": 116.405},
    ]
    ordered = nearest_neighbor_order(pois)
    names = [p["name"] for p in ordered]
    assert names[0] == "起点"
    assert names[1] == "近点"
    assert names[2] == "远点"


def test_nearest_neighbor_skips_missing_coords():
    pois = [
        {"name": "A", "latitude": 39.9, "longitude": 116.4},
        {"name": "B", "latitude": None, "longitude": None},
        {"name": "C", "latitude": 39.91, "longitude": 116.41},
    ]
    ordered = nearest_neighbor_order(pois)
    assert len(ordered) == 2
    assert all("latitude" in p for p in ordered)


def test_nearest_neighbor_empty():
    assert nearest_neighbor_order([]) == []
    assert nearest_neighbor_order([{"name": "x"}]) == []


def test_workflow_route_needs_fix_logic():
    # PR-2 后节点只收 UnifiedAgentState（属性访问）：直调点同步改构造模型
    assert needs_fix(UnifiedAgentState(validation_issues=["x"], fix_count=1)) == "fix"
    assert needs_fix(UnifiedAgentState(validation_issues=["x"], fix_count=3)) == "pass"
    assert needs_fix(UnifiedAgentState(error="boom", attempts=1)) == "fix"
    assert needs_fix(UnifiedAgentState(error="boom", attempts=3)) == "pass"
    assert needs_fix(UnifiedAgentState(error=None, validation_issues=[])) == "pass"


def test_parse_time_roundtrip():
    assert parse_time("08:30") == 8 * 60 + 30
    assert parse_time("23:59") == 1439
