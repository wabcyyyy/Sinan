"""经仓库唯一验证入口启动隔离活栈及正式浏览器；无模型或供应商请求。"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONT = ROOT / "travel-frontend-react"


@contextmanager
def server(command, cwd, directory, name, port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            pass
    except OSError:
        pass
    else:
        raise RuntimeError(f"验收端口 {port} 已被使用，拒绝连接其他服务")
    with (directory / f"{name}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUTF8": "1"}
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, (directory / f"{name}.log").read_text("utf-8", errors="replace")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert time.monotonic() < deadline, f"{name} startup timeout"
                    time.sleep(0.1)
            yield
        finally:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize("scenario", ("plan", "conflict", "manual"))
def test_user_journey(tmp_path, scenario):
    node = shutil.which("node")
    assert node, "Node is required for the browser journey"
    output = Path(os.environ.get("SINAN_JOURNEY_REPORT_DIR", str(tmp_path / "reports"))) / scenario
    output.mkdir(parents=True, exist_ok=False)
    with (
        server(
            [
                sys.executable,
                "-m",
                "scripts.serve_journey_browser",
                "--run-dir",
                str(tmp_path / "backend"),
                "--scenario",
                scenario,
            ],
            ROOT / "travel-agent-python",
            output,
            "backend",
            18077,
        ),
        server(
            [node, "node_modules/vite/bin/vite.js", "--config", "e2e/vite.journey.config.ts"],
            FRONT,
            output,
            "frontend",
            15177,
        ),
        (output / "runner.log").open("w", encoding="utf-8") as log,
    ):
        result = subprocess.run(
            [node, "e2e/user-journeys.mjs", scenario],
            cwd=FRONT,
            env={**os.environ, "SINAN_JOURNEY_OUTPUT": str(output)},
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=60,
        )
        state = json.loads((tmp_path / "backend/backend-state.json").read_text("utf-8"))
        assert state["new_model_calls"] == state["blocked_model_attempts"] == 0
        # 冻结城市字典的初始化可能尝试兜底外呼；transport 拦截确保没有真实请求。
        (output / "backend-state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        assert result.returncode == 0, (output / "runner.log").read_text("utf-8", errors="replace")
        assert json.loads((output / "browser.json").read_text("utf-8"))["status"] == "passed"
