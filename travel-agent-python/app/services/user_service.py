"""用户与登录服务（移植自 Java `UserServiceImpl` + `AuthController` 的编排）。

口令：Java 用 `BCryptPasswordEncoder`，库里存量哈希是 `$2a$` 前缀。bcrypt 的
`$2a$` 格式跨实现兼容，因此这里校验必须接受存量哈希、签发用 `$2a$`，否则
迁移后老账号全部登录不上。

登录响应**不回传 JWT**（`LoginResponse(user)`）：凭据只走 HttpOnly Cookie，
降低 XSS 直接读 body 偷 token 的面。Bearer 头仍被接受，供脚本/联调。

校验文案逐字保留 Java 的：
- 注册重名 → 「注册失败，请检查用户名或稍后重试」（刻意模糊，不告诉调用者哪一半失败）
- 口令错误 → 「用户名或密码错误」（不区分用户不存在/密码错，避免账号枚举）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import bcrypt
from sqlalchemy import select

from app.common import jwt_compat
from app.common.config import settings
from app.common.envelope import ApiError
from app.db.models import SysUser
from app.db.session import session_scope
from app.schemas.business.auth import UserInfoVO
from app.services import state_and_sessions as sessions

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"


@dataclass(frozen=True)
class LoginResult:
    token: str
    user: dict


def _bcrypt_salt() -> bytes:
    # prefix=2a 与 Spring Security 的 BCryptPasswordEncoder 默认一致
    return bcrypt.gensalt(rounds=10, prefix=b"2a")


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode("utf-8"), _bcrypt_salt()).decode("ascii")


def verify_password(raw: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        return bcrypt.checkpw(raw.encode("utf-8"), stored.encode("ascii"))
    except ValueError:
        # 存量哈希形态异常时按"密码错误"处理，不向上抛出 500
        logger.warning("malformed bcrypt hash in sys_user; treating as auth failure")
        return False


def to_vo(user: SysUser) -> dict:
    # 模型即契约：形状唯一源在 app/schemas/business/auth.py（G-1.1）
    return UserInfoVO(
        id=user.id,
        username=user.username,
        nickname=user.nickname,
        phone=user.phone,
        role=user.role,
    ).model_dump()


def _find_user(username: str) -> SysUser | None:
    with session_scope() as session:
        # 软删用户不可登录：SELECT 走全局作用域，这里再显式限定 username 唯一索引
        return session.execute(select(SysUser).where(SysUser.username == username)).scalar_one_or_none()


def register(username: str, password: str, nickname: str | None, ip: str) -> dict:
    if sessions.sliding_hit(sessions.register_key(ip), sessions.LOGIN_WINDOW_SECONDS) > sessions.MAX_REGISTER_ATTEMPTS:
        raise ApiError(429, "注册过于频繁，请稍后再试")
    if _find_user(username) is not None:
        raise ApiError(400, "注册失败，请检查用户名或稍后重试")
    if not _complex_enough(password):
        raise ApiError(400, "密码需同时包含字母和数字")

    try:
        with session_scope() as session:
            user = SysUser(
                username=username,
                password=hash_password(password),
                nickname=(nickname or "").strip() or username,
                status=1,
                role="user",
            )
            session.add(user)
            session.flush()
            vo = to_vo(user)
    except Exception as exc:  # 唯一键冲突等一律回同一句模糊文案
        # 只记异常类型：SQLAlchemy 的 StatementError 会把 SQL 与**绑定参数**一起
        # 打进 str(exc)，注册失败路径上的参数含 bcrypt 口令哈希——离线可破，
        # 等于把凭据写进 INFO 日志。原因另用 type/参数名定位，不外拷全文。
        logger.info("register failed: %s", type(exc).__name__)
        raise ApiError(400, "注册失败，请检查用户名或稍后重试") from exc
    return vo


def _complex_enough(password: str) -> bool:
    return any(c.isalpha() for c in password) and any(c.isdigit() for c in password)


def login(username: str, password: str, ip: str) -> LoginResult:
    throttle_key = sessions.login_fail_key(ip, username)
    if sessions.sliding_count(throttle_key, sessions.LOGIN_WINDOW_SECONDS) >= sessions.MAX_LOGIN_FAILURES:
        raise ApiError(429, "登录失败次数过多，请稍后再试")

    user = _find_user(username)
    if user is None or not verify_password(password, user.password):
        sessions.sliding_hit(throttle_key, sessions.LOGIN_WINDOW_SECONDS)
        raise ApiError(400, "用户名或密码错误")
    if user.status == 0:
        raise ApiError(403, "账号已禁用")

    sessions.reset_counter(throttle_key)
    ttl = settings.jwt_expire_hours * 3600
    token = jwt_compat.encode_token(user.username, settings.jwt_secret, ttl)
    try:
        claims = jwt_compat.decode_token(token, settings.jwt_secret)
        sessions.register_session(user.username, claims["jti"], token, jwt_compat.remaining_seconds(claims))
    except Exception as exc:
        # 会话登记失败不影响登录：Cookie 仍然可用
        logger.warning("session register failed: %s", exc)
    return LoginResult(token=token, user=to_vo(user))


def logout(raw_token: str | None) -> None:
    token = _normalize(raw_token)
    if not token:
        return
    try:
        claims = jwt_compat.decode_token(token, settings.jwt_secret)
        ttl = jwt_compat.remaining_seconds(claims)
        sessions.revoke_current(claims.get("sub", ""), claims.get("jti", ""), token, ttl)
    except jwt_compat.JwtError:
        # 无效/过期 token：登出仍视为成功（幂等）
        return


def logout_all(username: str) -> int:
    if not username:
        return 0
    return sessions.revoke_all(username)


def _normalize(raw_token: str | None) -> str | None:
    if not raw_token:
        return None
    token = raw_token[7:].strip() if raw_token.startswith("Bearer ") else raw_token.strip()
    return token or None


def info(username: str) -> dict:
    user = _find_user(username)
    if user is None:
        raise ApiError(401, "用户不存在")
    return to_vo(user)


def is_admin(user: dict) -> bool:
    return user.get("role") == ROLE_ADMIN
