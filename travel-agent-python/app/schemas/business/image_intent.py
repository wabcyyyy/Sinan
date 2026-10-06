"""图像意图理解线级契约（G-1.1；端点见 app/api/business/image_intent.py）。"""

from pydantic import BaseModel


class ImageIntentVO(BaseModel):
    """`POST /api/image-intent` 回显：描述 + 建议消息（回填输入框，用户编辑后发送）。"""

    text: str
    suggestedMessage: str
