"""协作（SPEC C2.3）跨端契约模型——登记进 contracts.py 的 business_api 组。

角色只有 editor/viewer：owner 由 itinerary_main.user_id 定义，不进成员表；
明文邀请 token 只在创建响应出现一次，列表/详情一律不回传。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CollaboratorRole = Literal["editor", "viewer"]


class InvitationCreate(BaseModel):
    role: CollaboratorRole


class InvitationVO(BaseModel):
    """邀请状态视图——永不包含 token/token_hash。"""

    id: int
    itineraryId: int
    role: CollaboratorRole
    expiresAt: str | None
    createdAt: str
    acceptedAt: str | None
    acceptedBy: int | None
    revokedAt: str | None
    status: Literal["pending", "accepted", "revoked", "expired"]


class InvitationCreatedVO(InvitationVO):
    """创建响应：明文 token 仅此一次返回，服务端只存摘要。"""

    token: str = Field(min_length=16, max_length=256)


class InvitationListVO(BaseModel):
    invitations: list[InvitationVO]


class MemberVO(BaseModel):
    id: int
    itineraryId: int
    userId: int
    username: str | None
    role: CollaboratorRole
    createdAt: str


class MemberListVO(BaseModel):
    members: list[MemberVO]


class MemberRoleUpdate(BaseModel):
    role: CollaboratorRole


class InvitationAcceptRequest(BaseModel):
    token: str = Field(min_length=16, max_length=256)


class InvitationAcceptVO(BaseModel):
    itineraryId: int
    role: CollaboratorRole
    title: str
