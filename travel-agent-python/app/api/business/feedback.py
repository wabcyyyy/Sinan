"""条目反馈路由（SPEC C3.5 反馈回路·记录链路）：`/api/itinerary/{id}/feedback`。

- `POST /api/itinerary/{id}/feedback`：upsert 一人一条（读面皆可写，Q1）；
- `DELETE /api/itinerary/{id}/feedback/{itemId}`：撤销自己的反馈（仅本人）；
- `GET /api/itinerary/{id}/feedback`：返回**自己**的反馈列表（前端回显勾选态）。

`item_feedback` addon 门控（默认关，关=404 隐藏能力存在性）；鉴权
`enforce_business_auth`（守卫机检要求每条业务路由声明）。不做聚合统计端点、
跨行程反馈查询与点赞类社交语义（SPEC §3 明确不做）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common import addons
from app.common.envelope import ApiError, ok
from app.schemas.feedback import FeedbackCreate
from app.services import feedback_service

router = APIRouter(
    prefix="/api/itinerary",
    tags=["feedback"],
    dependencies=[Depends(enforce_business_auth), Depends(addons.require_addon("item_feedback"))],
)


def _user(user: AuthUser | None) -> AuthUser:
    if user is None:
        raise ApiError(401, "请先登录")
    return user


@router.post("/{id}/feedback")
def submit_feedback(
    body: FeedbackCreate,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    return ok(feedback_service.upsert(_user(user).id, id, body))


@router.delete("/{id}/feedback/{itemId}")
def revoke_feedback(
    id: int = Path(..., ge=1),
    itemId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    feedback_service.revoke(_user(user).id, id, itemId)
    return ok()


@router.get("/{id}/feedback")
def list_my_feedback(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    return ok(feedback_service.list_mine(_user(user).id, id))
