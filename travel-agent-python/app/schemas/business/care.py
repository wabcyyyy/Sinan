"""出发前照看与显式长期记忆的线级契约（个人 Agent V1，规格 §4.2/§4.3）。

职责：
- care：详情页照看面板的读/写模型（GET/PUT /api/itinerary/{id}/care），
  有界最近结果（CareCheckResult：逐日观测、受影响项、简短原因、pendingMessageId——
  不复制整份 plans，草稿正文仍在聊天消息与确认图里）；
- memory：显式长期记忆集合的读/写模型（GET/PUT /api/itinerary/preferences/memory），
  只管 source="explicit_memory" 的正负标签。

边界：
- 「当前待确认草稿」的 pending/applied/stale 派生不在本包建模：派生口径是
  消息现状（规格 §4.3），由服务层读取聊天消息后填充 lastResult，避免另建状态机；
- 校验规则（标签 1-32 字符、正负合计 ≤20、监控项 1-20）在服务层以 ApiError 给
  中文文案，本包只声明形状——与 GenerateTripRequest 的既有取舍一致。
"""

from pydantic import BaseModel


class CareUpdateBody(BaseModel):
    """`PUT /api/itinerary/{id}/care` 请求体。

    开启（enabled=true）必须同时显式 allowBackgroundAi=true（后台 AI 费用授权，
    规格 §3.2）；关闭不要求再次授权。watchedItemIds 缺省=不动现有监控范围。
    """

    enabled: bool | None = None
    watchedItemIds: list[int] | None = None
    allowBackgroundAi: bool | None = None


class CareDayObservation(BaseModel):
    """单日观测：预报事实（checkedAt 时刻的查询结果），不是实际天气。"""

    date: str
    precipProb: float | None = None
    weatherCode: int | None = None
    condition: str | None = None
    risk: bool = False


class CareCheckResult(BaseModel):
    """有界最近结果（last_result_json 的线级形状，规格 §4.1）。"""

    checkedAt: str | None = None
    status: str
    reason: str | None = None
    days: list[CareDayObservation] = []
    affectedItemIds: list[int] = []
    pendingMessageId: int | None = None


class CareStateVO(BaseModel):
    """`GET /api/itinerary/{id}/care` 响应；不触发外呼。

    supported=false = 全局开关关闭或行程不具备资格（未完整交付/已出发/已归档等）；
    needsReselect=true = 监控 ID 在行程编辑后全部失效，需重新选择。
    """

    supported: bool
    enabled: bool
    watchedItemIds: list[int] = []
    status: str | None = None
    nextCheckAt: str | None = None
    lastCheckedAt: str | None = None
    lastResult: CareCheckResult | None = None
    needsReselect: bool = False


class CareDismissBody(BaseModel):
    """`POST /api/itinerary/{id}/care/dismiss` 请求体：忽略的是服务端当前
    pendingMessageId（规格 §4.3），客户端不指定对象，故无字段。"""

    pass


class MemoryBody(BaseModel):
    """`PUT /api/itinerary/preferences/memory` 请求体：原子替换显式记忆集合；
    空集合 = 遗忘此入口管理的全部记忆（规格 §3.1.6）。"""

    positive: list[str] = []
    negative: list[str] = []


class MemoryVO(BaseModel):
    """显式长期记忆的只读视图：仅 source="explicit_memory" 的行（历史标签不算）。"""

    positive: list[str] = []
    negative: list[str] = []
