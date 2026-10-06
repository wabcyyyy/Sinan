"""语音识别（ASR / MiMo）单测：线级形状 / 能力闸 / 业务面防线 / 全 mock。

覆盖：
- `speech_client.transcribe` 打出 MiMo 实证形状（chat/completions + input_audio data URI
  + 顶层 asr_options，转写取 choices[0].message.content；2026-10-04 校准）；
- 能力与凭据缺失时**发请求前**就报错（默认 provider 是 DeepSeek 官方，没有语音服务）；
- 业务面 `POST /api/speech-intent`：鉴权、格式白名单（MiMo 只收 wav/mp3）、空文件、
  上游故障 502、配置错 502 带原因。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.agent  # noqa: F401 先行破存量导入环（同 tests/test_image_intent.py 口径）
from app.api import deps
from app.api.business.speech_intent import router as speech_intent_router
from app.common import speech_client
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.common.http_client import configure_clients
from app.common.jwt_compat import encode_token
from app.db import session as db_session
from app.db.models import Base
from app.services import speech_intent as speech_service

SIGNING_MATERIAL = "example-only-hs256-signing-material-32b"  # 测试占位串，非真实凭据

MIMO_BASE = "https://api.mimo.example/v1"
_WAV = b"RIFF....WAVEfmt "


@pytest.fixture(autouse=True)
def _reset_stt_runtime_state():
    """清 ASR 车道的熔断/车道时间戳：否则用例之间会互相把对方熔断掉。"""
    speech_client._stt_client.reset_runtime_state()
    yield
    speech_client._stt_client.reset_runtime_state()
    configure_clients(api=None)


@pytest.fixture(autouse=True)
def _mimo_configured(monkeypatch):
    """默认把 stt 角色指到 MiMo：默认配置下 main 是 DeepSeek 官方，没有语音能力。"""
    monkeypatch.setattr(settings, "llm_provider_mimo_base_url", MIMO_BASE)
    monkeypatch.setattr(settings, "llm_provider_mimo_api_key", "mimo-key")
    monkeypatch.setattr(settings, "llm_role_stt", "mimo:mimo-asr")


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    engine = create_engine(f"sqlite:///{tmp_path / 'speech_intent.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))

    monkeypatch.setattr(deps.settings, "jwt_secret", SIGNING_MATERIAL)
    monkeypatch.setattr(deps.token_revocation, "is_revoked", lambda _t: False)
    monkeypatch.setattr(
        deps.user_repository,
        "find_by_username",
        lambda _u: {"id": 42, "username": "alice", "role": "user", "status": 1},
    )

    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(speech_intent_router)
    yield TestClient(app)
    db_session.init_engine(None, None)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('alice', SIGNING_MATERIAL, 3600)}"}


def _post(client: TestClient, data: bytes = _WAV, content_type: str = "audio/wav"):
    files = {"file": ("clip.wav", data, content_type)}
    return client.post("/api/speech-intent", headers=_headers(), files=files)


# ---------- 1. 线级形状：MiMo 的 chat/completions + input_audio ----------


def test_transcribe_posts_mimo_chat_completions_shape() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "帮我安排杭州三天"}}]})

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    text = speech_client.transcribe(_WAV, content_type="audio/wav")

    assert text == "帮我安排杭州三天"
    assert seen["url"] == f"{MIMO_BASE}/chat/completions"
    assert seen["auth"] == "Bearer mimo-key"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "mimo-asr"
    # 音频是 base64 data URI 放 input_audio 块；language 在请求顶层 asr_options
    part = body["messages"][0]["content"][0]
    assert part["type"] == "input_audio"
    data_uri = part["input_audio"]["data"]
    assert data_uri.startswith("data:audio/wav;base64,")
    assert base64.b64decode(data_uri.split(",", 1)[1]) == _WAV
    assert body["asr_options"] == {"language": "zh"}


def test_transcribe_raises_when_blocked_by_breaker() -> None:
    """供应商连挂 5 次后熔断开窗：调用在本地就被拒，不再外呼。"""
    attempts = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, json={"error": "down"})

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    for _ in range(6):
        with pytest.raises((RuntimeError, Exception)):
            speech_client.transcribe(_WAV)
    assert attempts["n"] <= 5, "熔断后不得继续外呼"


# ---------- 2. 能力与凭据闸：发请求前就报错 ----------


def test_transcribe_raises_when_stt_role_has_no_audio_capability(monkeypatch) -> None:
    """默认"留空复用 main"时通道是 DeepSeek 官方，没有语音服务——必须本地报错。

    旧断言 match="不支持语音识别"为什么改：那是写给运维的文案，经 502 直达终端
    用户（R1-F3）；用户文案改为"未启用语音识别服务"，运维指引在日志（见
    test_transcribe_config_error_is_user_facing_and_ops_go_to_logs）。
    """
    monkeypatch.setattr(settings, "llm_role_stt", "")  # 回落 main（DeepSeek）
    with pytest.raises(ValueError, match="未启用语音识别服务"):
        speech_client.transcribe(_WAV)


def test_transcribe_raises_when_mimo_has_no_key(monkeypatch) -> None:
    """没配凭据同走"未启用"用户文案（旧 match="没配凭据"是运维话术，理由同上）。"""
    monkeypatch.setattr(settings, "llm_provider_mimo_api_key", "")
    with pytest.raises(ValueError, match="未启用语音识别服务"):
        speech_client.transcribe(_WAV)


# ---------- 3. 业务面防线 ----------


def test_speech_intent_requires_auth(client: TestClient) -> None:
    response = client.post("/api/speech-intent", files={"file": ("clip.wav", _WAV, "audio/wav")})
    assert response.status_code == 401


def test_speech_intent_rejects_unsupported_format(client: TestClient) -> None:
    response = _post(client, content_type="audio/amr")
    assert response.status_code == 400
    assert "wav" in response.json()["message"]


def test_speech_intent_rejects_webm_even_though_recorders_emit_it(client: TestClient) -> None:
    """webm 是 MediaRecorder 的默认产物，但 MiMo 不收——白名单必须拒（2026-10-04 收窄）。

    前端已把录音转 WAV 再上传；这里钉住"忘了转也不会静默送上游"。"""
    response = _post(client, content_type="audio/webm")
    assert response.status_code == 400


def test_speech_intent_rejects_empty_audio(client: TestClient) -> None:
    response = _post(client, data=b"")
    assert response.status_code == 400


def test_speech_intent_returns_transcript(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(speech_service, "transcribe", lambda *_a, **_k: "想去成都吃火锅")
    response = _post(client)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["text"] == "想去成都吃火锅"
    assert data["suggestedMessage"] == "想去成都吃火锅"


def test_speech_intent_upstream_failure_is_generic_502(client: TestClient, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("upstream 503 for url https://api.mimo.example/v1/chat/completions")

    monkeypatch.setattr(speech_service, "transcribe", _boom)
    response = _post(client)
    assert response.status_code == 502
    assert response.json()["message"] == "语音识别服务暂不可用", "上游异常不得透传"


def test_speech_intent_empty_transcript_surfaces_reason(client: TestClient) -> None:
    """空转写是这段音频的确定性结果，不是服务故障：502 要带"请求失败：<原因>"，
    不得落"语音识别服务暂不可用"——那句会诱导用户原样重试必败请求（2026-10-05）。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    configure_clients(api=httpx.Client(transport=httpx.MockTransport(handler)))
    response = _post(client)
    assert response.status_code == 502
    assert "请求失败" in response.json()["message"], "空转写属 ValueError 通道，必须透出原因"
    assert "语音识别服务暂不可用" not in response.json()["message"], "不得伪装成服务故障"


def test_speech_intent_config_error_surfaces_reason(client: TestClient, monkeypatch) -> None:
    """配置错也是用户可见面：文案说"未启用，可直接输入"，不露 .env 键名（R1-F3）。

    旧断言为什么改：旧文案把运维键名（LLM_PROVIDER_MIMO_API_KEY）写进 502 message
    直达终端用户——用户看不懂也修不了，还泄漏部署细节；运维指引已挪进服务端日志。
    """

    def _misconfigured(*_a, **_k):
        raise ValueError("当前部署未启用语音识别服务，请稍后再试或直接输入文字")

    monkeypatch.setattr(speech_service, "transcribe", _misconfigured)
    response = _post(client)
    assert response.status_code == 502
    assert "未启用语音识别服务" in response.json()["message"]
    assert "LLM_PROVIDER_MIMO_API_KEY" not in response.json()["message"]


def test_router_mounted_on_real_app() -> None:
    """回归防线：business_routers 漏注册时端点会 404 而单域测试仍绿。"""
    import main

    paths = {getattr(route, "path", None) for route in main.app.routes}
    assert "/api/speech-intent" in paths


# ---------- R1-F3 文案 / R3-F3 故障语义三态（2026-10-05） ----------


def test_transcribe_config_error_is_user_facing_and_ops_go_to_logs(monkeypatch, caplog) -> None:
    """配置错（stt 角色无 audio 能力）：用户文案不露 .env 键名，运维指引进日志。"""
    import logging

    monkeypatch.setattr(settings, "llm_role_stt", "some-chat-model")  # 裸名 → default，无 audio 能力
    with caplog.at_level(logging.ERROR, logger="app.common.speech_client"):
        with pytest.raises(ValueError) as ei:
            speech_client.transcribe(_WAV)
    message = str(ei.value)
    assert "未启用语音识别服务" in message
    assert "LLM_PROVIDER_MIMO_API_KEY" not in message, "运维键名不得直达终端用户（R1-F3）"
    assert "LLM_PROVIDER_MIMO_API_KEY" in caplog.text, "运维指引挪进日志，但不能丢"


def test_transcribe_skipped_calls_map_to_service_unavailable(monkeypatch) -> None:
    """熔断开窗 / 车道放弃 / loader 故障都不是这段录音的问题：必须话术"服务暂不可用"，
    不得引导用户重录（R3-F3：旧实现把一切 None 话术成"请重录"，服务故障被说成用户的错）。"""
    from app.common.envelope import ApiError

    for reason in ("breaker_open", "lane_busy", "loader_error"):
        monkeypatch.setattr(speech_client._stt_client, "call", lambda *a, **k: None)
        monkeypatch.setattr(speech_client._stt_client, "last_skip_reason", lambda _r=reason: _r)
        with pytest.raises(ApiError) as ei:
            speech_client.transcribe(_WAV)
        assert "语音服务暂时不可用" in str(ei.value), f"skip={reason} 不得话术成请重录"


def test_transcribe_loader_empty_keeps_rerecord_copy(monkeypatch) -> None:
    """上游真跑且明确回空转写：保留 R1-F6 的"请重录"引导（确定性结果，非服务故障）。"""
    monkeypatch.setattr(speech_client._stt_client, "call", lambda *a, **k: None)
    monkeypatch.setattr(speech_client._stt_client, "last_skip_reason", lambda: "loader_empty")
    with pytest.raises(ValueError) as ei:
        speech_client.transcribe(_WAV)
    assert "请重录" in str(ei.value)
