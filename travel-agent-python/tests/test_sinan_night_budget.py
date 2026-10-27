import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from scripts import sinan_night_budget
from scripts.p5_scenarios import semantic_errors
from scripts.sinan_night_budget import CARRIED, NightBudget


@pytest.fixture(autouse=True)
def future_test_deadline(monkeypatch):
    monkeypatch.setattr(sinan_night_budget, "DEADLINE", datetime.now(UTC) + timedelta(days=1))


def payload():
    return {"model": "deepseek-flash", "thinking": {"type": "disabled"}, "max_tokens": 192}


def test_cumulative_known_usage_settles_but_unknown_usage_and_duplicate_ids_remain_reserved(tmp_path):
    ledger = NightBudget(tmp_path / "night.json")
    ledger.reserve("one", payload())
    reserved = ledger.total()
    assert reserved > CARRIED + 2
    ledger.settle("one", {"usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    assert ledger.total() == CARRIED + Decimal("0.00036")
    resumed = NightBudget(ledger.path)
    with pytest.raises(ValueError, match="重试"):
        resumed.reserve("one", payload())
    resumed.reserve("timeout", payload())
    resumed.settle("timeout", {})
    assert resumed.total() > CARRIED + 2


def test_thirty_yuan_cap_cannot_be_reset_or_exceeded(tmp_path):
    ledger = NightBudget(tmp_path / "night.json")
    for index in range(14):
        ledger.reserve(str(index), payload())
    before = copy.deepcopy(ledger.state)
    with pytest.raises(ValueError, match="超过30"):
        ledger.reserve("overflow", payload())
    assert ledger.state == before
    assert NightBudget(ledger.path).total() == ledger.total() < 30


def test_independent_simulator_check_preserves_negation_and_numeric_goal():
    event = {"kind": "delay", "target": "地点1", "delay_minutes": 20}
    assert not semantic_errors("请把地点1往后挪20分钟", event)
    assert semantic_errors("把地点1推迟30分钟", event)
    assert semantic_errors("把地点2推迟20分钟", event)
    assert not semantic_errors("不要取消地点1", {"kind": "negative_cancel", "target": "地点1"})


def test_expired_deadline_does_not_issue_or_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(sinan_night_budget, "DEADLINE", datetime.now(UTC) - timedelta(seconds=1))
    ledger = NightBudget(tmp_path / "expired.json")
    with pytest.raises(TimeoutError, match="07:00"):
        ledger.reserve("late", payload())
    assert ledger.total() == CARRIED and not ledger.path.exists()
