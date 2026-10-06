"""运行时功能开关（Addon 体系，G-3.1）。

职责：
- is_enabled(key)：查询某能力是否启用——优先读 addon_state 表行，无行回落
  env 默认值（INV-6：env 键保留为默认值来源）；进程内缓存 + TTL 兜底；
- set_enabled(key, enabled, changed_by)：落状态行 + 写 addon_audit 审计 +
  就地失效本进程缓存（管理 API 与业务同进程，切换即生效；TTL 只为多进程
  部署兜底）；
- require_addon(key)：FastAPI 依赖——能力停用时抛 ApiError(404)。**404 而
  不是 403**：隐藏能力存在性，而非宣告"你被禁用"（与业务面 404 语义一致）。

首批迁移的四个 env 开关（键名即 addon_key）：
- web_search          <- WEB_SEARCH_ENABLED（研究补池的联网搜索）
- live_price          <- LIVE_PRICE_SEARCH / LIVE_FOOD_PRICE_SEARCH（实时价）
- schedule_optimizer  <- SCHEDULE_OPTIMIZER_ENABLED（路线重排）
- route_service       <- ROUTE_SERVICE_ENABLED（路线矩阵校验）

依赖：app.db.session（SQL 会话）、app.common.config.settings、
app.common.envelope.ApiError；fastapi 仅依赖项用。
"""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy import text

from app.common.config import settings
from app.common.envelope import ApiError
from app.db.session import session_scope

#: 已登记的能力清单（键名 = 迁移表 addon_key）。新增能力在此登记并给出 env 默认。
ADDONS: dict[str, dict[str, str]] = {
    "web_search": {"env": "web_search_enabled", "label": "联网搜索补池"},
    "live_price": {"env": "live_price_search", "label": "实时价格查询"},
    "schedule_optimizer": {"env": "schedule_optimizer_enabled", "label": "路线优化重排"},
    "route_service": {"env": "route_service_enabled", "label": "路线矩阵校验"},
    # MCP 出口（G-3.6）：默认关闭——对外暴露能力必须由管理员显式打开
    "mcp": {"env": "mcp_enabled", "label": "MCP 只读工具出口"},
    # MCP 行程写入（C3.3）：默认关闭——读面开不足以写行程，写面单独显式打开
    "mcp_write": {"env": "mcp_write_enabled", "label": "MCP 行程写入"},
    # 模板广场（C2.4）：默认关闭——自托管私有部署不默认开放社区面
    "template_community": {"env": "template_community_enabled", "label": "模板广场"},
    # 条目对/错反馈（C3.5）：默认关闭——读面开不足以开反馈面，增强能力单独显式打开
    "item_feedback": {"env": "item_feedback_enabled", "label": "条目对/错反馈"},
}

#: 进程内缓存 TTL（秒）：管理切换会就地失效，TTL 只兜多进程部署的最终一致。
_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[bool, float]] = {}

_TABLE_READY: bool | None = None


def _env_default(key: str) -> bool:
    """env 默认值（INV-6）：addon 无状态行时的真值来源。"""
    attr = ADDONS[key]["env"]
    return bool(getattr(settings, attr))


def _table_exists() -> bool:
    """addon_state 表是否已建（迁移前/单测空库时优雅回落 env 默认）。"""
    global _TABLE_READY
    if _TABLE_READY is None:
        try:
            with session_scope() as session:
                session.execute(text("SELECT 1 FROM addon_state LIMIT 1"))
            _TABLE_READY = True
        except Exception:
            _TABLE_READY = False
    return _TABLE_READY


def is_enabled(key: str) -> bool:
    """能力开关查询：DB 行优先，无行回落 env 默认；结果走进程内 TTL 缓存。"""
    if key not in ADDONS:
        raise KeyError(f"未登记的能力：{key}")
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    value = _read_persisted(key) if _table_exists() else _env_default(key)
    _cache[key] = (value, now)
    return value


def _read_persisted(key: str) -> bool:
    with session_scope() as session:
        row = session.execute(text("SELECT enabled FROM addon_state WHERE addon_key = :key"), {"key": key}).scalar()
    return _env_default(key) if row is None else bool(row)


def set_enabled(key: str, enabled: bool, changed_by: str) -> None:
    """切换能力开关：upsert 状态行 + 写审计 + 本进程缓存立即失效。"""
    if key not in ADDONS:
        raise KeyError(f"未登记的能力：{key}")
    with session_scope() as session:
        session.execute(
            text(
                "INSERT INTO addon_state (addon_key, enabled, updated_by) VALUES (:key, :enabled, :by) "
                "ON DUPLICATE KEY UPDATE enabled = :enabled, updated_by = :by, updated_at = CURRENT_TIMESTAMP"
            ),
            {"key": key, "enabled": int(enabled), "by": changed_by},
        )
        session.execute(
            text("INSERT INTO addon_audit (addon_key, enabled, changed_by) VALUES (:key, :enabled, :by)"),
            {"key": key, "enabled": int(enabled), "by": changed_by},
        )
    _cache.pop(key, None)


def audit_log(key: str, limit: int = 50) -> list[dict]:
    """切换审计（append-only，新在前）；供管理端展示。"""
    if not _table_exists():
        return []
    with session_scope() as session:
        rows = (
            session.execute(
                text(
                    "SELECT enabled, changed_by, created_at FROM addon_audit "
                    "WHERE addon_key = :key ORDER BY id DESC LIMIT :limit"
                ),
                {"key": key, "limit": limit},
            )
            .mappings()
            .all()
        )
    return [
        {"enabled": bool(row["enabled"]), "changedBy": row["changed_by"], "createdAt": str(row["created_at"])}
        for row in rows
    ]


def snapshot() -> list[dict]:
    """全部能力的当前状态（管理端列表用）。"""
    return [{"key": key, "label": info["label"], "enabled": is_enabled(key)} for key, info in ADDONS.items()]


def require_addon(key: str) -> Callable[[], None]:
    """FastAPI 依赖工厂：能力停用时端点表现为 404（隐藏而非 403 暴露）。"""

    def _dependency() -> None:
        if not is_enabled(key):
            raise ApiError(404, "Not Found")

    return _dependency


def reset_cache() -> None:
    """测试/管理操作用：清空进程内缓存（含表存在性探测）。"""
    global _TABLE_READY
    _cache.clear()
    _TABLE_READY = None


class _AddonService:
    """模块单例门面：`from app.common.addons import addons` 后 addons.is_enabled(...)。

    方法在**调用期**解析模块级函数（非 staticmethod 绑定），测试 patch
    `app.common.addons.is_enabled` 对单例同样生效。
    """

    def is_enabled(self, key: str) -> bool:
        return is_enabled(key)

    def set_enabled(self, key: str, enabled: bool, changed_by: str) -> None:
        set_enabled(key, enabled, changed_by)

    def snapshot(self) -> list[dict]:
        return snapshot()

    def audit_log(self, key: str, limit: int = 50) -> list[dict]:
        return audit_log(key, limit)

    def reset_cache(self) -> None:
        reset_cache()


addons = _AddonService()
