"""Explicit bounded policy batches plus the actual conditional-infeasibility safety boundary."""

import os
from pathlib import Path

import pytest

from scripts.eval_night_evidence import execute, prepare


def test_prepare_night_evidence():
    root = os.environ.get("SINAN_NIGHT_EVIDENCE_PREPARE")
    if not root:
        pytest.skip("explicit task output required")
    assert prepare(Path(root)) == 18


def test_night_evidence_batch():
    root = os.environ.get("SINAN_NIGHT_EVIDENCE_ROOT")
    if not root:
        pytest.skip("explicit policy experiment output required")
    rows = execute(
        Path(root),
        os.environ["SINAN_NIGHT_EVIDENCE_SPLIT"],
        int(os.environ["SINAN_NIGHT_EVIDENCE_START"]),
        int(os.environ["SINAN_NIGHT_EVIDENCE_STOP"]),
    )
    assert len(rows) % 3 == 0
    assert all(not row["false_feasible"] for row in rows)
    assert all(row["actual_tool_calls"] == row["report"]["retrieval_calls"] <= 6 for row in rows)
