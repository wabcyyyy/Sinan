"""轻量本地 Trace 持久化。

默认 JSONL 适合单实例开发和故障回放；生产部署可以通过同一接口替换为
OpenTelemetry/数据库实现。每条记录已经是 TraceRecorder 的脱敏结果。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path


class TraceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, trace: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(trace, ensure_ascii=False, separators=(",", ":"))
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            # 轨迹不能阻塞旅行规划主链路；内存指标仍保留最近记录。
            return

    def get(self, run_id: str) -> dict | None:
        try:
            with self._lock, self.path.open("r", encoding="utf-8") as handle:
                found = None
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if value.get("run_id") == run_id:
                        found = value
                return found
        except (OSError, UnicodeError):
            return None
