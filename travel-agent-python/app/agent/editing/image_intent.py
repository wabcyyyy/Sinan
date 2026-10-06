"""图像意图理解：用户随手拍/截图 → 一段中文描述 + 可直接发给规划助手的一句话。

模型选择口径（2026-10-04 起走注册表）：BYOK 路由生效时用 route.model（用户自选网关
须自带视觉能力，设置页有提示）；否则用 **LLM_ROLE_VISION**（`provider:model`，留空
复用 main）——两条优先级的落点统一由 `app/common/model_registry.py` 解析。图片以
OpenAI 兼容 content 数组 image_url(data URI) 直传——瞬时意图输入不是资产，不落盘
（区别于封面路径，见 services/image_intent.py）。

通道前提：**视觉模型必须真由该通道提供**。主通道是 DeepSeek 官方时，默认的
qwen-vl-plus 在 api.deepseek.com 上并不存在——发过去只换来一次 404，再被上层读成
"图像服务暂不可用"。所以发请求前用 require_model_served 拦下，抛 ValueError 说明是
通道/模型不匹配（业务面转成带原因的 502，用户知道该去配 `LLM_ROLE_VISION` 或 BYOK）。
"""

import json
import logging

from app.common.llm_client import get_role_client, require_model_served
from app.common.model_registry import model_for

logger = logging.getLogger(__name__)

_PROMPT = (
    "你是旅行规划助手的图像理解模块。用户上传一张图片，想把它变成规划对话的起点。"
    "看图后只输出 JSON（不要输出任何其他文字）："
    '{"text":"用一段自然的中文描述图片里与旅行相关的内容（地点/美食/风景/玩法等，'
    '看不出旅行相关性就客观描述图片主体）",'
    '"suggestedMessage":"一句可直接发给旅行规划助手的话（例如想去图中的地方、想吃图里的菜、'
    '想安排图中的活动；中文口语，不超过 50 字）"}'
)
_MAX_CONTEXT_CHARS = 600


def run_image_intent(data_uri: str, context: str = "") -> dict:
    """看图产出 {text, suggestedMessage}；解析失败抛 ValueError（轨向上由 service 转 502）。"""
    client = get_role_client("vision")
    model = model_for("vision")
    # 通道与模型名不匹配（如 DeepSeek 官方通道 + qwen-vl-plus）在本地就响亮报错，
    # 不要用一次注定 404 的往返伪装成"上游不可用"。
    require_model_served(client.base_url, model)
    prompt = _PROMPT
    trimmed = (context or "").strip()
    if trimmed:
        prompt = f"{_PROMPT}\n当前对话背景（仅供理解，不要复述）：{trimmed[:_MAX_CONTEXT_CHARS]}"
    raw = client.chat(
        [
            {
                "role": "user",
                # OpenAI 兼容多模态 content 数组：图在前文本在后（与 dashscope 兼容层一致）
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        temperature=0.2,
        max_tokens=512,
        model=model,
        json_mode=True,
    )
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("image intent parse failed: %s | raw=%s", exc, raw[:200])
        raise ValueError("没能识别这张图片，请换一张试试") from exc
    if not isinstance(data, dict):
        raise ValueError("没能识别这张图片，请换一张试试")
    description = str(data.get("text") or "").strip()
    if not description:
        raise ValueError("没能识别这张图片，请换一张试试")
    # suggestedMessage 缺失/为空时回落描述本身：前端回填输入框永远有内容可填
    suggested = str(data.get("suggestedMessage") or "").strip() or description
    return {"text": description, "suggestedMessage": suggested}
