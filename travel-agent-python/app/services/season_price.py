"""酒店季节定价：必须与 `app/common/season.py`（Agent 侧）和 Java `SeasonPrice` 三方同规则。

规则是"日历窗口"而非真实节假日表：春节窗口 1/24-2/8、五一 4/30-5/5、国庆 9/30-10/7。
任何一侧改动都会让预算合计与 Agent 生成的报价对不上，故三个实现都要同步。
"""

from __future__ import annotations

from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal

PEAK_FACTOR = Decimal("1.8")
SUMMER_FACTOR = Decimal("1.5")
OFF_FACTOR = Decimal("0.85")
NORMAL_FACTOR = Decimal("1")

_HOLIDAY_WINDOWS = ((time(1, 24), time(2, 8)), (time(4, 30), time(5, 5)), (time(9, 30), time(10, 7)))


def _month_day(value: date) -> time:
    # 用 time(month, day) 当 MonthDay 用：只比较月/日，且能正确处理跨年窗口外的比较
    return time(value.month, value.day)


def _in_holiday(value: date) -> bool:
    md = _month_day(value)
    return any(start <= md <= end for start, end in _HOLIDAY_WINDOWS)


def factor(start_date: date | None = None) -> Decimal:
    day = start_date or date.today()
    if _in_holiday(day):
        return PEAK_FACTOR
    if day.month in (7, 8):
        return SUMMER_FACTOR
    if day.month in (12, 1, 2):
        return OFF_FACTOR
    return NORMAL_FACTOR


def label(start_date: date | None = None) -> str:
    day = start_date or date.today()
    if _in_holiday(day):
        return "节假日旺季"
    if day.month in (7, 8):
        return "暑期旺季"
    if day.month in (12, 1, 2):
        return "淡季"
    return "平季"


def apply(base: Decimal, start_date: date | None = None) -> Decimal:
    return (Decimal(base) * factor(start_date)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
