"""协作成员与邀请（SPEC C2.3）：owner 管理、角色校验、原子兑换。

安全要点：
- 邀请 token 用 `secrets.token_urlsafe(32)` 生成，库内只存 sha256 摘要；明文只在
  创建响应出现一次，不写日志（本模块不调用 logger 记录 token）。
- 兑换在同一事务内 `with_for_update()` 锁邀请行后校验 未撤销/未使用/未过期，
  并发兑换同一条邀请只能成功一次；已有成员兑换 409 且**不消费**邀请。
- 移除成员=硬删成员行：只撤销该行程访问权，不动对方全局登录会话。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.common.envelope import ApiError
from app.common.vo_json import iso_datetime
from app.db.models import ItineraryInvitation, ItineraryMain, ItineraryMember, SysUser
from app.db.session import session_scope
from app.schemas.collaboration import InvitationAcceptRequest, InvitationCreate, MemberRoleUpdate
from app.services import itinerary_query

INVITATION_TTL = timedelta(days=7)
_ROLES = ("editor", "viewer")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _invitation_status(row: ItineraryInvitation, now: datetime) -> str:
    if row.accepted_at is not None:
        return "accepted"
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= now:
        return "expired"
    return "pending"


def _invitation_vo(row: ItineraryInvitation, now: datetime) -> dict[str, Any]:
    return {
        "id": row.id,
        "itineraryId": row.itinerary_id,
        "role": row.role,
        "expiresAt": iso_datetime(row.expires_at),
        "createdAt": iso_datetime(row.created_at),
        "acceptedAt": iso_datetime(row.accepted_at),
        "acceptedBy": row.accepted_by,
        "revokedAt": iso_datetime(row.revoked_at),
        "status": _invitation_status(row, now),
    }


def create_invitation(user_id: int, itinerary_id: int, body: InvitationCreate) -> dict[str, Any]:
    if body.role not in _ROLES:
        raise ApiError(400, "角色只准 editor/viewer")
    with session_scope() as session:
        itinerary_query.require_owned_main(session, user_id, itinerary_id)
        token = secrets.token_urlsafe(32)
        row = ItineraryInvitation(
            itinerary_id=itinerary_id,
            role=body.role,
            token_hash=_hash_token(token),
            expires_at=datetime.now() + INVITATION_TTL,
        )
        session.add(row)
        session.flush()
        vo = _invitation_vo(row, datetime.now())
    return {**vo, "token": token}


def list_invitations(user_id: int, itinerary_id: int) -> list[dict[str, Any]]:
    """owner 查看邀请列表（含各状态）；不返回 token/摘要。"""
    with session_scope() as session:
        itinerary_query.require_owned_main(session, user_id, itinerary_id)
        rows = session.scalars(
            select(ItineraryInvitation)
            .where(ItineraryInvitation.itinerary_id == itinerary_id)
            .order_by(ItineraryInvitation.id)
        ).all()
        now = datetime.now()
        return [_invitation_vo(row, now) for row in rows]


def revoke_invitation(user_id: int, itinerary_id: int, invitation_id: int) -> None:
    with session_scope() as session:
        itinerary_query.require_owned_main(session, user_id, itinerary_id)
        row = session.scalar(
            select(ItineraryInvitation).where(
                ItineraryInvitation.id == invitation_id, ItineraryInvitation.itinerary_id == itinerary_id
            )
        )
        if row is None:
            raise ApiError(404, "邀请不存在")
        if row.accepted_at is not None:
            raise ApiError(400, "邀请已被使用，无法撤销")
        row.revoked_at = datetime.now()


def accept_invitation(user_id: int, body: InvitationAcceptRequest) -> dict[str, Any]:
    """登录后兑换：原子消费邀请并写入成员关系；已有成员 409 不消费。"""
    with session_scope() as session:
        row = session.scalar(
            select(ItineraryInvitation)
            .where(ItineraryInvitation.token_hash == _hash_token(body.token))
            .with_for_update()
        )
        if row is None:
            raise ApiError(404, "邀请无效")
        main = session.get(ItineraryMain, row.itinerary_id)
        if main is None:  # 行程已删除：邀请作废且不消费（保留 owner 侧可见状态）
            raise ApiError(404, "行程不存在")
        now = datetime.now()
        if row.revoked_at is not None:
            raise ApiError(400, "邀请已被撤销")
        if row.accepted_at is not None:
            raise ApiError(400, "邀请已被使用")
        if row.expires_at <= now:
            raise ApiError(400, "邀请已过期")
        existing = session.scalar(
            select(ItineraryMember).where(
                ItineraryMember.itinerary_id == row.itinerary_id, ItineraryMember.user_id == user_id
            )
        )
        if existing is not None:
            # 已是成员：不隐式改角色，也不消费这条邀请
            raise ApiError(409, "你已是该行程成员")
        member = ItineraryMember(itinerary_id=row.itinerary_id, user_id=user_id, role=row.role)
        session.add(member)
        row.accepted_at = now
        row.accepted_by = user_id
        try:
            session.flush()
        except IntegrityError as exc:
            # 并发路径（不同邀请同目标）：成员唯一键兜底，同样不消费当前邀请
            raise ApiError(409, "你已是该行程成员") from exc
        return {"itineraryId": row.itinerary_id, "role": row.role, "title": main.title}


def list_members(user_id: int, itinerary_id: int) -> list[dict[str, Any]]:
    """成员可读成员列表（含自己）；username 软删用户容忍缺失。"""
    with session_scope() as session:
        itinerary_query.require_main(session, user_id, itinerary_id)
        rows = session.execute(
            select(ItineraryMember, SysUser.username)
            .outerjoin(SysUser, SysUser.id == ItineraryMember.user_id)
            .where(ItineraryMember.itinerary_id == itinerary_id)
            .order_by(ItineraryMember.id)
        ).all()
        return [
            {
                "id": member.id,
                "itineraryId": member.itinerary_id,
                "userId": member.user_id,
                "username": username,
                "role": member.role,
                "createdAt": iso_datetime(member.created_at),
            }
            for member, username in rows
        ]


def _require_member(session: Session, user_id: int, itinerary_id: int, member_id: int) -> ItineraryMember:
    row = session.scalar(
        select(ItineraryMember).where(ItineraryMember.id == member_id, ItineraryMember.itinerary_id == itinerary_id)
    )
    if row is None:
        raise ApiError(404, "成员不存在")
    return row


def update_member_role(user_id: int, itinerary_id: int, member_id: int, body: MemberRoleUpdate) -> dict[str, Any]:
    if body.role not in _ROLES:
        raise ApiError(400, "角色只准 editor/viewer")
    with session_scope() as session:
        itinerary_query.require_owned_main(session, user_id, itinerary_id)
        row = _require_member(session, user_id, itinerary_id, member_id)
        row.role = body.role
        session.flush()
        username = session.scalar(select(SysUser.username).where(SysUser.id == row.user_id))
        vo = {
            "id": row.id,
            "itineraryId": row.itinerary_id,
            "userId": row.user_id,
            "username": username,
            "role": row.role,
            "createdAt": iso_datetime(row.created_at),
        }
        member_user_id = row.user_id
    # 角色变更必须立刻反映到该成员的详情缓存（myRole/前端写入口），不能等 600s TTL
    itinerary_query.evict_detail(member_user_id, itinerary_id)
    return vo


def remove_member(user_id: int, itinerary_id: int, member_id: int) -> None:
    with session_scope() as session:
        itinerary_query.require_owned_main(session, user_id, itinerary_id)
        row = _require_member(session, user_id, itinerary_id, member_id)
        member_user_id = row.user_id
        session.delete(row)  # 硬删：仅撤销该行程访问权，对方全局登录会话不受影响
    itinerary_query.evict_detail(member_user_id, itinerary_id)
