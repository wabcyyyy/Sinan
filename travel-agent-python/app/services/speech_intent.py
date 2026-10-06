"""语音意图理解业务面：`POST /api/speech-intent` 的服务层（对话输入的语音入口）。

流水线（2026-10-04，与 image_intent 同构）：配额闸（先过闸再干活）→ 流式读取上限
（超限 413）→ 音频格式白名单（400）→ `app.common.speech_client.transcribe`（stt 角色）。
识别结果只回填输入框交用户编辑，**不自动发送**。

为什么服务端 ASR 而不是继续只靠浏览器 Web Speech：Web Speech 依赖浏览器厂商的在线
服务（国内网络下经常不可用），失败时用户只看到"语音服务暂时连不上"。服务端走 MiMo 后
可用性掌握在自己手里；前端保留 Web Speech 作为降级（见 useSpeechInput）。
"""

from __future__ import annotations

import logging

from fastapi import UploadFile

from app.agent import use_scene
from app.common.envelope import ApiError
from app.common.speech_client import transcribe
from app.services import cover_service, llm_gateway_service, quota_service
from app.services.itinerary_city import guard_agent_call

logger = logging.getLogger(__name__)

SPEECH_INTENT_MAX_BYTES = 8 * 1024 * 1024
#: MiMo 只收 wav/mp3（2026-10-04 实证，见 speech_client 线级口径）；前端录音已转
#: 16k 单声道 WAV 再上传。换 stt 供应商时同步改这里——白名单就是供应商实收格式。
_ALLOWED_AUDIO_TYPES = {"audio/wav", "audio/x-wav", "audio/mpeg"}


def interpret(user_id: int, file: UploadFile) -> dict:
    """录音 → {text, suggestedMessage}：形状与图像入口一致，前端共用同一套回填逻辑。

    刻意不收 `context`（图像入口的对话背景）：ASR 不做"按上下文改词"，收一个用不上的
    参数只会让人以为它生效。
    """
    quota_service.enforce_llm_budget(user_id)
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _ALLOWED_AUDIO_TYPES:
        raise ApiError(400, "仅支持 wav/mp3 音频（浏览器录音会自动转 WAV）")
    data = cover_service.read_upload_capped(file.file, SPEECH_INTENT_MAX_BYTES)
    if not data:
        raise ApiError(400, "音频文件为空")
    with llm_gateway_service.route_scope(user_id), use_scene("assist"):
        # guard_agent_call：上游异常 → 502 通用文案；自己的 ValueError → 502 带原因
        text = guard_agent_call(
            "语音识别服务暂不可用",
            lambda: transcribe(data, content_type=content_type),
        )
    return {"text": text, "suggestedMessage": text}
