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

from app.api.deps import COOKIE_NAME, AuthUser, extract_token
from app.api.security import enforce_business_auth
from app.common.client_ip import client_ip
from app.common.config import settings
from app.common.envelope import ApiError, ok
from app.schemas.business.auth import LoginBody, RegisterBody
from app.services import user_service

auth_router = APIRouter(prefix="/api/auth", tags=["auth"])
user_router = APIRouter(prefix="/api/user", tags=["user"], dependencies=[Depends(enforce_business_auth)])


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
    user_service.register(body.username, body.password, body.nickname, client_ip(request))
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
