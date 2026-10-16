"""运维巡检（上线 v1 T8b）：把"运营能不能接住"变成三个可周期跑的检查。

- **磁盘余量**：数据盘写满会让导出、图检查点、轨迹归档一起静默失败；
- **备份龄期**：备份"存在"不等于"新鲜"，且备份目录没配时**如实跳过**，不假称通过；
- **依赖可用性**：复用 `deep_health` 的探测结果，不自己另连一次 MySQL/Redis。

每个检查越线时发一条告警、回到安全侧时补一条 recovered（边沿触发，避免每 5 分钟重报），
发送本身统一走 `app.common.alerting`（冷却 + 脱敏 + 失败静默都在那里）。

诚实边界：本模块只证明"检查会发告警"，**没有**证明"告警真的送达某个人的手机/邮箱"——
那需要一个真实配置的目标与一次实投递，属 T9 演练项。
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from app.common import alerting
from app.common.config import settings
from app.common.deep_health import snapshot

logger = logging.getLogger(__name__)

UP = "up"

_low_disk = False
_backup_stale = False
_deps_down = False


def check_disk() -> None:
    """数据盘余量低于下限时告警（`export_dir` 与 data/ 同盘，取它的挂载点）。"""
    global _low_disk
    floor_mb = float(getattr(settings, "alert_disk_min_free_mb", 0) or 0)
    if floor_mb <= 0:
        return  # 未设阈值 = 不检查（比默认一个猜出来的数诚实）
    probe = Path(str(settings.export_dir or "")).resolve()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free_mb = shutil.disk_usage(probe).free / 1_048_576
    except OSError as exc:
        logger.warning("ops-watch disk probe failed at %s: %s", probe, exc)
        return
    if free_mb < floor_mb and not _low_disk:
        _low_disk = True
        alerting.send("ops.disk_low", {"free_mb": round(free_mb), "floor_mb": round(floor_mb)}, scope="data")
    elif free_mb >= floor_mb and _low_disk:
        _low_disk = False
        alerting.send("ops.disk_recovered", {"free_mb": round(free_mb)}, scope="data")


def check_backups() -> None:
    """备份目录里最新一份的龄期超过上限就告警；目录未配置则跳过并说明原因。"""
    global _backup_stale
    directory = str(getattr(settings, "alert_backup_dir", "") or "").strip()
    max_hours = float(getattr(settings, "alert_backup_max_age_hours", 0) or 0)
    if not directory or max_hours <= 0:
        return
    root = Path(directory)
    if not root.is_dir():
        _backup_stale = True
        alerting.send("ops.backup_missing", {"directory": directory}, scope="backup")
        return
    newest = max(
        (item.stat().st_mtime for item in root.iterdir() if item.is_file()),
        default=None,
    )
    if newest is None:
        age_hours: float | None = None
    else:
        age_hours = (time.time() - newest) / 3600.0
    stale = newest is None or (age_hours is not None and age_hours > max_hours)
    if stale and not _backup_stale:
        _backup_stale = True
        alerting.send(
            "ops.backup_stale",
            {"age_hours": None if age_hours is None else round(age_hours, 1), "max_hours": round(max_hours, 1)},
            scope="backup",
        )
    elif not stale and _backup_stale:
        _backup_stale = False
        alerting.send("ops.backup_recovered", {"age_hours": round((age_hours or 0), 1)}, scope="backup")


def check_dependencies() -> None:
    """依赖从 up 翻到 not-up 时报一次；恢复时也报一条（边沿触发）。"""
    global _deps_down
    state = snapshot()
    down = sorted(key for key, value in state.items() if key != "status" and value != UP)
    if down and not _deps_down:
        _deps_down = True
        alerting.send("ops.dependencies_down", {"down": down}, scope="deps")
    elif not down and _deps_down:
        _deps_down = False
        alerting.send("ops.dependencies_recovered", {}, scope="deps")


def run_once() -> None:
    """cron 入口（`main.py` 注册）。任一检查抛错都不影响其它检查，也不影响服务。"""
    for check in (check_disk, check_backups, check_dependencies):
        try:
            check()
        except Exception:
            logger.exception("ops-watch check %s failed", check.__name__)


def reset_state() -> None:
    """测试复位用（边沿标志与告警冷却一起清）。"""
    global _low_disk, _backup_stale, _deps_down
    _low_disk = _backup_stale = _deps_down = False
    alerting.reset_state()
