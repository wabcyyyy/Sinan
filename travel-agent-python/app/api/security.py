"""业务路由的默认拒绝鉴权（对齐 Spring Security 的 `anyRequest().authenticated()`）。

为什么不是"每条路由自己写 Depends(require_user)"：那是**允许即安全**的反面——
新增端点时忘记加依赖就是匿名暴露，而且不会有任何报错。沿用白名单式放行、
其余一律要鉴权的默认方向。

agent 路由不在此列：它是内部调用面（服务间/运维直调），走 `X-Agent-Token` 内部令牌
（`app/api/agent.py:require_internal_token`），不是用户会话。
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.api.deps import AuthUser, authenticate, extract_token

# 匿名可达清单。历史上这里写的是"与 Java SecurityConfig 逐条对应、只增不删"，
# 但对照物（Java 模块）已随退役删除，那句话只会让人以为清单还有外部约束（R4-2）。
# 现在每一条都要自己站得住：
# - /api/test/hello：探活（start-all.ps1 与 CI 的健康检查目标）；
# - /api/auth/：登录/注册本身，靠按 IP 滑动窗口挡暴力（见 business/auth.py）；
# - /api/uploads/、/api/share/：分享页与上传图必须匿名可读，否则分享链接 401；
# - /api/poi-photo、/api/image-proxy：前端在登录前就要出图，且 R1-4 起各挂按 IP 限速、
#   R1-1/R1-2 起流式截断 + 拒绝 SVG。它们仍是本清单里风险最高的两条，收紧前先想清楚
#   "登录前首屏"这个理由是否还成立。
# - /api/client-errors：前端错误探针，登录页自身崩溃也要能上报（匿名是覆盖面要求，
#   不是疏漏）；滥用面由全字段上限 + 按 IP 分钟窗收（见 business/client_errors.py）。
# - /s/：分享卡外壳（GET only）。分享链接的收件人没有会话，匿名是产品语义；
#   与 /api/share/{token} 共用同一套按 IP 分钟窗限速。
PUBLIC_PATHS: tuple[str, ...] = (
    "/api/test/hello",
    "/api/auth/",
    "/api/poi-photo",
    "/api/image-proxy",
    "/api/uploads/",
    "/api/share/",
    "/api/client-errors",
    "/s/",
)

ADMIN_PATHS: tuple[str, ...] = ("/api/admin",)


def is_public_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in PUBLIC_PATHS)


def enforce_business_auth(request: Request) -> AuthUser | None:
    """router 级依赖：**总是**尝试解析身份，公开路径允许匿名，受保护路径无身份才 401。

    `permitAll` 不等于"不看票"。Spring 的 `JwtAuthenticationFilter` 对每个请求都会
    尝试认证，授权规则只决定"没有身份时能不能放行"。把它实现成"公开路径直接短路"
    会让 `/api/auth/logout-all` 这类"公开但需要知道是谁"的端点拿不到用户。
    """
    path = request.url.path
    token, _source = extract_token(request.headers.get("Authorization"), request.cookies)
    user = authenticate(token)

    if user is None and not is_public_path(path):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

    if path.startswith(ADMIN_PATHS) and (user is None or not user.is_admin):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user
