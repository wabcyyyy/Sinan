"""认证与用户信息端点（移植自 Java `AuthController`）。

两个 router 的鉴权方向不同，刻意分开：
- `auth_router`（/api/auth/**）公开，靠按 IP 的滑动窗口限速挡暴力；
- `user_router`（/api/user/**）挂 router 级默认拒绝依赖，未登录 401。

Cookie 属性逐字对齐 Java `AuthCookieSupport`：名 `TA_AUTH`、HttpOnly、Path=/、
SameSite=Lax、Max-Age=max(60, expire_hours*3600)、Secure 由配置决定。
CSRF 面：Java 侧关闭了 Spring CSRF 保护，靠 SameSite=Lax 挡跨站 POST——
本服务同样只接受 POST 变更会话，且 Lax 下跨站 POST 不带 Cookie，口径不变。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ValidationError, field_validator

from app.api.deps import COOKIE_NAME, AuthUser, extract_token
from app.api.security import enforce_business_auth
from app.common.client_ip import client_ip
from app.common.config import settings
from app.common.envelope import ApiError, ok
from app.schemas.business.auth import LoginBody, RegisterBody
from app.services import user_service

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
user_router = APIRouter(prefix="/api/user", tags=["user"], dependencies=[Depends(enforce_business_auth)])

#: `ChangePasswordBody` 借注册侧校验器把关口令形态时用的占位用户名。
#: 它必然通过 `RegisterBody` 的用户名规则，所以那条校验只可能因**口令**失败。
_PASSWORD_SHAPE_PROBE_USERNAME = "shapeprobe"


class ChangePasswordBody(BaseModel):
    """`POST /api/user/password` 请求体：只有两个口令，**刻意没有 username 字段**。

    - 身份取信已验签的会话（JWT 的 sub），不由请求体指定——否则这条端点自己就变成
      「拿自己的票改别人密码」的越权面；
    - 新口令的形态阈值不在这里重写第二份（AGENTS.md 哲学 #1）：长度 6-24、字符集、
      非空全部向 `app/schemas/business/auth.py` 的 `RegisterBody` 校验器要答案，
      失败文案因此与注册一字不差，并走既有的 400 信封映射；「字母+数字」那半条
      与注册同样落在服务层（`user_service.change_password`）。
    """

    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _new_password_rules(cls, value: str) -> str:
        try:
            RegisterBody(username=_PASSWORD_SHAPE_PROBE_USERNAME, password=value)
        except ValidationError as exc:
            # `ctx["error"]` 是校验器抛出的原始 ValueError，其文本就是给用户看的契约文案；
            # pydantic 的 `msg` 会再加一层 "Value error, " 前缀，不该出现在响应里。
            first = exc.errors()[0]
            cause = (first.get("ctx") or {}).get("error")
            raise ValueError(str(cause) if cause is not None else str(first.get("msg"))) from None
        return value


def _write_cookie(response: Response, token: str) -> None:
    max_age = max(60, settings.jwt_expire_hours * 3600)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
        max_age=max_age,
    )


def _clear_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite="lax", secure=settings.auth_cookie_secure)


@auth_router.post("/register")
def register(body: RegisterBody, request: Request) -> dict:
    user_service.register(body.username, body.password, body.nickname, client_ip(request), body.inviteCode)
    return ok()


@auth_router.post("/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    result = user_service.login(body.username, body.password, client_ip(request))
    # body 不回传 JWT：凭据只进 HttpOnly Cookie
    _write_cookie(response, result.token)
    return ok({"user": result.user})


@auth_router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    token, _source = extract_token(request.headers.get("Authorization"), request.cookies)
    user_service.logout(token)
    _clear_cookie(response)
    return ok()


@auth_router.post("/logout-all")
def logout_all(request: Request, response: Response, user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    if user is None:
        raise ApiError(401, "未登录")
    revoked = user_service.logout_all(user.username)
    token, _source = extract_token(request.headers.get("Authorization"), request.cookies)
    user_service.logout(token)
    _clear_cookie(response)
    return ok({"revoked": revoked})


@user_router.get("/info")
def current_user_info(user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    if user is None:
        raise ApiError(401, "未登录")
    return ok(user_service.info(user.username))


@user_router.post("/password")
def change_password(
    body: ChangePasswordBody,
    request: Request,
    response: Response,
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    """改密：验原口令、重新哈希，并撤销该账号其余全部会话（`user_service.change_password`）。

    挂在 `/api/user/**` 而不是 `/api/auth/**` 是**安全取向**，不是归类习惯：
    `/api/auth/` 在 `PUBLIC_PATHS` 的前缀白名单里（登录注册必须匿名可达），落在它下面
    的写端点会连票都不看就放行。改密必须被挡住，所以进默认拒绝的 user 面——
    这条边界由 `tests/test_default_deny_auth.py` 机检，不靠评审肉眼。
    """
    if user is None:
        raise ApiError(401, "未登录")
    token = user_service.change_password(user.username, body.old_password, body.new_password, client_ip(request))
    # 本设备续命：旧票已随 revoke_all 进黑名单，这里换成刚签发的新票；其他设备一律掉线。
    # 新票只进 HttpOnly Cookie，不进响应体（同 login）。
    _write_cookie(response, token)
    return ok()
