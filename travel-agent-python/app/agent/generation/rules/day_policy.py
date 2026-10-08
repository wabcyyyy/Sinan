"""DayPolicy：按有效需求与 day_no 派生的单日规则（M3，spec §8.2）。

职责：日类型（full/arrival/departure）× 明确窗口 × 慢游 → 当日的时长下限/
上限/景点数基线/无景点合法性，供 prompt、优化器、reflect、delivery gate
同用（单一真源，不引入新层）。

口径（spec 原文）：
- 普通全天且无新约束：保留 240/480 分钟与景点数量基线（reflect 既有常量）；
- 到达日/返程日：按明确可用窗口验收，不要求凑满 240 分钟；可用时间不足时
  "抵达/返程安排"是合法日类型（无景点不算伤），不冒充普通景点日；
- 慢游（pace=relaxed）：仅降低景点数上限（给休息留白），不全局下降时长阈值，
  不捏造身体条件；
- 到达/出发地点不明：用城市时间边界 + 标明 estimated 的预留默认（到达日
  默认 14:00 起可用、返程日默认 18:00 前结束）；不能假装知道机场/车站，
  硬保证仅针对用户明确给出的窗口（has_known_window）。

依赖：app.schemas；无 agent 层依赖（可被 rules/output 双侧消费）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.trip_requirements import TripRequirements

# ---- 单日形状常量（M3 真源下沉到 rules 最底层，原 reflect.py 定义迁此）----
MAX_DAILY_MINUTES = 480
MAX_DAILY_ATTRACTIONS = 6
# 白天有效活动窗口：约 09:00-19:00；排程过稀时要求回填
MIN_ACTIVE_MINUTES = 240

#: 到达/出发地点不明时的城市时间边界预留估算（分钟）；标 estimated，不是时刻表
ARRIVAL_DEFAULT_START_MIN = 14 * 60
DEPARTURE_DEFAULT_END_MIN = 18 * 60
#: 窗口可用时长低于该值时，"抵达/返程安排"（无景点）是合法日类型
_MIN_PLAYABLE_MINUTES = 120
#: 慢游日的景点数上限（给休息与步行策略留白；不降时长阈值）
_RELAXED_MAX_ATTRACTIONS = 4


@dataclass(frozen=True)
class DayPolicy:
    """单日验收与生成共用的规则快照。"""

    day_no: int
    kind: str  # full / arrival / departure
    #: 明确窗口（用户给出）；None = 未给出（arrival/departure 用预留估算，标 estimated）
    window_start_min: int | None
    window_end_min: int | None
    has_known_window: bool
    min_active_minutes: int
    max_daily_minutes: int
    max_attractions: int
    relaxed: bool
    #: 可用游玩时间是否足以安排景点（不足时"抵达/返程安排"为合法日类型）
    playable: bool

    @property
    def is_transit_day(self) -> bool:
        return self.kind in ("arrival", "departure")


def _window_of(requirements: TripRequirements | None, day_no: int) -> tuple[str, int | None, int | None, bool]:
    """取该日窗口：kind / start / end / 是否用户明确给出。"""
    if requirements is not None:
        for window in requirements.day_windows:
            if window.day_no == day_no:
                start = None
                end = None
                if window.not_before:
                    h, m = window.not_before.split(":")
                    start = int(h) * 60 + int(m)
                if window.finish_by:
                    h, m = window.finish_by.split(":")
                    end = int(h) * 60 + int(m)
                return window.kind, start, end, start is not None or end is not None
    return "full", None, None, False


def day_policy_for(requirements: TripRequirements | None, day_no: int) -> DayPolicy:
    """按结构化需求派生当日规则；无需求/无窗口 = 普通全天（现状基线不变）。"""
    kind, start, end, known = _window_of(requirements, day_no)
    relaxed = bool(requirements and requirements.pace == "relaxed")
    max_attractions = _RELAXED_MAX_ATTRACTIONS if relaxed else MAX_DAILY_ATTRACTIONS
    if kind == "full":
        return DayPolicy(
            day_no=day_no,
            kind=kind,
            window_start_min=start,
            window_end_min=end,
            has_known_window=known,
            min_active_minutes=MIN_ACTIVE_MINUTES,
            max_daily_minutes=MAX_DAILY_MINUTES,
            max_attractions=max_attractions,
            relaxed=relaxed,
            playable=True,
        )
    # 到达/返程日：明确窗口优先；地点不明用城市边界预留估算（estimated）
    start_min = start if start is not None else (ARRIVAL_DEFAULT_START_MIN if kind == "arrival" else None)
    end_min = end if end is not None else (DEPARTURE_DEFAULT_END_MIN if kind == "departure" else None)
    if start_min is not None and end_min is not None:
        available = max(end_min - start_min, 0)
    elif kind == "arrival" and start_min is not None:
        # 只给到达钟点：可玩到当日城市边界（24:00）——时长上限是分钟数不是钟点，
        # 不能拿 480 当 08:00 减（那会把下午落地判成负可用时长）
        available = max(24 * 60 - start_min, 0)
    elif end_min is not None:
        # 只给出发钟点：从当日 0:00 起可用
        available = end_min
    else:
        available = MAX_DAILY_MINUTES
    return DayPolicy(
        day_no=day_no,
        kind=kind,
        window_start_min=start_min,
        window_end_min=end_min,
        has_known_window=known,
        # 到离日不要求凑满 240：按明确可用窗口验收
        min_active_minutes=0,
        max_daily_minutes=min(MAX_DAILY_MINUTES, available) if available else MAX_DAILY_MINUTES,
        max_attractions=max_attractions,
        relaxed=relaxed,
        playable=available >= _MIN_PLAYABLE_MINUTES,
    )
