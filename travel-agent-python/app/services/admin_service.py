"""后台管理（移植自 Java `AdminServiceImpl`，端点见 `app/api/business/admin.py`）。

三条容易迁移失真的地方：

1. **分页形状**：Java 直接序列化 MyBatis-Plus 的 `Page`，前端类型只消费
   `{records, total, size, current, pages}`（`api/admin.ts`），`pages` 按
   `size==0 ? 0 : ceil(total/size)` 算，这里同式；`page=0` 会算出负 offset
   并让数据库报错——Java 也没有做任何钳制，因此这里同样不加"贴心"的默认值。
2. **totalUsers 取自增 ID 最大值**而不是行数：这是 Java 有意的口径（包含被物理删除的
   账号，反映真实注册规模），改成 `COUNT(*)` 会让仪表盘数字变小。
3. **Agent 指标与 LLM 用量原先是跨进程 HTTP**：Java 通过 `AgentService` 打
   `/api/agent/v1/{metrics,usage}`，失败时回一份"全 0 + agentAvailable=false"的兜底。
   迁移后是同一个进程，取数直接走 `metrics.snapshot()` / `usage_store.report()`；
   兜底分支保留——用量落在 SQLite，磁盘层故障是真实可能，前端要靠 `agentAvailable`
   显示降级横幅。刻意不移植的是 `circuitBreaker` 字段：它衡量的是"Java 调 Python"
   这一跳的健康度，单进程后这一跳不存在，前端也没有读它。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import func, or_, select

from app.agent import metrics, usage_store
from app.common.envelope import ApiError
from app.common.vo_json import iso_date, iso_datetime, number
from app.db.models import ItineraryMain, SysUser
from app.db.session import session_scope
from app.services import itinerary_command

logger = logging.getLogger(__name__)

ALLOWED_USER_STATUS = (0, 1)


def stats() -> dict[str, Any]:
    """概览七项；`deleted=0` 由会话级全局作用域追加（等价 @TableLogic）。"""
    # 今日零点：与 Java 的 LocalDate.now().atStartOfDay() 同源（应用本地时区，不是 UTC）
    today_start = datetime.combine(date.today(), time.min)
    with session_scope() as session:
        max_id = session.execute(select(func.coalesce(func.max(SysUser.id), 0))).scalar_one()
        return {
            "totalUsers": int(max_id),
            "activeUsers": _count(session, select(SysUser).where(SysUser.status == 1)),
            "disabledUsers": _count(session, select(SysUser).where(SysUser.status == 0)),
            "totalItineraries": _count(session, select(ItineraryMain)),
            "todayNewUsers": _count(session, select(SysUser).where(SysUser.created_at >= today_start)),
            "todayNewItineraries": _count(
                session, select(ItineraryMain).where(ItineraryMain.created_at >= today_start)
            ),
            "generatingItineraries": _count(session, select(ItineraryMain).where(ItineraryMain.status == 1)),
        }


def _count(session, statement) -> int:
    """把已带作用域的 SELECT 包成计数：软删过滤留在内层，不在此处重写条件。"""
    return int(session.execute(select(func.count()).select_from(statement.subquery())).scalar_one())


def page_users(page: int, size: int, keyword: str | None) -> dict[str, Any]:
    with session_scope() as session:
        statement = select(SysUser)
        if keyword and keyword.strip():
            # 与 MyBatis-Plus 的 like 同形：'%kw%'，不做通配符转义
            statement = statement.where(
                or_(SysUser.username.like(f"%{keyword}%"), SysUser.nickname.like(f"%{keyword}%"))
            )
        total = _count(session, statement)
        rows = (
            session.execute(statement.order_by(SysUser.created_at.desc()).limit(size).offset((page - 1) * size))
            .scalars()
            .all()
        )
        counts = _itinerary_counts(session, [user.id for user in rows])
        records = [
            {
                "id": user.id,
                "username": user.username,
                "nickname": user.nickname,
                "phone": user.phone,
                # 本仓把 status 映射成 Boolean、Java 侧是 Integer，仪表盘要的是 1/0；
                # 历史行允许 NULL（登录侧同样把 NULL 当正常），int() 前先判空
                "status": None if user.status is None else int(user.status),
                "role": user.role,
                "itineraryCount": counts.get(user.id, 0),
                "createdAt": iso_datetime(user.created_at),
            }
            for user in rows
        ]
    return _page(total, page, size, records)


def _itinerary_counts(session, user_ids: list[int]) -> dict[int, int]:
    """一页用户的行程数一次 group by 拿齐（Java `fillItineraryCounts` 同形，避免逐行 N+1）。"""
    if not user_ids:
        return {}
    rows = session.execute(
        select(ItineraryMain.user_id, func.count())
        .where(ItineraryMain.user_id.in_(user_ids))
        .group_by(ItineraryMain.user_id)
    ).all()
    return {int(owner): int(count) for owner, count in rows if owner is not None}


def update_status(target_user_id: int, status: int | None, operator_username: str) -> None:
    if status not in ALLOWED_USER_STATUS:
        raise ApiError(400, "非法的状态值")
    with session_scope() as session:
        user = _require_user(session, target_user_id)
        if user.username == operator_username:
            raise ApiError(400, "不能操作自己的账号")
        user.status = status


def delete_user(target_user_id: int, operator_username: str) -> None:
    with session_scope() as session:
        user = _require_user(session, target_user_id)
        if user.username == operator_username:
            raise ApiError(400, "不能删除自己的账号")
        user.deleted = 1  # @TableLogic：deleteById 是 UPDATE deleted=1


def _require_user(session, target_user_id: int) -> SysUser:
    user = session.get(SysUser, target_user_id)
    if user is None:
        raise ApiError(404, "用户不存在")
    return user


def page_itineraries(
    page: int, size: int, keyword: str | None, status: int | None, user_id: int | None
) -> dict[str, Any]:
    with session_scope() as session:
        statement = select(ItineraryMain)
        if user_id is not None:
            statement = statement.where(ItineraryMain.user_id == user_id)
        if keyword and keyword.strip():
            statement = statement.where(
                or_(ItineraryMain.title.like(f"%{keyword}%"), ItineraryMain.city.like(f"%{keyword}%"))
            )
        if status is not None:
            statement = statement.where(ItineraryMain.status == status)
        total = _count(session, statement)
        rows = (
            session.execute(statement.order_by(ItineraryMain.created_at.desc()).limit(size).offset((page - 1) * size))
            .scalars()
            .all()
        )
        records = [
            {
                "id": row.id,
                "userId": row.user_id,
                "title": row.title,
                "city": row.city,
                "startDate": iso_date(row.start_date),
                "endDate": iso_date(row.end_date),
                "days": row.days,
                "persons": row.persons,
                "budget": number(row.budget),
                "status": row.status,
                "createdAt": iso_datetime(row.created_at),
            }
            for row in rows
        ]
    return _page(total, page, size, records)


def _page(total: int, page: int, size: int, records: list[dict[str, Any]]) -> dict[str, Any]:
    # MyBatis-Plus Page.getPages()：size 为 0 时不除零
    pages = 0 if size == 0 else total // size + (0 if total % size == 0 else 1)
    return {"records": records, "total": total, "size": size, "current": page, "pages": pages}


def delete_itinerary(itinerary_id: int) -> None:
    """管理员删行程：不做版本快照（快照依赖归属校验，管理员非属主），级联口径与用户侧同源。"""
    with session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        if main is None:
            raise ApiError(404, "行程不存在")
        owner_id = main.user_id
    # Java 侧靠 @CacheEvict(allEntries=true) 清整片详情缓存；这里按属主精确失效同一份键
    itinerary_command.delete_cascade(owner_id, itinerary_id)


def agent_metrics() -> dict[str, Any]:
    try:
        data = dict(metrics.snapshot())
        data["agentAvailable"] = True
        return data
    except Exception as exc:
        logger.warning("fetch agent metrics failed: %s", exc)
        return {
            "agentAvailable": False,
            "runs": 0,
            "successes": 0,
            "failures": 0,
            "degraded_runs": 0,
            "llm_calls": 0,
            "tool_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "success_rate": 0.0,
            "failure_rate": 0.0,
            "degraded_rate": 0.0,
            "avg_event_latency_ms": 0.0,
            "recent_failures": [],
        }


def llm_usage(range_key: str, limit: int, offset: int) -> dict[str, Any]:
    try:
        payload = usage_store.report(range_key, limit, offset)
        payload["agentAvailable"] = True
        return payload
    except Exception as exc:
        logger.warning("fetch llm usage failed: %s", exc)
        # 兜底里 range 原样回显入参（成功路径才是归一化后的键），bucket 恒为 3600：
        # 这两条是 Java 的既有形状，前端只按 agentAvailable 判降级，别顺手"修好"它
        return {
            "agentAvailable": False,
            "range": range_key or "24h",
            "bucket": 3600,
            "summary": {
                "calls": 0,
                "successes": 0,
                "failures": 0,
                "success_rate": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "avg_duration_ms": 0.0,
            },
            "by_scene": [],
            "by_model": [],
            "timeline": [],
            "calls": {"total": 0, "records": []},
        }
