from app.agent.runtime.trace_store import TraceStore


def test_trace_store_round_trips_deidentified_trace(tmp_path):
    store = TraceStore(tmp_path / "traces.jsonl")
    value = {"run_id": "run-1", "request_id": "req-1", "events": []}
    store.append(value)
    assert store.get("run-1") == value
