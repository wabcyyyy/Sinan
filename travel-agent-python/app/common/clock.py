"""产品日期口径（个人 Agent V1）：「今天/明天」一律按 Asia/Shanghai（UTC+8）解释。

规格 §3.2：统一规则与日期判断均使用显式时区时间，禁止直接拿主机 `date.today()`
解释产品窗口——非 CST 主机上"今天"会漂移，照看窗口与当日 AI 限制都会算错。

为什么是固定 +08:00 而不是 `ZoneInfo("Asia/Shanghai")`：IANA tzdata 在 Windows
解释器环境里默认缺失（zoneinfo 依赖系统库或 tzdata 包），而本批规格明确不新增
依赖。中国自 1991 年后无夏令时、偏移恒为 UTC+8，产品窗口只判定当前与未来日期，
固定偏移与 `Asia/Shanghai` 在产品可达的每个日期上完全等价。口径只在此取
（单一真源），别处不得自行另建时区对象。

持久化仍按现有数据库约定存 naive 本地时刻（与 V16 planning_task / V17 cost_ledger
同源：应用侧时钟写入，部署保持应用与库同时区）；时区只决定**哪个日历日**归属
哪个产品窗口，不改变持久化时刻的写法。

依赖：仅标准库（datetime）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

PRODUCT_ZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def shanghai_now() -> datetime:
    """带时区的当前时刻：产品窗口的判定基准（非持久化写法）。"""
    return datetime.now(tz=PRODUCT_ZONE)


def shanghai_today() -> date:
    """上海日历日：照看的「今天」（出发判定、last_ai_attempt_on 的自然日口径）。"""
    return shanghai_now().date()
