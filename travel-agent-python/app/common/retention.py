"""增长面保留策略（审查 P2-2）。

盘点过的三处"只写不删"里有两处能就地收敛：

- `agent_traces.jsonl`：TraceStore 是纯追加，此前无轮转无上限（默认开）；
  现在按日改名归档 + 删 >14 天旧档——单文件不再无限增长，历史仍有 14 天可回放。
- `data/export/*.pdf`：应用侧从不删除，且被 backup bundle 连带打包放大；
  现在按文件修改时间删 >30 天旧导出。

第三处（用户上传图）未做：删除用户上传内容属于产品决策（可能要保留给用户
"我的图片"入口），不在本次"确定性卫生"范围内，留在已知局限表。

按文件 mtime 判定而不是回查 export_task.finished_at：文件落盘时刻就是任务完成
时刻，且不依赖业务库行还在（软删/迁移后仍能清）。归档用文件名里的日期做保留期
判定，与 mtime 无关——即使文件被拷来拷去，保留期也稳定可解释。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

TRACE_ARCHIVE_KEEP_DAYS = 14
EXPORT_KEEP_DAYS = 30


def _archive_name(path: Path, when: datetime) -> Path:
    return path.with_name(f"{path.stem}-{when:%Y-%m-%d}{path.suffix}")


def rotate_trace_store(
    path: str | Path, *, now: datetime | None = None, keep_days: int = TRACE_ARCHIVE_KEEP_DAYS
) -> int:
    """按日归档 trace JSONL 并清理过期归档；返回删除的归档数。

    流程：先删过期归档（文件名日期 < now - keep_days），再把当前活动文件改名成
    `<stem>-<当天日期>.jsonl`（下次 append 会自动重建活动文件；TraceStore 每次
    append 都重新打开文件，不持有句柄，改名安全）。

    注意副作用：`TraceStore.get()` 只读活动文件，归档后的 run 只能靠文件名到
    磁盘里取——这是"有界增长"换来的取舍，回放窗口 = keep_days 天。
    """
    target = Path(path)
    directory = target.parent
    moment = now or datetime.now(UTC)
    removed = 0
    cutoff = moment - timedelta(days=max(int(keep_days), 0))
    pattern = f"{target.stem}-*{target.suffix}"
    if directory.exists():
        for candidate in sorted(directory.glob(pattern)):
            stamp = _date_in_name(candidate, target)
            if stamp is not None and stamp.date() < cutoff.date():
                try:
                    candidate.unlink()
                    removed += 1
                except OSError as exc:
                    logger.warning("trace archive cleanup failed for %s: %s", candidate, exc)
    if target.exists():
        try:
            if target.stat().st_size > 0:
                archived = _archive_name(target, moment)
                if archived.exists():
                    # 同一天重复轮转：并入已有归档而不是覆盖（append 语义）
                    with archived.open("a", encoding="utf-8") as dst, target.open("r", encoding="utf-8") as src:
                        dst.write(src.read())
                    target.unlink()
                else:
                    os.replace(target, archived)
        except OSError as exc:
            logger.warning("trace rotation failed for %s: %s", target, exc)
    return removed


def _date_in_name(candidate: Path, target: Path) -> datetime | None:
    """从 `<stem>-YYYY-MM-DD<suffix>` 里取日期；命名不符返回 None（不当过期处理）。"""
    prefix = f"{target.stem}-"
    body = candidate.stem[len(prefix) :] if candidate.stem.startswith(prefix) else ""
    try:
        return datetime.strptime(body, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def cleanup_exports(
    directory: str | Path, *, older_than_days: int = EXPORT_KEEP_DAYS, now: datetime | None = None
) -> int:
    """删除导出目录里超过保留期的文件；返回删除数（目录不存在按 0）。"""
    root = Path(directory)
    if not root.is_dir():
        return 0
    moment = now or datetime.now(UTC)
    cutoff = moment.timestamp() - max(int(older_than_days), 0) * 86400
    removed = 0
    for candidate in root.iterdir():
        try:
            if not candidate.is_file() or candidate.stat().st_mtime >= cutoff:
                continue
            candidate.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("export cleanup failed for %s: %s", candidate, exc)
    return removed
