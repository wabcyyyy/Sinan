"""硬退出原进程，再由新进程运行真实恢复编排；不以清内存模拟进程死亡。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("stage", ("reserve", "research", "matrix", "solution", "first_day", "terminal"))
def test_cp_worker_hard_crash_resume_budget_and_delivered_days(tmp_path, stage):
    root = Path(os.environ.get("SINAN_P1_CRASH_REPORT_DIR", str(tmp_path / "crashes")))
    root.mkdir(parents=True, exist_ok=True)
    directory = tmp_path / stage
    directory.mkdir()
    output = root / stage
    output.mkdir()
    env = dict(os.environ, CHECKPOINT_DB_PATH=str(directory / "checkpoints.sqlite3"), PYTHONUTF8="1")
    command = [sys.executable, "-m", "tests.p1_crash_worker", "--directory", str(directory), "--stage", stage]
    # 直接写日志，避免 Windows 的默认 GBK reader thread 解码 UTF-8 异常而挂起 communicate。
    with (output / "crash.log").open("w", encoding="utf-8") as log:
        crashed = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=12)
    assert crashed.returncode == 91, (output / "crash.log").read_text("utf-8", errors="replace")
    with (output / "resume.log").open("w", encoding="utf-8") as log:
        resumed = subprocess.run([*command, "--resume"], env=env, stdout=log, stderr=subprocess.STDOUT, timeout=12)
    assert resumed.returncode == 0, (output / "resume.log").read_text("utf-8", errors="replace")
    for name in ("result.json", "events.jsonl"):
        (output / name).write_bytes((directory / name).read_bytes())
    result = json.loads((directory / "result.json").read_text("utf-8"))
    events = [json.loads(s) for s in (directory / "events.jsonl").read_text("utf-8").splitlines()]
    assert result["deadline_preserved"] and result["next_call_blocked"] and result["old_owner_rejected"]
    assert result["consumed_llm_calls"] == 1
    assert result["consumed_prompt_tokens"] == result["consumed_completion_tokens"] == 10
    assert sum(e["event"] == "simulated_request_reserved" for e in events) == 1
    assert result["already_delivered_items_unchanged"]
    if stage == "reserve":
        assert result["gen_state"] == "FAILED" and result["item_count"] == 0
    else:
        assert result["gen_state"] == "COMPLETED" and result["delivered_days"] == 2
        assert sum(e["event"] == "simulated_route_matrix" for e in events) == 1
    result["simulated_external_operations"] = events
    results = (
        json.loads((root / "report.json").read_text("utf-8"))["results"] if (root / "report.json").exists() else []
    )
    results.append(result)
    (root / "report.json").write_text(
        json.dumps(
            {
                "mode": "real process death and resume; frozen research/routes; isolated SQLite",
                "results": results,
                "new_model_calls": 0,
                "external_http": 0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
