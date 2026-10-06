"""图像意图理解业务面：`POST /api/image-intent` 的服务层（对话输入的图片入口）。

流水线（2026-10-02 拍板）：配额闸（与 /api/pois 同口径，先过闸再干活）→
`cover_service.read_upload_capped` 流式 5MB 截断（413）→ Pillow 解码校验
（仅 JPEG/PNG/WebP，坏图 400）→ 长边 2048 + JPEG q85 重编码（顺带剥 EXIF、
控 payload）→ base64 data URI **内存直传** agent 层（瞬时意图输入不是资产，
不落盘，免上传目录治理）。模型通道：用户 BYOK 网关优先（route_scope），
无路由回落 `LLM_ROLE_VISION` 角色，见 agent/editing/image_intent.py。
"""

from __future__ import annotations

import base64
import io
import logging

from fastapi import UploadFile
from PIL import Image

from app.agent import run_image_intent, use_scene
from app.common.envelope import ApiError
from app.services import cover_service, llm_gateway_service, quota_service
from app.services.itinerary_city import guard_agent_call

logger = logging.getLogger(__name__)

IMAGE_INTENT_MAX_BYTES = 5 * 1024 * 1024
IMAGE_INTENT_MAX_DIM = 2048
IMAGE_INTENT_JPEG_QUALITY = 85
# 显式像素上限（2026-10-02 终审）：5MB 压缩限流拦不住像素炸弹——Pillow 只在
# ~1.78 亿像素才抛 DecompressionBombError，~8950 万以下静默解码，RGBA 大图
# convert("RGB") 峰值内存可达 ~1GB。40MP 覆盖主流相机直出（≤61MP 顶级机身
# 需自行缩图），把峰值压掉一个量级；判定在 convert/thumbnail 放大内存之前。
IMAGE_INTENT_MAX_PIXELS = 40_000_000
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def interpret(user_id: int, file: UploadFile, context: str | None = None) -> dict:
    """图片 → {text, suggestedMessage}；输出回填输入框交用户编辑，不自动发送。"""
    quota_service.enforce_llm_budget(user_id)
    data = cover_service.read_upload_capped(file.file, IMAGE_INTENT_MAX_BYTES)
    data_uri = _to_data_uri(data)
    with llm_gateway_service.route_scope(user_id), use_scene("assist"):
        # guard_agent_call：上游异常 → 502 通用文案；自己的 ValueError → 502 带原因
        result = guard_agent_call("图像识别服务暂不可用", lambda: run_image_intent(data_uri, context or ""))
    return result


def _to_data_uri(data: bytes) -> str:
    """解码校验 + 缩放重编码（剥 EXIF）→ JPEG data URI。"""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:
        raise ApiError(400, "图片文件已损坏或不是有效图片") from exc
    if (image.format or "").upper() not in _ALLOWED_FORMATS:
        raise ApiError(400, "仅支持 jpeg/png/webp 图片")
    if image.width * image.height > IMAGE_INTENT_MAX_PIXELS:
        raise ApiError(400, "图片分辨率过高，请压缩后重试")
    rgb = image.convert("RGB")  # 顺带丢掉 alpha/调色板，重编码为 JPEG
    rgb.thumbnail((IMAGE_INTENT_MAX_DIM, IMAGE_INTENT_MAX_DIM))
    buffer = io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=IMAGE_INTENT_JPEG_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
