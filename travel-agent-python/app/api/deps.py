"""FastAPI 鉴权依赖：判定语义沿用自退役 Java 后端的 `JwtAuthenticationFilter`。

判定顺序（任一步不过 → 视为未认证，最终 401，无响应体细节）：
  取票（Bearer 头优先，回落 Cookie TA_AUTH）→ 吊销黑名单 → 验签 → 回查用户
  → status 为 1/NULL 且未软删 → 角色映射 ROLE_ADMIN / ROLE_USER

角色不写在 JWT claims 里（每次回查 sys_user.role），保证改角色即时生效。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from app.common import jwt_compat, token_revocation, user_repository
from app.common.config import settings

logger = logging.getLogger(__name__)

COOKIE_NAME = "TA_AUTH"
ROLE_ADMIN = "admin"


@dataclass(frozen=True)
class AuthUser:
    id: int
    username: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def extract_token(authorization: str | None, cookies: Mapping[str, str]) -> tuple[str | None, str | None]:
    """返回 (token, source)。Bearer 优先于 Cookie，与 Java readToken 顺序一致。"""
    if authorization and authorization.startswith("Bearer "):
        bearer = authorization[7:].strip()
        if bearer:
            return bearer, "bearer"
    cookie_value = cookies.get(COOKIE_NAME)
    if cookie_value and cookie_value.strip():
        return cookie_value.strip(), "cookie"
    return None, None


def authenticate(token: str | None) -> AuthUser | None:
    if not token:
        return None
    if token_revocation.is_revoked(token):
        logger.debug("jwt token revoked")
        return None
    try:
        claims = jwt_compat.decode_token(token, settings.jwt_secret)
    except jwt_compat.JwtError as exc:
        logger.debug("invalid jwt token: %s", exc)
        return None

    username = claims.get("sub")
    if not isinstance(username, str) or not username:
        return None
    user = user_repository.find_by_username(username)
    if not user:
        return None
    # status 为 NULL 或 1 视为可用：与 Java 过滤器的 (status == null || status == 1) 同口径
    if user.get("status") not in (None, 1):
        return None
    return AuthUser(id=int(user["id"]), username=username, role=str(user.get("role") or "user"))
