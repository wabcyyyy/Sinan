"""模板路由（SPEC C2.4）：发布/下架 + 模板广场 + fork。

- `POST/DELETE /api/itinerary/{id}/template`：owner 发布/下架（不做 addon 门控，
  可先发布后开广场）；
- `GET /api/templates`、`GET /api/templates/{id}`、`POST /api/templates/{id}/fork`：
  `template_community` addon 门控（默认关——自托管不默认开放社区面，关=404）。
鉴权 `enforce_business_auth`；owner/fork 归属判定在服务层。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common import addons
from app.common.envelope import ApiError, ok
from app.services import template_service

router = APIRouter(
    prefix="/api",
    tags=["templates"],
    # 守卫机检（test_auth_migration）要求每条业务路由声明鉴权依赖；模板无公开面
    dependencies=[Depends(enforce_business_auth)],
)


def _user(user: AuthUser | None) -> AuthUser:
    if user is None:
        raise ApiError(401, "请先登录")
    return user


def _require_community() -> None:
    if not addons.is_enabled("template_community"):
        raise ApiError(404, "Not Found")


@router.post("/itinerary/{id}/template")
def publish_template(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(template_service.publish(_user(user).id, id))


@router.get("/itinerary/{id}/template")
def template_view(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    """owner 视角的模板视图/预览：已发布=物化快照，未发布=即时构建（不落库）。"""
    return ok(template_service.template_view(_user(user).id, id))


@router.delete("/itinerary/{id}/template")
def unpublish_template(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    template_service.unpublish(_user(user).id, id)
    return ok()


@router.get("/templates/capability")
def template_capability(user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    """导航可见性探测：恒 200（addon 关时 enabled=false）；必须先于 /{templateId} 注册。"""
    _user(user)
    return ok({"enabled": addons.is_enabled("template_community")})


@router.get("/templates")
def list_templates(user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    _require_community()
    _user(user)
    return ok(template_service.list_templates())


@router.get("/templates/{templateId}")
def get_template(templateId: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    _require_community()
    _user(user)
    return ok(template_service.get_template(templateId))


@router.post("/templates/{templateId}/fork")
def fork_template(templateId: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    _require_community()
    return ok(template_service.fork(_user(user).id, templateId))
