"""时区对照与增长面保留策略（审查 P1-9 / P2-2）。全离线：不连库、不连 Redis。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.common import retention, timezone_check


class _FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list[str] = []

    def execute(self, sql: str) -> None:
        self.executed.append(sql)

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        return None


class _FakeConnection:
    def __init__(self, row):
        self.cursor_obj = _FakeCursor(row)

    def cursor(self):
        return self.cursor_obj


def test_offset_hours_parses_common_mysql_timezones() -> None:
    assert timezone_check.offset_hours("+08:00") == 8
    assert timezone_check.offset_hours("-05:30") == -5.5
    assert timezone_check.offset_hours("UTC") == 0
    assert timezone_check.offset_hours("SYSTEM") is None, "SYSTEM 无从对照，不报警"
    assert timezone_check.offset_hours("Asia/Shanghai") is None, "命名时区不猜偏移"
    assert timezone_check.offset_hours("") is None


def test_timezone_mismatch_warns_without_failing(monkeypatch, caplog) -> None:
    monkeypatch.setattr(timezone_check, "local_utc_offset_hours", lambda now=None: 8.0)
    connection = _FakeConnection({"tz": "+00:00"})
    with caplog.at_level(logging.WARNING, logger="app.common.timezone_check"):
        assert timezone_check.check_timezone_alignment(connection) is False
    assert "timezone mismatch" in caplog.text and "整体平移" in caplog.text
    assert connection.cursor_obj.executed == ["SELECT @@session.time_zone AS tz"]


def test_timezone_aligned_and_unreadable_are_silent(monkeypatch, caplog) -> None:
    monkeypatch.setattr(timezone_check, "local_utc_offset_hours", lambda now=None: 8.0)
    with caplog.at_level(logging.WARNING, logger="app.common.timezone_check"):
        assert timezone_check.check_timezone_alignment(_FakeConnection({"tz": "+08:00"})) is True
        assert timezone_check.check_timezone_alignment(_FakeConnection({"tz": "SYSTEM"})) is True
    assert caplog.text == "", "一致与无从对照都不该产生警告"

    class _Broken:
        def cursor(self):
            raise RuntimeError("db down")

    assert timezone_check.check_timezone_alignment(_Broken()) is True, "检查绝不抛（不是启动闸门）"


def test_rotate_trace_store_archives_by_day_and_prunes(tmp_path) -> None:
    store = tmp_path / "agent_traces.jsonl"
    store.write_text('{"run_id":"a"}\n', encoding="utf-8")
    now = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)
    # 两档归档：一档过期（09-01，>14 天）、一档在保留期（09-20）
    (tmp_path / "agent_traces-2026-09-01.jsonl").write_text("{}\n", encoding="utf-8")
    fresh_archive = tmp_path / "agent_traces-2026-09-20.jsonl"
    fresh_archive.write_text("{}\n", encoding="utf-8")

    removed = retention.rotate_trace_store(store, now=now)

    assert removed == 1
    assert not (tmp_path / "agent_traces-2026-09-01.jsonl").exists(), "过期归档删除"
    assert fresh_archive.exists(), "保留期内归档不动"
    archived = tmp_path / "agent_traces-2026-09-26.jsonl"
    assert archived.exists() and archived.read_text(encoding="utf-8") == '{"run_id":"a"}\n'
    assert not store.exists(), "活动文件被归档（下次 append 自动重建）"


def test_rotate_trace_store_is_idempotent_and_skips_empty(tmp_path) -> None:
    store = tmp_path / "agent_traces.jsonl"
    store.write_text("", encoding="utf-8")
    now = datetime(2026, 9, 26, 3, 0, tzinfo=UTC)
    assert retention.rotate_trace_store(store, now=now) == 0
    assert store.exists(), "空文件不轮转（不产出空归档）"

    store.write_text("{}\n", encoding="utf-8")
    retention.rotate_trace_store(store, now=now)
    store.write_text("{}\n", encoding="utf-8")
    retention.rotate_trace_store(store, now=now)  # 同日第二次：并入已有归档
    assert (tmp_path / "agent_traces-2026-09-26.jsonl").read_text(encoding="utf-8") == "{}\n{}\n"


def test_cleanup_exports_deletes_only_expired_files(tmp_path) -> None:
    old = tmp_path / "itinerary_1.pdf"
    fresh = tmp_path / "itinerary_2.pdf"
    old.write_bytes(b"%PDF-1.4")
    fresh.write_bytes(b"%PDF-1.4")
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    old_time = (now - timedelta(days=31)).timestamp()
    import os

    os.utime(old, (old_time, old_time))

    assert retention.cleanup_exports(tmp_path, now=now) == 1
    assert not old.exists() and fresh.exists()
    assert retention.cleanup_exports(tmp_path / "missing", now=now) == 0, "目录不存在按 0"
    assert isinstance(Path(str(tmp_path)), Path)
