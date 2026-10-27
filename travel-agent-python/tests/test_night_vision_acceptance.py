"""Explicit anonymous vision batches and shared-budget image allowlist boundary."""

import base64
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.image_intent import _to_data_uri
from scripts.night_vision_acceptance import VisionBudget, execute, prepare
from scripts.sinan_nl_budget import NIGHT_UPGRADE, NLBudget


def test_prepare_eight_night_images():
    root = os.environ.get("SINAN_NIGHT_VISION_PREPARE")
    if not root:
        pytest.skip("explicit new fixture directory required")
    assert len(prepare(Path(root))) == 8


def test_vision_scope_retains_same_ledger_and_rejects_unlisted_image(tmp_path):
    rows = prepare(tmp_path / "fixtures")
    ledger = NLBudget(tmp_path / "budget.json", approved=True)
    ledger.state.update(cap_cny="30", approval_upgrade=NIGHT_UPGRADE)
    from scripts.run_p4_acceptance import save

    save(ledger.path, ledger.state)
    guard = VisionBudget(ledger.path, tmp_path / "fixtures/manifest.json", approved=True)

    def payload(data):
        return {
            "model": "deepseek-flash",
            "thinking": {"type": "disabled"},
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "extract anonymous fields"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()},
                        },
                    ],
                }
            ],
        }

    with pytest.raises(ValueError, match="frozen"):
        guard.reserve("unlisted", payload(b"private or unapproved bytes"))
    assert not guard.state["records"]
    wire_bytes = base64.b64decode(_to_data_uri(Path(rows[0]["image"]).read_bytes()).split(",", 1)[1])
    guard.reserve("allowed", payload(wire_bytes))
    assert guard.total() == Decimal("2.12992")
    with pytest.raises(ValueError, match="already issued"):
        guard.reserve("allowed", payload(wire_bytes))


def test_two_night_vision_calls(monkeypatch):
    root = os.environ.get("SINAN_NIGHT_VISION_ROOT")
    if not root:
        pytest.skip("explicit paid night batch required")
    from app.common.config import Settings, settings

    monkeypatch.setattr(settings, "llm_api_key", Settings().llm_api_key)
    rows = execute(
        Path(root),
        Path(root).parents[1] / "sinan-nl-01-20261025/budget.json",
        int(os.environ["SINAN_NIGHT_VISION_START"]),
        int(os.environ["SINAN_NIGHT_VISION_STOP"]),
    )
    assert len(rows) == 2 and all(row["provider"].startswith("vision:") for row in rows)


def test_departure_rule_single_regression(monkeypatch):
    root = os.environ.get("SINAN_NIGHT_VISION_ROOT")
    if not root:
        pytest.skip("explicit justified transport regression required")
    from app.common.config import Settings, settings

    monkeypatch.setattr(settings, "llm_api_key", Settings().llm_api_key)
    rows = execute(Path(root), Path(root).parents[1] / "sinan-nl-01-20261025/budget.json", 5, 6, label="departure-rule")
    assert len(rows) == 1 and rows[0]["status"] == "extracted"
