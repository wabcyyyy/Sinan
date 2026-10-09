"""行程生成进度事件（事件类型表移植自 Java `ItineraryEventPublisher`）。

信封 `{type, itineraryId, seq, ts, data}` 与通道 `gen:events:{itineraryId}` 由
`app/common/event_publisher.py` 负责；本模块只钉 `data` 的**键名与取值口径**——
前端按这些键渲染进度 UI，改一个键名就是前端少一格进度（双跑期曾由 Java SSE
网关转发，M7 起订阅方与发布方同进程）。

事件是尽力而为：DB 才是真相，发布失败只记日志（见 `publish_event`）。

AILIVE-3 分域登记口径（2026-10-06）：本模块 + `itinerary_events.py`（terminal_snapshot/
心跳/终态补帧）构成**业务面进度 SSE**（`GET /api/itinerary/{id}/events`）的运行时帧集，
与 `app/schemas/stream_events.py` 登记的 **agent 面 generate-stream JSONL 契约**是两个
消费域：后者有 schema 导出与 drift 门禁，前者以本文件为单一真源、前端以开放 `type:
string` 消费（sinan.ts ItineraryStreamEvent），互不冒充。新增业务面帧类型必须同时改：
本模块的 emitter、前端 useHomePlanning/TripDetailPage 的消费分支、以及下表。

业务面运行时帧类型全表（改动时同步维护）：
  进度：research_start / research_done / day_start / day_done / butler_note / complete
  候选（M5a，spec §10.2）：item_preview / item_preview_withdrawn / day_preview_mapping
  里程碑（M5b，spec §11）：core_ready（主行程可查看、备选富化中的 DB 权威中间态）
  指标（M5b，spec §11）：stage_timing（生成阶段分段耗时，观测用，前端可忽略）
  降级与错误：degraded / error（AGENT_ERROR 等码在 data 层）
  会话层（event_publisher）：heartbeat / error_envelope（含 AGENT_BUSY）
  导出：export_done
"""

from __future__ import annotations

from typing import Any

from app.common.event_publisher import publish_event


def day_start(itinerary_id: int, day_no: int) -> None:
    publish_event(itinerary_id, "day_start", {"dayNo": int(day_no)})


def day_done(itinerary_id: int, day_no: int, theme: str | None, item_count: int, note: str | None) -> None:
    publish_event(
        itinerary_id,
        "day_done",
        {
            "dayNo": int(day_no),
            "theme": theme,
            "itemCount": int(item_count),
            "note": note,
        },
    )


def item_preview(itinerary_id: int, run_id: str, day_no: int, item_ordinal: int, item: dict[str, Any]) -> None:
    """逐项候选预览（M5a）：单个 item 候选闭合即发，浏览器先亮「正在完善」。

    候选身份 = runId + dayNo + itemOrdinal（服务端分配，与 LLM chunk 切割无关），
    previewId = "runId:dayNo:itemOrdinal" 供消费端定位替换/移除。`item` 是开放
    形状（候选未过 ground，坐标/关键事实保持未知）；随后同一天的正式 day 快照
    到达即整体替换该日。runId 同时落当前 trace（事件↔行程↔轨迹三向对账），
    与 agent 面契约的 runId 同源。
    """
    rid = str(run_id)
    publish_event(
        itinerary_id,
        "item_preview",
        {
            "runId": rid,
            "previewId": f"{rid}:{int(day_no)}:{int(item_ordinal)}",
            "dayNo": int(day_no),
            "itemOrdinal": int(item_ordinal),
            "item": item,
        },
        run_id=rid or None,
    )


def item_preview_withdrawn(itinerary_id: int, run_id: str, day_no: int, item_ordinal: int, reason: str) -> None:
    """逐项候选撤回（M5a）：整段生成腿失败/降级后已发布候选显式失效。

    消费端按 previewId 居右移除该候选，不能留在板上，也不能静默变成另一地点
    （缺口天由逐日兜底重生成，随后正式 day 快照覆盖）。
    """
    rid = str(run_id)
    publish_event(
        itinerary_id,
        "item_preview_withdrawn",
        {
            "runId": rid,
            "previewId": f"{rid}:{int(day_no)}:{int(item_ordinal)}",
            "dayNo": int(day_no),
            "itemOrdinal": int(item_ordinal),
            "reason": str(reason),
        },
        run_id=rid or None,
    )


def day_preview_mapping(itinerary_id: int, run_id: str, day_no: int, mappings: list[dict[str, Any]]) -> None:
    """previewId→itemId 映射（M5a spec §10.2「持久化后提供映射」的收口帧）。

    流式天落库后，把该日候选按 **poi_name** 对到正式条目：预览与正式快照之间
    有反驳剔除/排程重排，**序号不保真**，对应关系只能在持久化时刻按内容身份
    判定。`mappings` 每项 {previewId, itemId, poiName}；没对上的候选 = 被后
    处理淘汰（缺席即如实陈述，不发伪 withdrawn）。映射只在生成进行中有意义
    （预览不落库，刷新重建后只有正式条目，映射自然消失）。
    """
    rid = str(run_id)
    publish_event(
        itinerary_id,
        "day_preview_mapping",
        {
            "runId": rid,
            "dayNo": int(day_no),
            "mappings": [dict(entry) for entry in mappings],
        },
        run_id=rid or None,
    )


def core_ready(itinerary_id: int, revision: int, days_emitted: int) -> None:
    """核心就绪里程碑（M5b，spec §11）：主行程已落库且整趟硬规则通过，可查看/可继续编辑，
    备选富化仍在进行。`revision` = 发布时的 planning_revision（DB 现值，消费端编辑
    与在途富化以此为隔离边界）；`daysEmitted` 来自 DB 的 SUCCEEDED 天数，不从
    事件流推导。与 complete 的边界：complete 的收尾含义不变，core_ready 只是
    中间里程碑（前端「主行程已可查看」提示的权威信号，消费归下一批）。
    """
    publish_event(
        itinerary_id,
        "core_ready",
        {
            "revision": int(revision),
            "daysEmitted": int(days_emitted),
        },
    )


def stage_timing(itinerary_id: int, stage: str, elapsed_ms: float) -> None:
    """生成阶段分段耗时（M5b，spec §11 指标）：submit 起各里程碑的 monotonic 墙钟。

    只记录、不断言（历史 152 秒不设基线）；不承载任何用户可见状态，前端可忽略。
    """
    publish_event(
        itinerary_id,
        "stage_timing",
        {
            "stage": str(stage),
            "elapsedMs": max(round(float(elapsed_ms)), 0),
        },
    )


def degraded(itinerary_id: int, scope: str, reason: str | None, fallback: str) -> None:
    """降级通知。`scope` 形状固定：`day_{n}` / `research` / `butler` / `poi_intros`。"""
    publish_event(
        itinerary_id,
        "degraded",
        {
            "scope": scope,
            "reason": reason,
            "fallback": fallback,
        },
    )


def error(itinerary_id: int, code: str, message: str | None, retryable: bool) -> None:
    publish_event(
        itinerary_id,
        "error",
        {
            "code": code,
            "message": message,
            "retryable": bool(retryable),
        },
    )


def complete(
    itinerary_id: int, status: str, day_count: int | None, degraded_days: list[int], version_id: int | None
) -> None:
    """终态事件：前端据此停止监听。`status` 只有 COMPLETED / PARTIAL 两值。"""
    publish_event(
        itinerary_id,
        "complete",
        {
            "status": status,
            "dayCount": day_count,
            "degradedDays": [int(day_no) for day_no in (degraded_days or [])],
            "versionId": version_id,
        },
    )


def butler_note(itinerary_id: int, length: int, preview: str) -> None:
    """管家讲解写回成功。事件体只带长度与 ≤60 字预览，不携带长文本。"""
    payload: dict[str, Any] = {"length": int(length), "preview": preview}
    publish_event(itinerary_id, "butler_note", payload)


def export_done(itinerary_id: int, task_id: int, status: str, download_url: str) -> None:
    """PDF 导出完成：前端可据此停止轮询 `GET /api/export/tasks/{id}`。"""
    publish_event(
        itinerary_id,
        "export_done",
        {
            "taskId": int(task_id),
            "status": status,
            "downloadUrl": download_url,
        },
    )
