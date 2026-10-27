"""Lock evaluation inputs and runtime implementation before the one confirmation batch."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.run_p4_acceptance import save

REPO = Path(__file__).resolve().parents[2]
FILES = (
    "travel-agent-python/scripts/eval_sinan_nl.py",
    "travel-agent-python/scripts/sinan_nl_check.py",
    "travel-agent-python/scripts/sinan_nl_cases.py",
    "travel-agent-python/scripts/sinan_nl_budget.py",
    "travel-agent-python/scripts/sinan_nl_freeze.py",
    "travel-agent-python/app/agent/editing/clarify.py",
    "travel-agent-python/app/schemas/requirement_patches.py",
    "travel-agent-python/app/agent/generation/orchestration/constraint_planning.py",
    "travel-agent-python/app/agent/generation/content/constraint_trip.py",
    "travel-agent-python/app/agent/generation/content/constraint_trip_solver.py",
    "travel-agent-python/app/agent/generation/content/constraint_compile.py",
    "travel-agent-python/app/agent/generation/content/constraint_requirements.py",
)


def snapshot(root):
    paths = [REPO / path for path in FILES] + [
        root / name for name in ("design.json", "reference.json", "diagnostic-design.json")
    ]
    return {str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def freeze(root):
    path = root / "freeze.json"
    if path.exists():
        raise FileExistsError(path)
    save(
        path,
        {
            "frozen_utc": datetime.now(UTC).isoformat(),
            "model": "deepseek-flash",
            "official_version": "DeepSeek-V4.1-Flash-1017",
            "runtime_and_inputs": snapshot(root),
            "confirmation": 24,
            "diagnostics": 6,
            "single_confirmation": True,
        },
    )


def freeze_diagnostics(root):
    design = json.loads((root / "design.json").read_text("utf8"))
    assert all(
        (root / "results" / f"{row['id']}-{arm}.json").exists()
        for row in design["rows"]
        if row["split"] == "confirm"
        for arm in "ABC"
    )
    path = root / "diagnostic-freeze.json"
    if path.exists():
        raise FileExistsError(path)
    save(
        path,
        {
            "frozen_utc": datetime.now(UTC).isoformat(),
            "runtime_and_inputs": snapshot(root),
            "note": (
                "After all 72 main results: diagnostic provenance labels corrected only; "
                "no NL/facts/numeric/checker retuning. Original main runtime archived."
            ),
        },
    )


def require_frozen(root, *, split="confirm"):
    name = "diagnostic-freeze.json" if split == "diagnostic" else "freeze.json"
    locked = json.loads((root / name).read_text("utf8"))
    if locked["runtime_and_inputs"] != snapshot(root):
        raise ValueError("frozen confirmation implementation/input changed; preserve results, no silent retuning")
