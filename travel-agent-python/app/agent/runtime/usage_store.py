"""LLM 用量历史存储：SQLite 落库，支撑后台仪表盘的历史查询。

每次真实 LLM 调用（含流式）由 llm_client 上报一行明细；
聚合查询按时间范围/分桶粒度在 SQL 层完成，避免全量载入内存。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.common.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    scene TEXT NOT NULL DEFAULT 'other',
    model TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1,
    error TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls (ts);
CREATE INDEX IF NOT EXISTS idx_llm_calls_scene ON llm_calls (scene);
"""

# 仪表盘可选时间窗；未知 range 一律回落 24h
_RANGE_SECONDS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def bucket_seconds(range_key: str) -> int:
    """分桶粒度：1h 按分钟、24h 按小时、7d/30d 按天。"""
    if range_key == "1h":
        return 60
    if range_key == "24h":
        return 3600
    return 86400


class UsageStore:
    """单连接 + 互斥锁的轻量 SQLite 用量存储。"""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        # 存量库升级（SQLite 内部库，不走 MySQL 迁移链）：探列缺 run_id 就补——
        # 该列是"一次生成花多少 token/钱"的 run 级归因键（P1-7）
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
        if "run_id" not in columns:
            self._conn.execute("ALTER TABLE llm_calls ADD COLUMN run_id TEXT")
        self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def record(
        self,
        scene: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: int,
        success: bool,
        error: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """登记一次 LLM 调用明细（失败调用 token 记 0，用于错误率统计）。"""
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO llm_calls (ts, scene, model, prompt_tokens, completion_tokens,"
                " duration_ms, success, error, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    scene or "other",
                    model or "",
                    max(int(prompt_tokens or 0), 0),
                    max(int(completion_tokens or 0), 0),
                    max(int(duration_ms or 0), 0),
                    1 if success else 0,
                    (error or None) if success else (error or "unknown"),
                    run_id or None,
                ),
            )

    def summary(self, start_ts: int, end_ts: int) -> dict:
        """时间范围内的总体统计。"""
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(*), SUM(success), SUM(prompt_tokens), SUM(completion_tokens),"
                " AVG(duration_ms) FROM llm_calls WHERE ts >= ? AND ts < ?",
                (start_ts, end_ts),
            ).fetchone()
        calls = int(row[0] or 0)
        successes = int(row[1] or 0)
        prompt = int(row[2] or 0)
        completion = int(row[3] or 0)
        return {
            "calls": calls,
            "successes": successes,
            "failures": calls - successes,
            "success_rate": round(successes / calls, 4) if calls else 0.0,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "avg_duration_ms": round(float(row[4] or 0), 1),
        }

    def by_scene(self, start_ts: int, end_ts: int) -> list[dict]:
        return self._group("scene", start_ts, end_ts)

    def by_model(self, start_ts: int, end_ts: int) -> list[dict]:
        return self._group("model", start_ts, end_ts)

    def _group(self, column: str, start_ts: int, end_ts: int) -> list[dict]:
        # column 仅由 by_scene/by_model 传入白名单字段，无注入风险。
        sql = (
            "SELECT " + column + ", COUNT(*), SUM(success), SUM(prompt_tokens),"
            " SUM(completion_tokens), AVG(duration_ms) FROM llm_calls"
            " WHERE ts >= ? AND ts < ? GROUP BY " + column + ""
            " ORDER BY SUM(prompt_tokens) + SUM(completion_tokens) DESC"
        )
        with self._cursor() as cur:
            rows = cur.execute(sql, (start_ts, end_ts)).fetchall()
        return [
            {
                column: row[0] if row[0] is not None else "",
                "calls": int(row[1] or 0),
                "successes": int(row[2] or 0),
                "prompt_tokens": int(row[3] or 0),
                "completion_tokens": int(row[4] or 0),
                "avg_duration_ms": round(float(row[5] or 0), 1),
            }
            for row in rows
        ]

    def timeline(self, start_ts: int, end_ts: int, bucket_seconds: int) -> list[dict]:
        """按固定秒数分桶的消耗趋势（桶起点 ts 对齐 epoch）。"""
        with self._cursor() as cur:
            rows = cur.execute(
                "SELECT (ts / ?) * ? AS bucket, COUNT(*), SUM(prompt_tokens),"
                " SUM(completion_tokens) FROM llm_calls WHERE ts >= ? AND ts < ?"
                " GROUP BY bucket ORDER BY bucket",
                (bucket_seconds, bucket_seconds, start_ts, end_ts),
            ).fetchall()
        by_bucket = {
            int(row[0]): {
                "calls": int(row[1] or 0),
                "prompt_tokens": int(row[2] or 0),
                "completion_tokens": int(row[3] or 0),
            }
            for row in rows
        }
        result = []
        for slot in range(start_ts - start_ts % bucket_seconds, end_ts, bucket_seconds):
            data = by_bucket.get(slot, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
            result.append({"ts": slot, **data})
        return result

    def calls(self, start_ts: int, end_ts: int, limit: int = 100, offset: int = 0) -> dict:
        """时间范围内的调用明细（时间倒序分页）。"""
        with self._cursor() as cur:
            total = cur.execute(
                "SELECT COUNT(*) FROM llm_calls WHERE ts >= ? AND ts < ?", (start_ts, end_ts)
            ).fetchone()[0]
            rows = cur.execute(
                "SELECT ts, scene, model, prompt_tokens, completion_tokens, duration_ms,"
                " success, error, run_id FROM llm_calls WHERE ts >= ? AND ts < ?"
                " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (start_ts, end_ts, limit, offset),
            ).fetchall()
        return {
            "total": int(total),
            "records": [
                {
                    "ts": row[0],
                    "scene": row[1],
                    "model": row[2],
                    "prompt_tokens": int(row[3] or 0),
                    "completion_tokens": int(row[4] or 0),
                    "duration_ms": int(row[5] or 0),
                    "success": bool(row[6]),
                    "error": row[7],
                    "run_id": row[8],
                }
                for row in rows
            ],
        }

    def report(self, range_key: str, limit: int = 200, offset: int = 0) -> dict:
        """仪表盘一次性取数：`/api/agent/v1/usage` 与 `/api/admin/llm-usage` 共用。

        range/limit/offset 的三条收敛规则原先只写在 agent 路由里；Admin 侧复用同一条
        链路后必须同源，否则两个入口对同一个 `range` 会给出不同时间窗。
        """
        key = range_key if range_key in _RANGE_SECONDS else "24h"
        end = int(time.time()) + 60
        start = end - _RANGE_SECONDS[key]
        bucket = bucket_seconds(key)
        return {
            "range": key,
            "bucket": bucket,
            "summary": self.summary(start, end),
            "by_scene": self.by_scene(start, end),
            "by_model": self.by_model(start, end),
            "timeline": self.timeline(start, end, bucket),
            "calls": self.calls(start, end, max(1, min(int(limit), 500)), max(0, int(offset))),
        }

    def cleanup(self, retain_seconds: int) -> int:
        """删除保留期之前的明细，返回删除行数。"""
        cutoff = int(time.time()) - retain_seconds
        with self._cursor() as cur:
            cur.execute("DELETE FROM llm_calls WHERE ts < ?", (cutoff,))
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# 模块级单例：全 Agent 共享一个 SQLite 连接（内部有锁）。
usage_store = UsageStore(settings.usage_db_path)
