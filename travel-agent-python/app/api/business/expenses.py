"""记账路由（SPEC C2.1）：`/api/itinerary/**/expenses`。

- `GET/POST /api/itinerary/{id}/expenses`：逐笔列表+聚合 / 记一笔；
- `PUT/DELETE /api/itinerary/expenses/{expenseId}`：改/删一笔。
鉴权 `enforce_business_auth`，归属校验在服务层（require_main，owner-only）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ApiError, ok
from app.schemas.expense import ExpenseCreate, ExpenseUpdate
from app.services import expense_service

router = APIRouter(
    prefix="/api/itinerary",
    tags=["expenses"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/{id}/expenses")
def list_expenses(id: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    if user is None:
        raise ApiError(401, "请先登录")
    return ok(expense_service.list_expenses(user.id, id))


@router.post("/{id}/expenses")
def create_expense(
    body: ExpenseCreate,
    id: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    if user is None:
        raise ApiError(401, "请先登录")
    return ok(expense_service.create(user.id, id, body))


@router.put("/expenses/{expenseId}")
def update_expense(
    body: ExpenseUpdate,
    expenseId: int = Path(..., ge=1),
    user: AuthUser | None = Depends(enforce_business_auth),
) -> dict:
    if user is None:
        raise ApiError(401, "请先登录")
    return ok(expense_service.update(user.id, expenseId, body))


@router.delete("/expenses/{expenseId}")
def delete_expense(expenseId: int = Path(..., ge=1), user: AuthUser | None = Depends(enforce_business_auth)) -> dict:
    if user is None:
        raise ApiError(401, "请先登录")
    expense_service.delete(user.id, expenseId)
    return ok()
