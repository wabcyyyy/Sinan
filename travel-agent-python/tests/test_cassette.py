"""Cassette 录制/回放单测（PR-8 / D6）：回放纯离线、录制即脱敏、小样本纪律。

覆盖验收三件套：
1. 一条真实 case 录制 → 离线回放 → CI 跑通的端到端用例（最后一条测试）；
2. cassette 脱敏（录制即 `redact`）——另配合 CI 的 check-secrets 全仓扫描；
3. 资产纪律：单文件 ≤20 条（cassette 是大文本资产）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.editing import nl_edit
from app.common.llm_client import LLMClient
from app.schemas.trip import EditOpRequest
from tests.agent_eval.cassette import (
    MAX_ENTRIES,
    Cassette,
    CassetteMiss,
    _fingerprint,
    cassette_session,
    cassette_sha256,
)

CASSETTE_DIR = Path(__file__).resolve().parent / "agent_eval" / "cassettes"
REAL_CASE = CASSETTE_DIR / "edit_real_case.json"


class _FakeLLM:
    """录制态下的底层出口替身（cassette 会把「当时的真实实现」包起来）。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    def chat_response(self, self_client, messages, **kwargs):
        self.calls += 1
        return {"message": {"role": "assistant", "content": self.content}, "usage": {"total_tokens": 7}}


def test_record_then_replay_roundtrip(tmp_path: Path, monkeypatch):
    fake = _FakeLLM('{"ops": [{"action": "delete", "day_no": 1, "poi_name": "西湖"}]}')
    monkeypatch.setattr(LLMClient, "chat_response", fake.chat_response)
    path = tmp_path / "case.json"
    with cassette_session(path, record=True):
        assert LLMClient().chat([{"role": "user", "content": "hi"}]).startswith('{"ops"')
    assert fake.calls == 1

    monkeypatch.setattr(LLMClient, "chat_response", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得打网")))
    with cassette_session(path):
        assert LLMClient().chat([{"role": "user", "content": "hi"}]).startswith('{"ops"'), "回放应给回录制应答"


def test_record_redacts_secrets_on_save(tmp_path: Path, monkeypatch):
    """录制即脱敏（D6）：URL key 与 Bearer 头不得落进 cassette 文本。"""
    leaky = '{"ops": [], "note": "call https://api.example.com/x?apikey=SECRET123 with Bearer TOKEN456 done"}'
    fake = _FakeLLM(leaky)
    monkeypatch.setattr(LLMClient, "chat_response", fake.chat_response)
    path = tmp_path / "leaky.json"
    with cassette_session(path, record=True):
        LLMClient().chat([{"role": "user", "content": "https://api.example.com/y?key=SECRET123"}])
    text = path.read_text(encoding="utf-8")
    assert "SECRET123" not in text and "TOKEN456" not in text, "录制必须即刻脱敏"
    assert "***" in text
    # 指纹对脱敏后请求计算：密钥变化不影响命中（跨环境回放稳定）
    assert _fingerprint({"url": "https://x?key=AAA"}) == _fingerprint({"url": "https://x?key=BBB"})


def test_replay_miss_is_loud_not_silent_network(tmp_path: Path):
    with pytest.raises(CassetteMiss):
        with cassette_session(tmp_path / "absent.json"):
            LLMClient().chat([{"role": "user", "content": "hi"}])


def test_cassette_enforces_small_sample_and_committed_files_fit(tmp_path: Path):
    cassette = Cassette(tmp_path / "big.json", record=True)
    for index in range(MAX_ENTRIES):
        cassette.record_entry("llm.chat_response", str(index), {"i": index}, {"ok": True})
    with pytest.raises(RuntimeError, match="小样本"):
        cassette.record_entry("llm.chat_response", "overflow", {}, {})
    for path in CASSETTE_DIR.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["entry_count"] <= MAX_ENTRIES, f"{path.name} 超出小样本纪律"
        assert len(cassette_sha256(path)) == 64


def test_real_edit_case_replays_offline_end_to_end():
    """验收①：真实录制的 nl_edit case 在 CI 纯离线回放跑通（禁止打网）。"""
    request = EditOpRequest(
        city="杭州",
        days=1,
        plans=[
            {
                "day_no": 1,
                "items": [
                    {"item_type": "attraction", "poi_name": "西湖"},
                    {"item_type": "food", "poi_name": "楼外楼"},
                ],
            }
        ],
        instruction="把第 1 天的西湖改到 10:30 出发",
    )
    with cassette_session(REAL_CASE):
        ops = nl_edit.run_edit_ops(request)
    assert len(ops) == 1
    assert (ops[0].action, ops[0].poi_name, ops[0].start_time) == ("update_time", "西湖", "10:30")
