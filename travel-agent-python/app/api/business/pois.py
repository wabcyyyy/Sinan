"""加点工作台地点检索：`GET /api/pois`。

数据源 = OTM 半径池 + 联网搜索补池（无本地语料，见 app/services/poi_search.py），
与 Agent 生成时能引用的点位同源（同一 `app.agent.tools.impl.workbench_search`）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ApiError, ok
from app.services import poi_search, quota_service
from app.services.poi_search import CATEGORY_LABELS

router = APIRouter(
    prefix="/api/pois",
    tags=["pois"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("")
def list_pois(
    city: str = Query(..., min_length=1, description="目的地城市（城市字典覆盖的城市名）"),
    keywords: str | None = Query(None, description="名称关键词；留空则取该城前 30"),
    category: str | None = Query(None, description="attraction|food|hotel"),
    user: AuthUser = Depends(enforce_business_auth),
) -> dict:
    # 补池走付费联网搜索 LLM：与 itinerary 各 LLM 入口同口径，先过闸再检索（审查 P1-2）
    quota_service.enforce_llm_budget(user.id)
    if category and category not in CATEGORY_LABELS:
        raise ApiError(400, "未知的点位类别")
    return ok(poi_search.search_local(user.id, city, keywords=keywords or "", category=category))
