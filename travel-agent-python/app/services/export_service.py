"""PDF 导出任务（移植自 Java `ExportServiceImpl` + `ExportTaskRunner`）。

异步语义照搬：创建即返回 `RUNNING` 的任务，前端轮询 `GET /tasks/{id}` 拿
`downloadUrl`（或订阅 `export_done` 事件）。Java 的渲染走默认 `taskExecutor`
（core4/max8/queue50 + **CallerRunsPolicy**）——池满时不报错，而是退回请求线程
就地渲染（响应变慢但任务不丢）。这里用同一个有界池原语复刻那条兜底路径。

失败终态：`FAILED` + `error_msg`（截 500 字，空消息写「未知错误」），且**异常一律吞掉**，
绝不让后台渲染线程把栈抛到日志外面去。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.common.config import settings
from app.common.envelope import ApiError
from app.common.task_pool import SlotExecutor, TaskRejected
from app.common.vo_json import iso_datetime, iso_time
from app.db.models import BudgetDetail, ItineraryDay, ItineraryItem, ItineraryMain
from app.db.session import session_scope
from app.services import export_pdf, generation_events, itinerary_query

logger = logging.getLogger(__name__)

# Java: taskExecutor core4/max8/queue50 → 入口拦截等价于 58
EXPORT_SLOTS = 8 + 50
MAX_ERROR_LENGTH = 500
UNKNOWN_ERROR = "未知错误"
DOWNLOAD_URL_PREFIX = "/api/export/download/"

export_pool = SlotExecutor("travel-export", 8, EXPORT_SLOTS)


def create_pdf(user_id: int, itinerary_id: int) -> dict[str, Any]:
    with session_scope() as session:
        # 归属校验只用 itinerary_query.require_main 这一份实现（同 Java findOwnedMain 口径）
        itinerary_query.require_main(session, user_id, itinerary_id)
    task_id = _insert_task(user_id, itinerary_id)
    _render_or_inline(task_id)
    return _to_vo(task_id, include_download_url=False)


def _insert_task(user_id: int, itinerary_id: int) -> int:
    from app.db.models import ExportTask

    with session_scope() as session:
        task = ExportTask(itinerary_id=itinerary_id, user_id=user_id, task_type="PDF", status="RUNNING")
        session.add(task)
        session.flush()
        return task.id


def get_task(user_id: int, task_id: int) -> dict[str, Any]:
    status = _require_owned_task(user_id, task_id)
    return _to_vo(task_id, include_download_url=status == "DONE")


def pdf_file(user_id: int, task_id: int) -> Path:
    row = _require_owned_task(user_id, task_id, with_path=True)
    status, path = row
    if status != "DONE" or not path:
        raise ApiError(400, "导出任务尚未完成")
    file = Path(path)
    if not file.exists():
        raise ApiError(404, "导出文件不存在")
    return file


# ---------- 渲染 ----------


def _render_or_inline(task_id: int) -> None:
    try:
        export_pool.submit(render_pdf, task_id)
    except TaskRejected:
        # CallerRuns：任务不丢，代价是这次请求变慢（与 Java 同）
        logger.warning("export pool saturated; rendering task %s inline", task_id)
        render_pdf(task_id)


def render_pdf(task_id: int) -> None:
    try:
        with session_scope() as session:
            task = session.get(_task_model(), task_id)
            if task is None:
                logger.error("export task not found: %s", task_id)
                return
            itinerary_id = task.itinerary_id
            main = session.get(ItineraryMain, itinerary_id)
            model = build_model(main)
        target = Path(settings.export_dir) / f"itinerary_{task_id}.pdf"
        _ensure_font()
        export_pdf.build_pdf(model, target)
        with session_scope() as session:
            task = session.get(_task_model(), task_id)
            if task is None:
                logger.error("export task not found: %s", task_id)
                return
            task.status = "DONE"
            task.file_path = str(target)
            task.finished_at = datetime.now()
        generation_events.export_done(itinerary_id, task_id, "DONE", DOWNLOAD_URL_PREFIX + str(task_id))
        logger.info("export pdf done: task=%s file=%s", task_id, target)
    except Exception as exc:
        logger.error("export pdf failed: task=%s", task_id, exc_info=True)
        with session_scope() as session:
            task = session.get(_task_model(), task_id)
            if task is not None:
                task.status = "FAILED"
                task.error_msg = (str(exc) or UNKNOWN_ERROR)[:MAX_ERROR_LENGTH]
                task.finished_at = datetime.now()


_font_ready = False


def _ensure_font() -> None:
    global _font_ready
    if _font_ready:
        return
    path = Path(settings.export_font_file)
    if not path.exists():
        raise RuntimeError(f"缺少印刷字体 {path}；请放置字体或设置 EXPORT_FONT_FILE")
    export_pdf.register_font(path)
    _font_ready = True


# ---------- 印刷模型 ----------


def build_model(main: ItineraryMain | None) -> dict[str, Any]:
    if main is None:
        raise RuntimeError("行程不存在或已删除")
    with session_scope() as session:
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == main.id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        day_list: list[dict[str, Any]] = []
        for day in days:
            items = (
                session.execute(
                    select(ItineraryItem).where(ItineraryItem.day_id == day.id).order_by(ItineraryItem.sort_no)
                )
                .scalars()
                .all()
            )
            row: dict[str, Any] = {
                "dayNo": day.day_no,
                "note": day.note,
                "travelDate": str(day.travel_date) if day.travel_date else None,
            }
            _add_day_metadata(row, day.metadata_json)
            row["items"] = [
                {
                    "itemType": item.item_type,
                    # 类型与来源都映射成用户能读的词，印刷品不外露内部枚举
                    "typeLabel": export_pdf.type_label(item.item_type),
                    "srcLabel": export_pdf.source_label(item.source),
                    "poiName": item.poi_name,
                    "startTime": iso_time(item.start_time),
                    "endTime": iso_time(item.end_time),
                    "durationMin": item.duration_min,
                    "cost": item.cost,
                    "tag": item.tag,
                    "remark": item.remark,
                    "whyThis": item.why_note,
                    "openTime": item.open_time,
                    "source": item.source,
                }
                for item in items
            ]
            day_list.append(row)
        budgets = session.execute(select(BudgetDetail).where(BudgetDetail.itinerary_id == main.id)).scalars().all()
    budget_list = [{"category": b.category, "amount": b.amount, "itemCount": b.item_count} for b in budgets]
    total = sum((b["amount"] or 0 for b in budget_list), start=0)
    return {
        "title": main.title,
        "city": main.city,
        "days": main.days,
        "persons": main.persons,
        "budget": main.budget,
        "startDate": str(main.start_date) if main.start_date else None,
        "endDate": str(main.end_date) if main.end_date else None,
        "preferences": main.preferences,
        "dayList": day_list,
        "budgetList": budget_list,
        "totalAmount": total,
    }


def _add_day_metadata(row: dict[str, Any], metadata_json: str | None) -> None:
    """元数据损坏也要能出片：只丢可选栏目，不抛。"""
    if not metadata_json or not metadata_json.strip():
        return
    try:
        metadata = json.loads(metadata_json)
    except (ValueError, TypeError):
        logger.warning("day metadata parse failed for day %s; exported without it", row.get("dayNo"))
        return
    if not isinstance(metadata, dict):
        return
    if metadata.get("theme"):
        row["theme"] = metadata["theme"]
    notes = [str(tip) for tip in (metadata.get("practicalNotes") or []) if str(tip).strip()]
    if notes:
        row["practicalNotes"] = notes
    backups = [
        str(item.get("name") or item.get("title") or "")
        for item in (metadata.get("backupPlan") or [])
        if isinstance(item, dict)
    ]
    backups = [name for name in backups if name.strip()]
    if backups:
        row["backupPlan"] = "、".join(backups)


# ---------- 任务表访问 ----------


def _task_model():
    from app.db.models import ExportTask

    return ExportTask


def _to_vo(task_id: int, include_download_url: bool) -> dict[str, Any]:
    with session_scope() as session:
        task = session.get(_task_model(), task_id)
        if task is None:  # 调用方 _require_owned_task 已验过，这里只防跨事务竞态
            raise ApiError(404, "导出任务不存在")
        # 与 Java 的一处有意差异：`createPdf` 里 Java 直接回显刚插入的内存实体，
        # 于是 status 恒为 RUNNING、createdAt 恒为 null（MyBatis-Plus 不回填 server default）。
        # 这里从库里重读，池满走 CallerRuns 就地渲染时首次响应就能拿到 DONE。
        # 前端不受影响：它只用 create 响应的 id，状态一律靠轮询 tasks/{id} 判定。
        vo = {
            "id": task.id,
            "itineraryId": task.itinerary_id,
            "taskType": task.task_type,
            "status": task.status,
            "errorMsg": task.error_msg,
            "createdAt": iso_datetime(task.created_at),
            "finishedAt": iso_datetime(task.finished_at),
        }
    # Java 的 ExportTaskVO 没有 @JsonInclude，Jackson 默认连 null 一起发：
    # downloadUrl 恒在（未完成时为 null），前端类型也声明为 `string | null`。
    vo["downloadUrl"] = DOWNLOAD_URL_PREFIX + str(task_id) if include_download_url else None
    return vo


def _require_owned_task(user_id: int, task_id: int, with_path: bool = False):
    with session_scope() as session:
        task = session.get(_task_model(), task_id)
        if task is None or task.user_id != user_id:
            raise ApiError(404, "导出任务不存在")
        return (task.status, task.file_path) if with_path else task.status
