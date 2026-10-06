"""公开只读分享（SPEC v2.3 §6.6）：`GET /api/share/{token}` 匿名可读。

白名单（`PUBLIC_PATHS` 的 `/api/share/`）只覆盖本 router 的 GET；写接口全部在
`/api/itinerary/{id}/share`（authenticated）。按 IP 限流在服务层统一执行。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from app.api.security import enforce_business_auth
from app.common.client_ip import client_ip
from app.common.envelope import ok
from app.services import share_service

router = APIRouter(
    prefix="/api/share",
    tags=["share"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/{token}")
def view_shared(token: str, request: Request, response: Response) -> dict:
    share_service.enforce_rate_limit(client_ip(request))
    response.headers["Cache-Control"] = "no-store"
    return ok(share_service.view_shared(token))
