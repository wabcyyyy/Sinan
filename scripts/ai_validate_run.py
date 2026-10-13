"""Serial validation execution with input-bound receipts and failure propagation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ai_validate_quick import cache_key, cached_receipt, run_bounded


def run_validation(
    ROOT: Path,
    CFG: dict[str, Any],
    result: dict[str, Any],
    args: argparse.Namespace,
    files: list[str],
    git: Callable[..., str],
    paths: Callable[[], list[str]],
    fingerprint: Callable[[list[str]], str],
) -> int:
    output = (ROOT / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock_dir = ROOT / ".validation"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = lock_dir / "running.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit("Validation already running (or stale lock); inspect PID before removing lock.") from None
    os.write(descriptor, str(os.getpid()).encode())
    os.close(descriptor)
    receipt = dict(
        result,
        head=git("rev-parse", "HEAD").strip(),
        input_before=fingerprint(files),
        interpreter=sys.version,
        executable=sys.executable,
        steps=[],
        status="running",
    )
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    budget = (
        (getattr(args, "budget", None) or CFG.get("quick_budget_seconds", 90)) if result.get("quick_mode") else None
    )
    deadline = time.monotonic() + budget if budget else None
    try:
        temp = Path(args.temp_root).resolve() / CFG["project"] / run_id
        temp.mkdir(parents=True, exist_ok=False)
        env = dict(
            os.environ,
            TEMP=str(temp),
            TMP=str(temp),
            TMPDIR=str(temp),
            PYTHONIOENCODING="utf-8",
        )
        receipt["environment_sha256"] = hashlib.sha256(json.dumps(sorted(env.items())).encode()).hexdigest()
        receipt["temp"] = str(temp)
        interpreter = Path(
            CFG.get("python_executable")
            or ROOT / CFG["python_cwd"] / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
        )
        identity = subprocess.run(
            [
                str(interpreter),
                "-c",
                (
                    "import hashlib,importlib.metadata as m,json,sys; "
                    "p=sorted((d.metadata['Name'],d.version) for d in m.distributions()); "
                    "print(json.dumps({'python':sys.version,"
                    "'packages_sha256':hashlib.sha256(json.dumps(p).encode()).hexdigest()}))"
                ),
            ],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        receipt["project_environment"] = json.loads(identity.stdout)
        receipt["project_environment"]["git_head"] = receipt["head"]
        receipt["project_environment"]["temp_root"] = str(Path(args.temp_root).resolve())
        receipt["cache_key"] = cache_key(
            ROOT, CFG, result, receipt["input_before"], receipt["project_environment"], env
        )
        cached = (
            cached_receipt(output, receipt["cache_key"], CFG.get("cache_max_age_seconds", 86400))
            if result.get("quick_mode") and not getattr(args, "no_cache", False) and not result.get("manual_required")
            else None
        )
        if cached:
            receipt["status"] = "passed"
            receipt["reused_from"] = cached["receipt_file"]
            print("Reused matching successful receipt; full regression remains in CI.", flush=True)
        for index, command in enumerate([] if cached else result["commands"]):
            cwd = ROOT / command["cwd"]
            argv = [part.replace("$PY", str(interpreter)).replace("$TEMP", str(temp)) for part in command["argv"]]
            if os.name == "nt" and argv[0] in {"npm", "npx", "uv"}:
                argv[0] = shutil.which(argv[0] + (".cmd" if argv[0] != "uv" else ".exe")) or argv[0]
            started = time.monotonic()
            log = output / f"{run_id}-{index}.log"
            remaining = min(args.timeout, deadline - time.monotonic()) if deadline else args.timeout
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, budget)
            with log.open("wb") as stream:
                code = run_bounded(argv, cwd, env, stream, remaining)
            receipt["steps"].append(
                {
                    "command": argv,
                    "cwd": str(cwd),
                    "exit_code": code,
                    "seconds": round(time.monotonic() - started, 3),
                    "log": str(log),
                    "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
                }
            )
            print(f"  step {index + 1}: exit {code}, {receipt['steps'][-1]['seconds']}s", flush=True)
            if code:
                receipt["status"] = "failed"
                break
        else:
            receipt["status"] = "manual_required" if result["manual_required"] else "passed"
        receipt["input_after"] = fingerprint(paths())
        if receipt["input_after"] != receipt["input_before"]:
            receipt["status"] = "inputs_changed"
    except subprocess.TimeoutExpired as exc:
        receipt["status"], receipt["error"] = "budget_exhausted", str(exc)
        receipt["deferred_commands"] = result["commands"][len(receipt["steps"]) :]
        print("Quick-check time budget reached. Incomplete; continue development and leave remaining regression to CI.")
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        receipt["status"], receipt["error"] = "incomplete", str(exc)
    finally:
        (output / f"{run_id}.json").write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
        lock.unlink()
    print("Receipt:", output / f"{run_id}.json", "status:", receipt["status"])
    return 0 if receipt["status"] == "passed" else 1
