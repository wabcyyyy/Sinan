"""Frozen official queries and unmodified evaluator layout; never pass oracle fields to producers."""

import ast
import csv
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs/benchmarks/sinan-nl-01-20261025"
OUTPUT = ROOT / "docs/benchmarks/sinan-chinatravel-original-20261026"
CODE = SOURCE / "upstream/LAMDA-NeSy-ChinaTravel-f445e00"
ORACLES = {"hard_logic", "hard_logic_py", "hard_logic_nl"}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")


def queries(split="easy"):
    with (SOURCE / f"upstream-data/{split}.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    result = []
    for raw in rows:
        row = dict(raw)
        row["hard_logic_py"] = ast.literal_eval(raw["hard_logic_py"])
        if not isinstance(row["hard_logic_py"], list) or any(type(v) is not str for v in row["hard_logic_py"]):
            raise ValueError(f"invalid official DSL container: {row['uid']}")
        for key in ("days", "people_number"):
            row[key] = int(raw[key])
        for key in ("limit_rooms", "limits_room_type"):
            if raw[key] not in {"True", "False"}:
                raise ValueError("unexpected official boolean")
            row[key] = raw[key] == "True"
        result.append(row)
    if len({r["uid"] for r in result}) != len(result):
        raise ValueError("duplicate official UID")
    return result


def public_query(row):
    # Only the original natural language is needed by the producer; do not expose typed Gold either.
    return {"uid": row["uid"], "nature_language": row["nature_language"]}


def _source_uids(value):
    found = set()
    if isinstance(value, dict):
        if value.get("source_uid"):
            found.add(value["source_uid"])
        for child in value.values():
            found.update(_source_uids(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_source_uids(child))
    return found


def freeze(*, allow_seen_correction=False):
    rows = queries()
    seen = {
        "e20241028160248698752",
        "e20241028160842228543",
        "h20241029143447759844",
        "h20241029143515190249",
        "m20241028164637595271",
        "m20241028164620332052",
        "h20241204223651527057",
    }  # All individually inspected compatibility examples, not human review.
    designs = [SOURCE / "design.json", SOURCE / "repair-20261026/design.json"]
    for path in designs:
        seen.update(_source_uids(json.loads(path.read_text("utf8"))))
    dev, confirm = [], []
    for row in rows:
        # Deterministic UID-only split; no outcome/feasibility screening.
        bucket = int(hashlib.sha256(row["uid"].encode()).hexdigest()[:8], 16) % 10
        (dev if row["uid"] in seen or bucket == 0 else confirm).append(row["uid"])
    manifest = {
        "scope": "official easy300 unchanged; confirmation excludes prior adapted sources and inspected UID",
        "code": json.loads((SOURCE / "source-version.json").read_text("utf8")),
        "data": json.loads((SOURCE / "data-version.json").read_text("utf8")),
        "source_csv_sha256": hashlib.sha256((SOURCE / "upstream-data/easy.csv").read_bytes()).hexdigest(),
        "all": [r["uid"] for r in rows],
        "seen": sorted(seen & {r["uid"] for r in rows}),
        "dev": dev,
        "confirm": confirm,
        "selection": "prior seen OR sha256(uid) first8 modulo10 ==0 => dev; otherwise confirm",
        "formal_run_scope": None,
    }
    path = OUTPUT / "manifest.json"
    if path.exists() and json.loads(path.read_text("utf8")) != manifest:
        old = json.loads(path.read_text("utf8"))
        if (
            not allow_seen_correction
            or (OUTPUT / "protocol.json").exists()
            or old["all"] != manifest["all"]
            or not set(manifest["confirm"]) <= set(old["confirm"])
        ):
            raise ValueError("manifest frozen; only explicit pre-formal contamination removal is permitted")
        preserved = OUTPUT / "manifest-initial-m0.json"
        if not preserved.exists():
            save(preserved, old)
        save(
            OUTPUT / "cohort-correction.json",
            {
                "reason": "previous compatibility examples omitted from seen registry",
                "before_formal_results": True,
                "removed_from_confirmation": sorted(set(old["confirm"]) - set(manifest["confirm"])),
                "old_confirm_count": len(old["confirm"]),
                "new_confirm_count": len(manifest["confirm"]),
            },
        )
    save(path, manifest)
    return manifest


def prepare_evaluator():
    work = OUTPUT / "official"
    if not work.exists():
        shutil.copytree(CODE, work, ignore=shutil.ignore_patterns(".git", "__pycache__", "database"))
    database = work / "chinatravel/environment/database"
    original = SOURCE / "upstream-data/sandbox/database"
    if not database.exists():
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(database), str(original)], check=True, capture_output=True)
        else:
            database.symlink_to(original, target_is_directory=True)
    # Evaluator sources remain byte-identical. Dataset conversion is container-only.
    for relative in (
        "eval_exp.py",
        "chinatravel/evaluation/hard_constraint.py",
        "chinatravel/symbol_verification/commonsense_constraint.py",
    ):
        if (CODE / relative).read_bytes() != (work / relative).read_bytes():
            raise ValueError("official evaluator was modified")
    manifest = freeze()
    for row in queries():
        save(work / f"chinatravel/data/sinan_original/{row['uid']}.json", row)
    for name, ids in (
        ("dev", manifest["dev"]),
        ("confirm", manifest["confirm"]),
        ("all", manifest["all"]),
        ("environment", [manifest["dev"][0]]),
    ):
        path = work / f"chinatravel/evaluation/default_splits/sinan_original_{name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(ids) + "\n", encoding="utf8")
    return work


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--cohort-correction", action="store_true")
    args = parser.parse_args()
    freeze(allow_seen_correction=args.cohort_correction)
    work = prepare_evaluator()
    manifest = freeze()
    print(
        json.dumps(
            {
                "work": str(work),
                "all": len(manifest["all"]),
                "seen": len(manifest["seen"]),
                "dev": len(manifest["dev"]),
                "confirm": len(manifest["confirm"]),
            }
        )
    )
