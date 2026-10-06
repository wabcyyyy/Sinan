"""语音识别（ASR）：用户录音 → 文本，通道由 `LLM_ROLE_STT` 角色解析。

口径（2026-10-04，线级形状已实证）：
- **能力在 provider 上声明**：目前只有 MiMo provider 带 `audio` 能力。默认 provider 是
  DeepSeek 官方，它没有语音服务——所以没配 `LLM_PROVIDER_MIMO_API_KEY` + `LLM_ROLE_STT`
  时，这里在发请求之前就**响亮报错**，而不是发一次注定 404 的往返再被上层读成"上游故障"。
- **MiMo 线级形状（实证：api.xiaomimimo.com 探针——/v1/chat/completions GET→405、
  /v1/audio/transcriptions GET→404——再对照社区实现 dsh-voice-input 的 transcribeXiaomi）**：
  `POST {base}/chat/completions`，音频是 base64 data URI 放 `messages[0].content[].input_audio.data`，
  language 在请求顶层 `asr_options.language`，转写取 `choices[0].message.content`。
  上游只收 wav/mp3（前端已把录音转 16k 单声道 WAV 再上传，见 speechWav.ts）；
  模型名须全小写（`mimo-v2.5-asr`）。换供应商时只改 `_endpoint` / `_payload` / `_transcript`。
- **INV-9**：外呼经 `ExternalClient`（超时 + 响应字节上限 + 熔断 + 车道节流）；
  **刻意关缓存**（`ttl_seconds=0`）——语音转写是用户内容，不该驻留进程内存。
"""

from __future__ import annotations

import base64
import logging
from typing import Any

from app.common import model_registry
from app.common.envelope import ApiError
from app.common.external_client import INTERACTIVE, ExternalClient, fetch_json
from app.common.http_client import api_client

logger = logging.getLogger(__name__)

#: ASR 车道：ttl=0 关缓存（用户语音不驻留）；负结果也不缓存（一次失败不该粘住重试）。
_stt_client: ExternalClient = ExternalClient(
    name="speech_stt",
    ttl_seconds=0,
    negative_ttl_seconds=0,
    timeout_seconds=60,
    max_response_bytes=256 * 1024,
)

#: MiMo 识别中文为主，显式带 language 让供应商走中文声学模型。
_LANGUAGE = "zh"


def _endpoint(base_url: str) -> str:
    """MiMo 的 ASR 不走 /audio/transcriptions（该网关上不存在），走 chat/completions。"""
    return base_url.rstrip("/") + "/chat/completions"


def _payload(model: str, audio: bytes, content_type: str) -> dict[str, Any]:
    """请求体：data URI 进 input_audio 块，language 在顶层 asr_options。"""
    data_uri = f"data:{content_type};base64,{base64.b64encode(audio).decode('ascii')}"
    return {
        "model": model,
        "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}]}],
        "asr_options": {"language": _LANGUAGE},
    }


def _transcript(payload: Any) -> str | None:
    """转写在 choices[0].message.content（chat 形状）；异形/空文本一律归 None。"""
    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    text = content.strip() if isinstance(content, str) else ""
    return text or None


def transcribe(audio: bytes, *, content_type: str = "audio/wav") -> str:
    """把一段录音转成文本；失败抛异常（业务面转 502，调用方不得拿空串冒充识别成功）。

    失败三态（R1-F3 文案 + R3-F3 故障语义，2026-10-05）：
    - 配置错（provider 不支持语音 / 没配凭据）：运维指引（配什么键）进 `logger.error`，
      用户文案只说"未启用语音识别，可直接输入文字"——.env 键名直达终端用户是运维
      话术泄漏，用户既看不懂也修不了；
    - 服务没调通（熔断开窗 / 车道放弃 / 上游故障）：`ApiError(502)`"语音服务暂时
      不可用，请稍后再试"——这段录音没有问题，"请重录"只会诱导无效重录；
    - 上游明确空转写（请求真到达且返回空内容）：`ValueError` 引导重录——这是这段
      音频**确定性地**识别不出内容，不是服务故障。
    """
    bound = model_registry.binding("stt")
    if "audio" not in bound.capabilities():
        logger.error(
            "speech_stt: provider %s（base_url=%s）不支持语音识别。运维指引：在 .env 配 "
            "LLM_PROVIDER_MIMO_API_KEY，并把 LLM_ROLE_STT 指向 mimo:mimo-v2.5-asr"
            "（MiMo base_url 已有官方默认，无需另配）",
            bound.provider.name,
            bound.base_url or "未配置",
        )
        raise ValueError("当前部署未启用语音识别服务，请稍后再试或直接输入文字")
    if not bound.provider.api_key.strip():
        logger.error(
            "speech_stt: 语音通道（%s）没配凭据。运维指引：设置 LLM_PROVIDER_MIMO_API_KEY（或 LLM_API_KEY）",
            bound.base_url,
        )
        raise ValueError("当前部署未启用语音识别服务，请稍后再试或直接输入文字")

    def _load() -> str | None:
        payload = fetch_json(
            _stt_client,
            api_client(),
            _endpoint(bound.base_url),
            headers={"Authorization": f"Bearer {bound.provider.api_key}"},
            json_body=_payload(bound.model, audio, content_type),
        )
        if payload is None:
            return None
        text = _transcript(payload)
        if text is None:
            logger.warning("speech_stt: 响应里没有可用转写（choices[0].message.content）")
        return text

    # 无缓存通道：cache_key 只用于日志/去重语义，不驻留内容
    result = _stt_client.call(f"stt:{bound.model}:{len(audio)}", _load, lane=INTERACTIVE)
    if not result:
        # None 是多义的（R3-F3）：先看 skip 原因再定话术——服务没调通与"录音没内容"
        # 是两回事，混成一句"请重录"会把服务故障的话术成用户的错。
        if _stt_client.last_skip_reason() != "loader_empty":
            raise ApiError(502, "语音服务暂时不可用，请稍后再试")
        # 空转写是这段音频的确定性结果，不是服务故障：走 ValueError 让业务面透出原因，
        # 不落通用 502——"服务暂不可用"只会诱导用户原样重试必败请求。
        raise ValueError("语音没有识别出内容：请重录一段更清晰的语音再试")
    return result
