"""管理员读取的持久规划任务状态；不包含旅行内容或执行令牌。"""

from app.schemas.common import WireModel


class PlanningTerminalVO(WireModel):
    itinerary_id: int
    attempt: int
    state: str
    result_code: str | None
    updated_at: str


class PlanningQueueVO(WireModel):
    observed_at: str
    capacity: int
    live_tasks: int
    waiting_tasks: int
    claimed_tasks: int
    running_tasks: int
    oldest_wait_seconds: float | None
    expired_leases: int
    overdue_tasks: int
    recent_since: str
    recent_terminals: list[PlanningTerminalVO]
