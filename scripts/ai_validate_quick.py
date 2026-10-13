"""Small default test selection, conservative receipt reuse, bounded processes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


def select_quick(
    cfg: dict[str, Any],
    graph: dict[str, set[str]],
    selected: set[str],
    changed: list[str],
    requested: list[str],
    limit: int,
) -> tuple[set[str], list[str]]:
    """Keep explicitly changed/requested tests; rank others by import distance."""
    mandatory = {s.split("::")[0] for s in requested}
    mandatory |= {s for s in changed if s in selected}
    distance = dict.fromkeys(changed, 0)
    while True:
        more = {
            name: min(distance[d] for d in deps if d in distance) + 1
            for name, deps in graph.items()
            if name not in distance and deps & distance.keys()
        }
        if not more:
            break
        distance.update(more)
    words = set(re.findall(r"[a-z0-9]+", " ".join(changed).lower()))
    by_file = {s.split("::")[0] for s in selected}

    def rank(name: str) -> tuple[int, int, int, str]:
        tokens = set(re.findall(r"[a-z0-9]+", Path(name).stem.lower())) - {"test"}
        smoke = any(word in name for word in cfg.get("quick_critical_names", []))
        return (
            0 if name in mandatory else 1,
            0 if smoke else 1,
            distance.get(name, 100) * 10 - len(tokens & words),
            name,
        )

    keep_files = set(sorted(by_file, key=rank)[: max(limit, len(mandatory))]) | mandatory
    keep = {s for s in selected if s.split("::")[0] in keep_files}
    # A whole-file selection already covers explicitly named nodes within it.
    keep = {s for s in keep if "::" not in s or s.split("::")[0] not in keep}
    return keep, sorted(selected - keep - {s for s in selected if "::" in s and s.split("::")[0] in keep})


def cache_key(
    root: Path,
    cfg: dict[str, Any],
    result: dict[str, Any],
    input_hash: str,
    identity: dict[str, Any],
    env: dict[str, str],
) -> str:
    files = [root / ".env", root / cfg["python_cwd"] / ".env"]
    if cfg.get("frontend_related"):
        files.append(root / cfg["frontend"] / "node_modules/.package-lock.json")
        identity = dict(identity, node=subprocess.check_output(["node", "--version"], text=True).strip())
    local_settings = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    stable_env = {
        key: value
        for key, value in env.items()
        if key not in {"TEMP", "TMP", "TMPDIR", "PYTEST_CURRENT_TEST", "SHLVL", "_"}
    }
    data = {
        "input": input_hash,
        "identity": identity,
        "env": stable_env,
        "settings": local_settings,
        "commands": result["commands"],
        "scope": result["changed"],
        "deferred": result.get("deferred_tests", []),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def cached_receipt(output: Path, key: str, max_age: int) -> dict[str, Any] | None:
    for file in sorted(output.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if time.time() - file.stat().st_mtime > max_age:
            continue
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            if data.get("status") != "passed" or data.get("cache_key") != key:
                continue
            if data.get("input_before") != data.get("input_after"):
                continue
            if data.get("reused_from") or not data.get("steps"):
                continue  # Cache hits do not extend the original result's lifetime.
            if all(
                s.get("exit_code") == 0
                and Path(s["log"]).is_file()
                and hashlib.sha256(Path(s["log"]).read_bytes()).hexdigest() == s.get("log_sha256")
                for s in data["steps"]
            ):
                return dict(data, receipt_file=str(file))
        except (OSError, ValueError, KeyError):
            continue
    return None


def run_bounded(argv: list[str], cwd: Path, env: dict[str, str], stream: Any, timeout: float) -> int:
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=os.name != "nt"
    )
    try:
        return proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == "nt":
            cleanup = subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=5,
            )
            if cleanup.returncode and proc.poll() is None:
                raise OSError(
                    f"Could not terminate validation process tree (PID {proc.pid}); "
                    "stop it with process-management permission before retrying."
                ) from None
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        raise
