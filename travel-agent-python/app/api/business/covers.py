"""封面图库检索代理（SPEC v2.3 §6.2）。

服务端持 Unsplash key 调上游（key 绝不下发前端）；结果进进程内有界 LRU。
封面「设置/上传/恢复默认」写在 itinerary 资源下（`/api/itinerary/{id}/cover`），
见 `app/services/cover_service.py`。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.services import cover_service

router = APIRouter(
    prefix="/api/covers",
    tags=["covers"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/search")
def search_covers(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    perPage: int = Query(12, ge=1, le=30),
    provider: str = Query("unsplash"),
) -> dict:
    return ok(cover_service.search_covers(q, page, perPage, provider))
