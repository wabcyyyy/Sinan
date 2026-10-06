"""后台管理端点（对应 Java `AdminController`）。

鉴权方向：`app/api/security.py` 的默认拒绝规则已把 `/api/admin` 整段限定为
"必须登录且 role=admin"（非管理员 403、匿名 401），因此这里仍按既有业务路由的
写法挂 router 级依赖，而不是逐条自查角色——少挂一条就是匿名可读的管理面。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.addons import ADDONS, addons
from app.common.envelope import ApiError, ok
from app.services import admin_service

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/addons")
def list_addons() -> dict:
    """能力开关列表（G-3.1）：键/标签/当前状态。"""
    return ok({"addons": addons.snapshot()})


@router.get("/addons/{key}/audit")
def addon_audit(key: str = Path(...)) -> dict:
    """切换审计（append-only，新在前）。"""
    if key not in ADDONS:
        raise ApiError(404, "Not Found")
    return ok({"audit": addons.audit_log(key)})


@router.put("/addons/{key}")
def set_addon(key: str = Path(...), body: dict | None = None, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    """切换能力开关（G-3.1）：落状态行 + 审计 + 本进程缓存即失效。

    请求体 {"enabled": bool}；未登记的 key 返回 404（与 addon 门控同话术）。
    """
    if key not in ADDONS:
        raise ApiError(404, "Not Found")
    enabled = bool((body or {}).get("enabled"))
    addons.set_enabled(key, enabled, changed_by=user.username)
    return ok({"key": key, "enabled": addons.is_enabled(key)})


@router.get("/stats")
def stats() -> dict:
    return ok(admin_service.stats())


@router.get("/users")
def users(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=500), keyword: str | None = Query(None)) -> dict:
    return ok(admin_service.page_users(page, size, keyword))


@router.put("/users/{id}/status/{status}")
def update_status(
    id: int = Path(...), status: int = Path(...), user: AuthUser = Depends(enforce_business_auth)
) -> dict:
    admin_service.update_status(id, status, user.username)
    return ok()


@router.delete("/users/{id}")
def delete_user(id: int = Path(...), user: AuthUser = Depends(enforce_business_auth)) -> dict:
    admin_service.delete_user(id, user.username)
    return ok()


@router.get("/itineraries")
def itineraries(
    page: int = Query(1, ge=1),
    size: int = Query(10, ge=1, le=500),
    keyword: str | None = Query(None),
    status: int | None = Query(None),
    userId: int | None = Query(None),
) -> dict:
    return ok(admin_service.page_itineraries(page, size, keyword, status, userId))


@router.delete("/itineraries/{id}")
def delete_itinerary(id: int = Path(...)) -> dict:
    admin_service.delete_itinerary(id)
    return ok()


@router.get("/agent-metrics")
def agent_metrics() -> dict:
    return ok(admin_service.agent_metrics())


@router.get("/llm-usage")
def llm_usage(range: str = Query("24h"), limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict:
    return ok(admin_service.llm_usage(range, limit, offset))
