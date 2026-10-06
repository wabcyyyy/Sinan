from app.agent.runtime.observability import metrics, observe_run, use_scene
from app.agent.runtime.trace import record_event


def test_observe_run_aggregates_llm_tools_retries_and_tokens():
    metrics.reset()
    with observe_run("test-run"):
        record_event("llm", "llm.generate")
        # token 用量由 llm_client 直接上报（覆盖未包 trace 的调用），不再依赖事件聚合。
        metrics.record_llm_call(100, 20)
        record_event("tool", "poi.search_attractions")
        record_event("route", "retry")
        record_event("route", "fallback")
        record_event("decision", "run_status", metadata={"status": "degraded"})
    snapshot = metrics.snapshot()
    assert snapshot["runs"] == 1
    assert snapshot["successes"] == 0
    assert snapshot["degraded_runs"] == 1
    assert snapshot["degraded_rate"] == 1.0
    assert snapshot["llm_calls"] == 1
    assert snapshot["tool_calls"] == 1
    assert snapshot["validation_retries"] == 1
    assert snapshot["fallback_runs"] == 1
    assert snapshot["prompt_tokens"] == 100
    assert snapshot["completion_tokens"] == 20


def test_record_llm_call_counts_without_trace_context():
    metrics.reset()
    # 未处于 observe_run 上下文（如 clarify/city-guide）时也应计入。
    metrics.record_llm_call(7, 3)
    metrics.record_llm_call(1, 1)
    snapshot = metrics.snapshot()
    assert snapshot["llm_calls"] == 2
    assert snapshot["prompt_tokens"] == 8
    assert snapshot["completion_tokens"] == 4


def test_observe_run_aggregates_mcp_failures():
    metrics.reset()
    with observe_run("mcp-run"):
        record_event("mcp", "amap.search_poi", status="error", error="timeout")
    snapshot = metrics.snapshot()
    assert snapshot["mcp_calls"] == 1
    assert snapshot["mcp_failures"] == 1
    assert snapshot["mcp_failure_rate"] == 1.0


def test_observe_run_keeps_error_as_failed_run():
    metrics.reset()
    try:
        with observe_run("failed-run"):
            record_event("tool", "amap.search_poi", status="error", error="timeout")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    snapshot = metrics.snapshot()
    assert snapshot["failures"] == 1
    assert snapshot["recent_failures"][0]["run_id"] == "failed-run"
    failed_trace = metrics.get_trace("failed-run")
    assert failed_trace is not None
    assert any(
        event["name"] == "run_status" and event["metadata"]["status"] == "failed" for event in failed_trace["events"]
    )


def test_cancelled_run_counted_separately_from_success_and_failure():
    """客户端断开取消：单列 cancelled_runs，既不算成功也不算失败。"""
    metrics.reset()
    with observe_run("cancelled-run"):
        record_event("decision", "run_status", status="cancelled", metadata={"status": "cancelled"})
    snapshot = metrics.snapshot()
    assert snapshot["runs"] == 1
    assert snapshot["cancelled_runs"] == 1
    assert snapshot["successes"] == 0
    assert snapshot["failures"] == 0
    assert snapshot["degraded_runs"] == 0


def test_observe_run_keeps_recent_memory_and_persistent_trace_lookup():
    metrics.reset()
    with observe_run("lookup-run"):
        record_event("node", "parse")
    trace = metrics.get_trace("lookup-run")
    assert trace is not None
    assert trace["run_id"] == "lookup-run"
    assert trace["events"][0]["name"] == "parse"

    for index in range(101):
        with observe_run(f"run-{index}"):
            record_event("node", "parse")
    # 超出内存窗口后仍可从 JSONL Trace 存储查询，满足故障回放需求。
    assert metrics.get_trace("lookup-run") is not None
    # 超出内存窗口后仍可从持久化存储查询旧 Trace。
    assert metrics.get_trace("run-0") is not None
    assert metrics.get_trace("run-100") is not None


def test_trace_has_request_span_parent_and_action_links():
    with observe_run("run-linked", request_id="request-linked", action_id="action-linked") as trace:
        with __import__("app.agent.runtime.trace", fromlist=["trace_span"]).trace_span("node", "parent"):
            record_event("decision", "child")
    data = trace.to_dict()
    assert data["request_id"] == "request-linked"
    assert data["action_id"] == "action-linked"
    child = next(event for event in data["events"] if event["name"] == "child")
    parent = next(event for event in data["events"] if event["name"] == "parent")
    assert parent["span_id"] != child["span_id"]
    assert child["parent_span_id"] == parent["span_id"]
    assert child["action_id"] == "action-linked"


def test_llm_call_scene_attribution():
    metrics.reset()
    with use_scene("generate"):
        metrics.record_llm_call(100, 10)
    metrics.record_llm_call(7, 3)  # 无场景标记 → other
    snapshot = metrics.snapshot()
    assert snapshot["tokens_by_scene"]["generate"]["llm_calls"] == 1
    assert snapshot["tokens_by_scene"]["generate"]["prompt_tokens"] == 100
    assert snapshot["tokens_by_scene"]["other"]["llm_calls"] == 1
    assert snapshot["llm_calls"] == 2


def test_token_timeline_buckets():
    metrics.reset()
    metrics.record_llm_call(50, 5)
    metrics.record_llm_call(20, 2)
    snapshot = metrics.snapshot()
    timeline = snapshot["tokens_timeline"]
    assert len(timeline) == 1
    assert timeline[0]["calls"] == 2
    assert timeline[0]["prompt_tokens"] == 70
    assert timeline[0]["completion_tokens"] == 7


def test_llm_json_repair_total_visible_in_snapshot_and_prometheus():
    """P1-6：repair 率此前只进 trace 无归集出口，现在决策事件按 name 聚合。"""
    metrics.reset()
    with observe_run("repair-run"):
        record_event("decision", "llm_json_repair", metadata={"raw_len": 900})
        record_event("decision", "llm_json_repair", metadata={"raw_len": 1200})
        record_event("decision", "critic_result", metadata={"score": 0.5})
    snapshot = metrics.snapshot()
    assert snapshot["llm_json_repair_total"] == 2
    assert snapshot["decisions_by_name"]["llm_json_repair"] == 2
    assert snapshot["decisions_by_name"]["critic_result"] == 1
    text = metrics.prometheus_text()
    assert "travel_agent_llm_json_repair_total 2" in text
    metrics.reset()
    assert metrics.snapshot()["llm_json_repair_total"] == 0, "reset 清空决策计数"
