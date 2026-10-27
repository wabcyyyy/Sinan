"""Explicitly authorized repair batch driver; all costs go to the original ledger."""

import os
from pathlib import Path

import pytest

from scripts.eval_nl_repair import execute, prepare


def test_prepare_nl_repair():
    root = os.environ.get("SINAN_NL_REPAIR_PREPARE")
    if not root:
        pytest.skip("explicit output root required")
    result = prepare(Path(root))
    assert result == {"new_main": 12, "new_diagnostic": 4, "seen_regression": 18}


def test_authorized_nl_repair_batch(monkeypatch):
    root = os.environ.get("SINAN_NL_REPAIR_ROOT")
    if not root:
        pytest.skip("explicit experiment scope required")
    from app.common.config import Settings, settings

    assert os.environ.get("SINAN_NL_APPROVED") == "SINAN-NL-01"
    monkeypatch.setattr(settings, "llm_api_key", Settings().llm_api_key)
    execute(
        Path(root),
        os.environ["SINAN_NL_SPLIT"],
        int(os.environ["SINAN_NL_START"]),
        int(os.environ["SINAN_NL_STOP"]),
        approved=True,
    )
