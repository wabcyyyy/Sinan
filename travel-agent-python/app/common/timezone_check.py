"""启动期时区一致性检查（审查 P1-9）。

僵尸续跑的活跃窗口依赖「应用侧 `datetime.now()` 与 MySQL `updated_at` 同一时区」
（`generation_recovery.py` 的口径注释）。此前这条约束**只有注释没有防线**：
宿主机 +08、容器里 MySQL UTC 时，5 分钟窗口整体平移 8 小时，恢复既可能提前命中
也可能永不到位。

选择 warn 而不是 fail-fast（与审查 R1-Q4 的应答一致）：当前部署恰是「实例 +08 ×
容器 UTC」形态，硬失败会把能跑的现状变成起不来的服务；先用一句警告把问题摆在
日志里，等部署口径统一后再收紧。

本模块独立成文件以便单测（mock 连接即可，不需要活库）。
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def offset_hours(text: str) -> float | None:
    """MySQL 时区串 → UTC 偏移小时数。

    接受 '+08:00' / '-05:30' / 'UTC' / 'SYSTEM'（后者返回 None：加了 SYSTEM 就
    是要求服务端按自己的时区解释，应用侧无从对照）。
    """
    value = (text or "").strip()
    if not value:
        return None
    if value.upper() in ("UTC", "GMT", "+00:00", "-00:00", "0", "00:00"):
        return 0.0
    if value.upper() == "SYSTEM":
        return None
    sign = 1
    body = value
    if body[0] in "+-":
        sign = -1 if body[0] == "-" else 1
        body = body[1:]
    parts = body.split(":")
    if len(parts) != 2:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return sign * (hours + minutes / 60)


def local_utc_offset_hours(now: datetime | None = None) -> float:
    """应用进程的 UTC 偏移小时数（按当前时刻算，兼容夏令时切换）。"""
    moment = now or datetime.now()
    offset = moment.astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else 0.0


def check_timezone_alignment(connection) -> bool:
    """对照 MySQL 会话时区与本地 UTC 偏移；不一致记一条 warning，返回是否一致。

    连接失败/时区读不到一律静默返回 True——这是可观测性检查，不是启动闸门
    （启动期 DB 可能还没就绪，不能因为它把服务拦下）。加定 `SYSTEM` 也视为
    "无从对照"，不报警。
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT @@session.time_zone AS tz")
            row = cursor.fetchone()
    except Exception as exc:  # 启动期检查绝不抛
        logger.debug("timezone check skipped: %s", exc)
        return True
    if not row:
        return True
    raw = row.get("tz") if isinstance(row, dict) else (row[0] if row else None)
    remote = offset_hours(str(raw or ""))
    if remote is None:
        return True
    local = local_utc_offset_hours()
    if abs(remote - local) < 1e-6:
        return True
    logger.warning(
        "timezone mismatch: MySQL session time_zone=%s (UTC%+g) vs app local UTC%+g — "
        "僵尸恢复的 5 分钟活跃窗口会整体平移，请统一两侧时区",
        raw,
        remote,
        local,
    )
    return False


def verify() -> bool:
    """周期任务入口：从连接池取一条连接做对照；取不到连接就静默返回 True。"""
    from app.common import db_pool

    try:
        with db_pool.connection() as conn:
            return check_timezone_alignment(conn)
    except Exception as exc:
        logger.debug("timezone check skipped (no db connection): %s", exc)
        return True
