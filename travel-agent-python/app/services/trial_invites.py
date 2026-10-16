"""一次性邀请码。明码只在发放时返回；核销与注册共用事务。"""

import hashlib
import secrets
from datetime import datetime
from typing import cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.common.envelope import ApiError
from app.db.models import TrialInvite
from app.db.session import session_scope


def issue(count: int, operator: str) -> list[str]:
    if not 1 <= count <= 100:
        raise ApiError(400, "每次可生成 1-100 个邀请码")
    codes = [secrets.token_urlsafe(24) for _ in range(count)]
    with session_scope() as session:
        session.add_all(TrialInvite(code_hash=_hash(code), created_by=operator) for code in codes)
    return codes


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().encode()).hexdigest()


def consume(session: Session, code: str | None, user_id: int) -> None:
    result = session.execute(
        update(TrialInvite)
        .where(TrialInvite.code_hash == _hash(code or ""), TrialInvite.consumed_by.is_(None))
        .values(consumed_by=user_id, used_at=datetime.now())
    )
    if cast(CursorResult, result).rowcount != 1:
        raise ApiError(400, "邀请码无效或已使用")
