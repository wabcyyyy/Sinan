from tests.agent_eval.replay import _run


def test_failure_replay_detects_all_known_failure_types():
    assert _run({"id": "unknown", "kind": "authority"})["detected"]
    assert _run({"id": "route", "kind": "route"})["detected"]
    assert _run({"id": "opening", "kind": "opening"})["detected"]
