"""协作路由（SPEC C2.3）：成员与邀请管理。

- `POST/GET /api/itinerary/{id}/invitations`：owner 生成（token 仅本次返回）/查看；
- `DELETE /api/itinerary/{id}/invitations/{invitationId}`：owner 撤销；
- `GET /api/itinerary/{id}/members`：成员可读；`PUT/DELETE .../members/{memberId}`：owner 改角色/移除；
- `POST /api/itinerary/invitations/accept`：登录后兑换（token 放请求体，不放查询参数）。

鉴权 `enforce_business_auth`；owner/成员判定在服务层（require_owned_main/require_main）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ApiError, ok
from app.schemas.collaboration import (
    InvitationAcceptRequest,
    InvitationCreate,
    MemberRoleUpdate,
)
from app.services import collaboration_service

router = APIRouter(
    prefix="/api/itinerary",
    tags=["collaboration"],
    dependencies=[Depends(enforce_business_auth)],
)


def _user(user: AuthUser | None) -> AuthUser:
    if user is None:
        raise ApiError(401, "请先登录")
    return user


@router.post("/invitations/accept")
def accept_invitation(body: InvitationAcceptRequest, user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(collaboration_service.accept_invitation(_user(user).id, body))


@router.post("/{id}/invitations")
def create_invitation(
    body: InvitationCreate, id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)
) -> dict:
    return ok(collaboration_service.create_invitation(_user(user).id, id, body))


@router.get("/{id}/invitations")
def list_invitations(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(collaboration_service.list_invitations(_user(user).id, id))


@router.delete("/{id}/invitations/{invitationId}")
def revoke_invitation(
    id: int = Path(..., ge=1),
    invitationId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    collaboration_service.revoke_invitation(_user(user).id, id, invitationId)
    return ok()


@router.get("/{id}/members")
def list_members(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(collaboration_service.list_members(_user(user).id, id))


@router.put("/{id}/members/{memberId}")
def update_member(
    body: MemberRoleUpdate,
    id: int = Path(..., ge=1),
    memberId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(collaboration_service.update_member_role(_user(user).id, id, memberId, body))


@router.delete("/{id}/members/{memberId}")
def remove_member(
    id: int = Path(..., ge=1),
    memberId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    collaboration_service.remove_member(_user(user).id, id, memberId)
    return ok()
