"""Atlas 行程图鉴（SPEC v2.3 §6.7）：登录可见的聚合读接口。

聚合口径全部在 `app/services/atlas_service`（质心自聚合 / city_geo 归国 /
coverage 契约字段），本 router 只做参数校验与信封。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.services import atlas_service

router = APIRouter(
    prefix="/api/atlas",
    tags=["atlas"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("")
def get_atlas(
    scope: str | None = Query(None, description="all|planned|visited"),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(atlas_service.build_atlas(user.id, scope))
