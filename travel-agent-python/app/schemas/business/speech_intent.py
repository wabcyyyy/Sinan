"""语音意图理解线级契约（G-1.1；端点见 app/api/business/speech_intent.py）。

形状与 ImageIntentVO 刻意一致：前端两条输入腿共用同一套"回填输入框、用户编辑后发送"
的回填逻辑，多一个字段就多一处分叉。
"""

from pydantic import BaseModel


class SpeechIntentVO(BaseModel):
    """`POST /api/speech-intent` 回显：转写文本 + 建议消息（回填输入框，用户编辑后发送）。"""

    text: str
    suggestedMessage: str
