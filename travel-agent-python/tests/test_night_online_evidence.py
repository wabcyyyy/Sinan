"""Explicit source observations and narrow parser/date/identity guards."""

import os
from pathlib import Path

import pytest

from scripts.night_online_evidence import closure, execute


def test_named_closure_keeps_date_and_rejects_other_entity():
    text = "Tokyo National Museum December 15, 2026: The museum will be closed for facility inspection and maintenance."
    fact = closure("tokyo", text, "2026-10-26T00:00:00+08:00")
    assert fact is not None and fact.opening_hours is not None
    assert str(fact.opening_hours.valid_from) == str(fact.opening_hours.valid_to) == "2026-12-15"
    assert closure("tokyo", text.replace("Tokyo National Museum", "Other branch"), "2026-10-26T00:00:00+08:00") is None


def test_two_destinations_current_free_source():
    root = os.environ.get("SINAN_NIGHT_SOURCE_ROOT")
    key = os.environ.get("SINAN_NIGHT_SOURCE_KEY")
    if not root or not key:
        pytest.skip("explicit free online source batch required")
    result = execute(Path(root), key)
    assert result["before"][0]["constraint_planning"]["status"] == "UNKNOWN"
    assert result["after"][0]["constraint_planning"]["status"] == "INFEASIBLE"
    active = result["after"][0]["constraint_planning"]["active_evidence"]
    assert active["retrieval_calls"] == 1 and active["queries"][0]["outcome"] == "updated"
