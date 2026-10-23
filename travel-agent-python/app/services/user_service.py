"""用户与登录服务（移植自 Java `UserServiceImpl` + `AuthController` 的编排）。

口令：Java 用 `BCryptPasswordEncoder`，库里存量哈希是 `$2a$` 前缀。bcrypt 的
`$2a$` 格式跨实现兼容，因此这里校验必须接受存量哈希、签发用 `$2a$`，否则
迁移后老账号全部登录不上。

校验文案逐字保留 Java 的：
- 注册重名 → 「注册失败，请检查用户名或稍后重试」（刻意模糊，不告诉调用者哪一半失败）
- 口令错误 → 「用户名或密码错误」（不区分用户不存在/密码错，避免账号枚举）

改密（`change_password`）的文案不受反枚举约束：身份已由会话票确认，"原密码对不对"
不是攻击者不知道的信息，所以可以直说「原密码错误」——含混反而会让真人以为账号出问题。

登录响应**不回传 JWT**（`LoginResponse(user)`）：凭据只走 HttpOnly Cookie，
降低 XSS 直接读 body 偷 token 的面。Bearer 头仍被接受，供脚本/联调。
改密成功后同样只把新票写进 Cookie：响应体里没有任何凭据。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import bcrypt
from sqlalchemy import select

from app.common import jwt_tokens, trial_billing
from app.common.config import settings
from app.common.envelope import ApiError
from app.db.models import SysUser
from app.db.session import session_scope
from app.schemas.business.auth import UserInfoVO
from app.services import state_and_sessions as sessions
from app.services import trial_invites

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


def register(username: str, password: str, nickname: str | None, ip: str, invite_code: str | None = None) -> dict:
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
            if settings.registration_invite_required:
                trial_invites.consume(session, invite_code, user.id)
            trial_billing.open_wallet(session, user.id)
            vo = to_vo(user)
    except ApiError:
        raise
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
    # 按 IP 总量闸（审计 §3.3.3）：先记一次再判定，放在 (IP, 用户名) 失败锁与任何
    # bcrypt 计算之前——超限 IP 的第 N+1 次连 _find_user 都不进，喷密码循环既喷不动
    # 也烧不动 CPU。窗口与键见 state_and_sessions（与注册闸同实现：Redis 滑窗，
    # 不可用时进程内兜底）；阈值为 Settings.login_ip_rate_per_minute（默认 30/min）。
    ip_hits = sessions.sliding_hit(sessions.login_ip_key(ip), sessions.LOGIN_IP_WINDOW_SECONDS)
    if ip_hits > settings.login_ip_rate_per_minute:
        raise ApiError(429, "登录过于频繁，请稍后再试")
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
    return LoginResult(token=_issue_session(username), user=to_vo(user))


def _issue_session(username: str) -> str:
    """签发会话票并登记到会话注册表；返回票本身（调用方只把它写进 Cookie）。

    登录与改密共用这一条登记路径。登记成功后才返回 token；本地回退表满时
    返回可重试错误，不签出无法被「退出所有设备」找到的会话。
    """
    ttl = settings.jwt_expire_hours * 3600
    token = jwt_tokens.encode_token(username, settings.jwt_secret, ttl)
    claims = jwt_tokens.decode_token(token, settings.jwt_secret)
    sessions.register_session(username, claims["jti"], token, jwt_tokens.remaining_seconds(claims))
    return token


def logout(raw_token: str | None) -> None:
    token = _normalize(raw_token)
    if not token:
        return
    try:
        claims = jwt_tokens.decode_token(token, settings.jwt_secret)
        ttl = jwt_tokens.remaining_seconds(claims)
        sessions.revoke_current(claims.get("sub", ""), claims.get("jti", ""), token, ttl)
    except jwt_tokens.JwtError:
        # 无效/过期 token：登出仍视为成功（幂等）
        return


def logout_all(username: str) -> int:
    if not username:
        return 0
    return sessions.revoke_all(username)


def change_password(username: str, old_password: str, new_password: str, ip: str) -> str:
    """改密：验原口令 → 重新哈希落库 → 杀掉该账号全部已登记会话 → 给当前设备签一张新票。

    身份只取**认证主体**：`username` 由路由从 JWT 的 sub 传入，请求体里没有用户名字段，
    所以「A 改 B 的密码」在这份签名上无法表达——没有可以填别人用户名的地方。
    返回值只准进 HttpOnly Cookie（同 `login`）：不进响应体，也不进日志。

    新口令的门槛与注册**同一份规则、同一个切分**（哲学 #1，阈值不重写第二遍）：
    长度/字符集/非空由请求体校验器借用 `RegisterBody` 的校验器把关（见
    `app/api/business/auth.py` 的 `ChangePasswordBody`，400 文案走既有的信封映射），
    这里只补注册同样落在服务层的那半条：字母+数字。
    """
    throttle_key = sessions.password_fail_key(ip, username)
    if sessions.sliding_count(throttle_key, sessions.LOGIN_WINDOW_SECONDS) >= sessions.MAX_LOGIN_FAILURES:
        raise ApiError(429, "原密码错误次数过多，请稍后再试")
    user = _find_user(username)
    if user is None:
        # 票还有效但账号已查不到（软删等）：与 info() 同口径按未登录处理，不解释原因
        raise ApiError(401, "用户不存在")
    if not verify_password(old_password, user.password):
        sessions.sliding_hit(throttle_key, sessions.LOGIN_WINDOW_SECONDS)
        raise ApiError(400, "原密码错误")
    if not _complex_enough(new_password):
        raise ApiError(400, "密码需同时包含字母和数字")

    with session_scope() as session:
        target = session.get(SysUser, user.id)
        if target is None:
            raise ApiError(401, "用户不存在")
        # 重新哈希（bcrypt $2a$，与注册同一条 `hash_password`）：存量哈希不因改密换形态
        target.password = hash_password(new_password)
    # 落库成功之后才撤销：反过来的话，一次改密失败会先把整账号的会话打死
    revoked = sessions.revoke_all(username)
    token = _issue_session(username)
    # 只记「撤销了几张票」这件事本身：用户名、口令、新旧 token 一律不落日志
    logger.info("password changed; revoked_sessions=%d", revoked)
    return token


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
