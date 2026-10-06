"""UsageStore（SQLite 用量历史库）单元测试。"""

import time

from app.agent.runtime.usage_store import UsageStore


def _store(tmp_path) -> UsageStore:
    return UsageStore(tmp_path / "usage.db")


def test_record_and_summary(tmp_path):
    store = _store(tmp_path)
    store.record("generate", "qwen-plus", 100, 20, 500, True)
    store.record("generate", "qwen-plus", 50, 10, 300, True)
    store.record("clarify", "qwen-turbo", 10, 2, 100, False, "timeout")

    s = store.summary(0, int(time.time()) + 60)
    assert s["calls"] == 3
    assert s["successes"] == 2
    assert s["failures"] == 1
    assert s["prompt_tokens"] == 160
    assert s["completion_tokens"] == 32
    assert s["total_tokens"] == 192
    assert abs(s["success_rate"] - 2 / 3) < 1e-4
    assert s["avg_duration_ms"] == 300.0


def test_by_scene_and_model(tmp_path):
    store = _store(tmp_path)
    store.record("generate", "qwen-plus", 100, 20, 500, True)
    store.record("clarify", "qwen-turbo", 10, 2, 100, True)
    store.record("clarify", "qwen-turbo", 20, 4, 200, True)

    scenes = {row["scene"]: row for row in store.by_scene(0, int(time.time()) + 60)}
    assert scenes["generate"]["calls"] == 1
    assert scenes["clarify"]["calls"] == 2
    # 按 token 总量降序：generate(120) > clarify(36)
    ordered = store.by_scene(0, int(time.time()) + 60)
    assert ordered[0]["scene"] == "generate"

    models = {row["model"]: row for row in store.by_model(0, int(time.time()) + 60)}
    assert models["qwen-plus"]["prompt_tokens"] == 100
    assert models["qwen-turbo"]["calls"] == 2


def test_timeline_buckets_fill_gaps(tmp_path):
    store = _store(tmp_path)
    store.record("generate", "m", 10, 5, 100, True)
    end = int(time.time()) + 60
    start = end - 600  # 10 分钟范围，按 60s 分桶 → 对齐后 10~11 桶
    buckets = store.timeline(start, end, 60)
    assert 10 <= len(buckets) <= 11
    total_prompt = sum(b["prompt_tokens"] for b in buckets)
    assert total_prompt == 10
    non_empty = [b for b in buckets if b["calls"]]
    assert len(non_empty) == 1


def test_calls_paging(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.record("other", "m", i, 1, 10, True)
    page = store.calls(0, int(time.time()) + 60, limit=3, offset=0)
    assert page["total"] == 5
    assert len(page["records"]) == 3
    page2 = store.calls(0, int(time.time()) + 60, limit=3, offset=3)
    assert len(page2["records"]) == 2
    # 时间倒序：最后记录的排最前
    assert page["records"][0]["prompt_tokens"] == 4


def test_cleanup(tmp_path):
    store = _store(tmp_path)
    store.record("other", "m", 1, 1, 10, True)
    assert store.cleanup(90 * 86400) == 0
    # 直接改库模拟旧数据
    with store._lock:
        store._conn.execute("UPDATE llm_calls SET ts = ts - 100 * 86400")
        store._conn.commit()
    assert store.cleanup(90 * 86400) == 1
    assert store.summary(0, int(time.time()) + 60)["calls"] == 0


def test_run_id_recorded_and_legacy_column_added(tmp_path):
    """P1-7：run_id 随明细落库；存量库（无 run_id 列）init 时自动补列。"""
    import sqlite3

    store = _store(tmp_path)
    store.record("generate", "qwen-plus", 100, 20, 500, True, run_id="run-abc")
    store.record("research", "qwen-plus", 10, 2, 100, True)
    report = store.calls(0, int(time.time()) + 60)
    by_run = {r["run_id"] for r in report["records"]}
    assert by_run == {"run-abc", None}, "run_id 如实落库；无 trace 上下文为 NULL"
    store.close()

    # 模拟存量库：手工建一个没有 run_id 列的 llm_calls
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL,"
        " scene TEXT NOT NULL DEFAULT 'other', model TEXT NOT NULL DEFAULT '',"
        " prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,"
        " duration_ms INTEGER NOT NULL DEFAULT 0, success INTEGER NOT NULL DEFAULT 1, error TEXT)"
    )
    conn.commit()
    conn.close()

    reopened = UsageStore(legacy)
    reopened.record("generate", "qwen-plus", 1, 1, 10, True, run_id="run-legacy")
    assert reopened.calls(0, int(time.time()) + 60)["records"][0]["run_id"] == "run-legacy", (
        "PRAGMA 探列缺 run_id → ALTER TABLE 补列，旧库升级不丢写"
    )
    reopened.close()
