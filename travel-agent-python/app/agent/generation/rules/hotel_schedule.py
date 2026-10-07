"""住宿实体与每日入住排程的纯规则。

跨模块 API：hotel_for_day，为住宿摊铺构造每日副本。
"""

import copy
from typing import Any


def hotel_for_day(source: dict, local: dict | None, items: list[dict]) -> dict:
    """住宿实体与每日排程分开：换同店不换时间，漏排晚次接在当天活动后。"""
    schedule_fields = {"id", "sort_no", "start_time", "end_time", "duration_min"}
    clone = copy.deepcopy({key: value for key, value in source.items() if key not in schedule_fields})
    if local is not None:
        clone.update({key: local[key] for key in schedule_fields if key in local})
    else:
        ends = [_clock_minutes(item.get("end_time")) for item in items if item.get("item_type") != "hotel"]
        last_end = max((end for end in ends if end is not None), default=None)
        if last_end is not None:
            # 这是当天入住/返回住宿的标记，不把整晚睡眠建成活动。
            finish = min(last_end + 30, 24 * 60)
            clone.update(
                start_time=f"{last_end // 60:02d}:{last_end % 60:02d}",
                end_time=f"{finish // 60:02d}:{finish % 60:02d}",
                duration_min=finish - last_end,
            )
    return clone


def _clock_minutes(value: Any) -> int | None:
    try:
        hour, minute = str(value).split(":")[:2]
        hour, minute = int(hour), int(minute)
    except (ValueError, TypeError):
        return None
    return hour * 60 + minute if 0 <= hour <= 24 and 0 <= minute < 60 and (hour < 24 or minute == 0) else None
