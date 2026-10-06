"""图检查点存储（PR-3 / D2）：langgraph-checkpoint-sqlite 的进程级单例 + 落盘与清理约定。

D2 定案：dev/单机用 `SqliteSaver`（SQLite 文件库，自带建表——**不动 MySQL 迁移**，
INV-3 不受影响）；生产路径预留 PostgresSaver（同一 `BaseCheckpointSaver` 协议，
换 saver 即换存储介质，恢复语义不变）。

- **thread_id = 生成任务标识**（`day-{itinerary_id}-{day_no}` 这类 action_id）截断
  ≤255 字符（LangGraph checkpoints 表 thread_id 列宽坑）。刻意不用随机 UUID：
  进程重启后随机 id 无处可找，"从 checkpoint 续跑"就无从谈起——除非给 MySQL 加
  任务列，而 PR-3 改动面明确不动迁移；确定性任务标识是"可找回"的唯一免列方案。
- **反序列化白名单**（`allowed_msgpack_modules`）：只放行本仓状态定义里的类型
  （`UnifiedAgentState` 的字段类型，见 `research/agent_state.py`），checkpoint 库
  被污染也不会执行任意代码（langgraph-checkpoint-sqlite 的 Security 告示）；
  `pickle_fallback` 保持关闭。状态新增字段类型时同步补白名单。
- **保留期清理**：checkpoints 无限增长是 LangGraph 官方告警——`cleanup_old_threads`
  由 main.py 的 cron 每天调一次（对齐 usage 清理的节奏）。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver

from app.common.config import settings

#: checkpoints 表 thread_id 列宽（LangGraph 官方口径）：任务标识一律截断到此长度。
THREAD_ID_MAX = 255

#: 反序列化白名单：(模块, 类型名)——与 UnifiedAgentState 的字段类型一一对应。
_ALLOWED_MSGPACK_MODULES: tuple[tuple[str, str], ...] = (
    ("app.schemas.trip", "GenerateRequest"),
    ("app.schemas.trip", "GenerateDayRequest"),
    ("app.schemas.trip", "DailyPlan"),
    ("app.schemas.trip", "TripItem"),
    ("app.schemas.trip", "Suggestion"),
    ("app.schemas.trip", "GenerateResponse"),
    ("app.schemas.trip", "ChatTurnResponse"),
    ("app.agent.research.evidence", "ResearchTask"),
    ("app.agent.research.evidence", "EvidencePack"),
)

_lock = threading.Lock()
_saver: SqliteSaver | None = None


def get_checkpointer() -> SqliteSaver:
    """进程级检查点单例（SqliteSaver 内部自带锁；sqlite 连接关掉跨线程检查）。

    图在编译期绑住这个对象（`compile(checkpointer=...)`），因此它是**唯一实例**；
    数据面与 `runtime/usage_store` 同款：SQLite 文件由 `settings.checkpoint_db_path`
    决定（data/ 目录缺失时自动建，fresh clone 即可启动）。测试按 action_id 用确定性
    thread，重复跑同 thread = 新 run 覆盖旧 run（day 状态全量重置），互不串味。
    """
    global _saver
    with _lock:
        if _saver is None:
            Path(settings.checkpoint_db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(settings.checkpoint_db_path, check_same_thread=False)
            _saver = SqliteSaver(
                conn,
                serde=JsonPlusSerializer(allowed_msgpack_modules=_ALLOWED_MSGPACK_MODULES),
            )
        return _saver


def checkpoint_thread_id(*parts: str) -> str:
    """生成任务标识 → thread_id（截断 ≤255：checkpoints 表 thread_id 列宽坑）。"""
    raw = ":".join(str(part) for part in parts if part)
    return raw[:THREAD_ID_MAX]


def run_config(thread_id: str) -> RunnableConfig:
    """LangGraph run config：thread 是续跑的最小单位。"""
    return {"configurable": {"thread_id": checkpoint_thread_id(thread_id)}}


def cleanup_old_threads(retention_seconds: int) -> int:
    """删除最后活动时间超出保留期的 thread 全部检查点，返回删除的 thread 数。

    LangGraph 官方告警"checkpoints 无限增长需定期清理"的落地；thread 级删除
    （`delete_thread`）不清半截——恢复语义只认整条 run 的历史。
    """
    cutoff = datetime.now(UTC).timestamp() - retention_seconds
    saver = get_checkpointer()
    latest: dict[str, float] = {}
    for checkpoint in saver.list(None):
        thread_id = str(checkpoint.config.get("configurable", {}).get("thread_id") or "")
        if not thread_id:
            continue
        stamp = checkpoint.checkpoint.get("ts") or ""
        try:
            moment = datetime.fromisoformat(str(stamp)).timestamp()
        except ValueError:
            continue
        latest[thread_id] = max(latest.get(thread_id, 0.0), moment)
    removed = 0
    for thread_id, moment in latest.items():
        # `<=` 而非 `<`：Windows 系统时钟粒度实测 2ms（连续采样 99.99% 同值，
        # min 正间隔 0.0020s），"刚写入的检查点 ts"与"retention=0 的 cutoff"会落
        # 在同一刻度上——严格小于把它判成"未过期"漏删（test_confirm_flow 保留期
        # 用例的偶发红，2026-10-02 全量复跑定位；加压复现 7/300）。保留期语义上
        # "恰好到点"本就该删，生产日级保留期下该边界不可达。
        if moment <= cutoff:
            saver.delete_thread(thread_id)
            removed += 1
    return removed
