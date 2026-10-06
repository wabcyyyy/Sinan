"""Cassette 录制/回放（PR-8 / D6）：外呼唯一通道的真实交互固化为离线 fixture。

D6 定档：录制器挂**唯一外呼通道**——LLM 的 `LLMClient.chat_response` /
`stream_chat_deltas`（`complete`/`chat` 的共同出口；D6 锚点写 `llm_client.complete`，
现行实现里真正的唯一非流出口是 chat_response，按语义挂这两处）与外部 HTTP 的
`ExternalClient.fetch_bytes`；脱敏复用 `runtime.trace.redact`（**录制即脱敏**：
落盘前整段文本过一遍，含 URL 里的 key 与 Bearer 头）。

- 录制：`cassette_session(path, record=True)` 包住真实交互，退出时把
  「通道 + 请求指纹 + 脱敏后的请求/响应」写成 JSON。资产纪律：**只收小样本**
  （单文件 ≤20 条，超出即响亮失败——cassette 是大文本资产）；报告记 cassette 哈希。
- 回放：同请求指纹命中即回放；未命中抛 `CassetteMiss`，**禁止静默打网**——
  CI 的端到端用例必须纯离线跑通。
- 指纹：对**脱敏后**的规范化请求 JSON 取 sha256——密钥片段不入哈希，回放环境的
  凭据与录制时不同也能命中；`model` 不入指纹（应答只跟"问了什么"绑定）。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agent.runtime.trace import redact
from app.common import external_client
from app.common.llm_client import LLMClient

#: 单 cassette 的资产纪律：只收小样本（PR-8 风险栏）
MAX_ENTRIES = 20


class CassetteMiss(RuntimeError):
    """回放未命中：宁可响亮失败，也不静默打网（离线回放的底线）。"""


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _fingerprint(payload: dict) -> str:
    """请求指纹 = 脱敏后规范化 JSON 的 sha256（密钥不入哈希，跨环境可命中）。"""
    return hashlib.sha256(redact(_canonical(payload)).encode("utf-8")).hexdigest()


def cassette_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Cassette:
    def __init__(self, path: Path, *, record: bool) -> None:
        self.path = path
        self.record = record
        self.entries: list[dict] = []
        self._used: set[str] = set()
        if not record and path.is_file():
            document = json.loads(path.read_text(encoding="utf-8"))
            self.entries = list(document.get("entries") or [])

    def lookup(self, channel: str, fingerprint: str) -> dict:
        for entry in self.entries:
            if entry.get("channel") == channel and entry.get("fingerprint") == fingerprint:
                self._used.add(fingerprint)
                return entry
        raise CassetteMiss(f"cassette 未命中：{self.path.name} / {channel} / {fingerprint[:12]}（请补录后再离线回放）")

    def record_entry(self, channel: str, fingerprint: str, request: dict, response: Any) -> None:
        if len(self.entries) >= MAX_ENTRIES:
            raise RuntimeError(f"cassette 超出小样本纪律（≤{MAX_ENTRIES} 条）：{self.path.name}")
        self.entries.append({"channel": channel, "fingerprint": fingerprint, "request": request, "response": response})

    def save(self) -> None:
        """落盘 = 整段脱敏（录制即 _redact，D6）+ 记录时间与条数。"""
        document = {
            "version": 1,
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "entry_count": len(self.entries),
            "entries": self.entries,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(redact(json.dumps(document, ensure_ascii=False, indent=2)) + "\n", encoding="utf-8")


def _response_bytes_payload(raw: bytes | None) -> dict:
    if raw is None:
        return {"bytes": None}
    try:
        return {"bytes_text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"bytes_b64": base64.b64encode(raw).decode("ascii")}


def _payload_bytes(payload: dict) -> bytes | None:
    if payload.get("bytes") is None:
        return None
    if "bytes_text" in payload:
        return str(payload["bytes_text"]).encode("utf-8")
    return base64.b64decode(str(payload["bytes_b64"]))


@contextmanager
def cassette_session(path: Path, *, record: bool = False) -> Iterator[Cassette]:
    """把三条外呼通道换成录制/回放实现；退出时 record 模式落盘（脱敏）。"""
    cassette = Cassette(path, record=record)
    real_chat_response = LLMClient.chat_response
    real_stream_chat_deltas = LLMClient.stream_chat_deltas
    real_fetch_bytes = external_client.fetch_bytes

    def chat_response(self: LLMClient, *args: Any, **kwargs: Any) -> dict:
        messages = args[0] if args else kwargs.get("messages")
        payload = {
            "channel": "llm.chat_response",
            "messages": messages,
            "temperature": kwargs.get("temperature"),
            "max_tokens": kwargs.get("max_tokens"),
            "json_mode": kwargs.get("json_mode"),
        }
        fingerprint = _fingerprint(payload)
        if not cassette.record:
            return cassette.lookup("llm.chat_response", fingerprint)["response"]
        response = real_chat_response(self, *args, **kwargs)
        cassette.record_entry("llm.chat_response", fingerprint, payload, response)
        return response

    def stream_chat_deltas(self: LLMClient, *args: Any, **kwargs: Any) -> Iterator[str]:
        messages = args[0] if args else kwargs.get("messages")
        payload = {
            "channel": "llm.stream_chat_deltas",
            "messages": messages,
            "temperature": kwargs.get("temperature"),
            "max_tokens": kwargs.get("max_tokens"),
            "json_mode": kwargs.get("json_mode"),
        }
        fingerprint = _fingerprint(payload)
        if not cassette.record:
            yield from cassette.lookup("llm.stream_chat_deltas", fingerprint)["response"]["chunks"]
            return
        chunks = list(real_stream_chat_deltas(self, *args, **kwargs))
        cassette.record_entry("llm.stream_chat_deltas", fingerprint, payload, {"chunks": chunks})
        yield from chunks

    def fetch_bytes(client, http, url, *, params=None, headers=None, json_body=None):
        payload = {
            "channel": "external.fetch_bytes",
            "name": getattr(client, "name", ""),
            "url": url,
            "params": params,
            "json_body": json_body,
        }
        fingerprint = _fingerprint(payload)
        if not cassette.record:
            return _payload_bytes(cassette.lookup("external.fetch_bytes", fingerprint)["response"])
        raw = real_fetch_bytes(client, http, url, params=params, headers=headers, json_body=json_body)
        cassette.record_entry("external.fetch_bytes", fingerprint, payload, _response_bytes_payload(raw))
        return raw

    LLMClient.chat_response = chat_response
    LLMClient.stream_chat_deltas = stream_chat_deltas
    external_client.fetch_bytes = fetch_bytes
    try:
        yield cassette
    finally:
        LLMClient.chat_response = real_chat_response
        LLMClient.stream_chat_deltas = real_stream_chat_deltas
        external_client.fetch_bytes = real_fetch_bytes
        if cassette.record:
            cassette.save()
