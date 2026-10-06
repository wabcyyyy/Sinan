"""前端错误探针端点：POST /api/client-errors（匿名可达 + 按 IP 分钟窗限速）。

为什么匿名可达：探针要覆盖「登录页自己崩了」的场景，强求会话会把最有价值的
首屏错误全部丢掉。滥用面用两道闸收——请求体全字段上限（schemas/client_errors.py）
加按来源 IP 的滑动分钟窗（超限 429；前端探针 fire-and-forget，不受影响）。

为什么响应恒 ok：探针失败不能反过来打扰用户。429/422 只作为诚实的拒绝信号，
前端本就不看响应体。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Request

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.client_ip import client_ip
from app.common.envelope import ApiError, ok
from app.schemas.client_errors import ClientErrorReport
from app.services import state_and_sessions

logger = logging.getLogger("app.client_errors")

router = APIRouter(
    prefix="/api/client-errors",
    tags=["client-errors"],
    dependencies=[Depends(enforce_business_auth)],
)

_WINDOW_SECONDS = 60
_MAX_PER_MINUTE = 30


@router.post("")
def report_client_error(
    request: Request,
    body: ClientErrorReport = Body(...),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    ip = client_ip(request)
    if state_and_sessions.sliding_hit(f"client-errors:{ip}", _WINDOW_SECONDS) > _MAX_PER_MINUTE:
        raise ApiError(429, "上报过于频繁")
    detail = body.message if body.stack is None else f"{body.message}\n{body.stack}"
    logger.error(
        "[client-error] user=%s ip=%s path=%s source=%s\n%s",
        user.username if user else "-",
        ip,
        body.path or "-",
        body.source or "-",
        detail,
    )
    return ok()
