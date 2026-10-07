"""VO JSON 序列化助手：把 Python 值渲染成与 Java/Jackson 完全一致的字符串。

要点是 Jackson `JavaTimeModule` 的**零分量省略**行为：`LocalTime 09:30` 序列化成
`"09:30"` 而不是 `"09:30:00"`，`LocalDateTime` 同理省略秒。Python 的 `isoformat()`
不省略，前端若按固定宽度切片就会出现显示错位，因此这里显式对齐。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from decimal import Decimal


def parse_time_safe(value: str | time | None) -> time | None:
    """入库时间转换；24:00 归一到 00:00，非法值留空，ORM 时间原样返回。"""
    if isinstance(value, time):
        return value
    if value is None or not (text := value.strip()):
        return None
    if text.startswith("24:"):
        text = "00:" + text[3:]
    if ":" not in text:
        return None
    try:
        return time.fromisoformat(text)
    except ValueError:
        logging.getLogger(__name__).debug("time value unparsable, left empty instead: %s", value)
        return None


def iso_time(value: time | None) -> str | None:
    if value is None:
        return None
    if value.second == 0 and value.microsecond == 0:
        return value.strftime("%H:%M")
    if value.microsecond == 0:
        return value.strftime("%H:%M:%S")
    return value.isoformat()


def iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.second == 0 and value.microsecond == 0:
        return value.strftime("%Y-%m-%dT%H:%M")
    return value.isoformat()


def iso_date(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def number(value: Decimal | int | None) -> float | int | None:
    return None if value is None else float(value)
