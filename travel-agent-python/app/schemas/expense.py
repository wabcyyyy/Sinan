"""Expense contracts: Decimal inputs, string amounts on the wire, no currency conversion."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import WireModel

ExpenseCategory = Literal["attraction", "food", "hotel", "transport", "shopping", "other"]
AmountDecimal = Annotated[
    Decimal,
    Field(gt=0, le=Decimal("99999999.99"), decimal_places=2, allow_inf_nan=False),
]
Currency = Annotated[str, Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")]


class ExpenseCreate(WireModel):
    model_config = ConfigDict(extra="forbid")

    category: ExpenseCategory
    amount: AmountDecimal
    currency: Currency = "CNY"
    dayNo: int | None = Field(default=None, ge=1)
    itemId: int | None = Field(default=None, ge=1)
    spentAt: date | None = None
    paymentMethod: str | None = Field(default=None, max_length=24)
    note: str | None = Field(default=None, max_length=255)


class ExpenseUpdate(WireModel):
    """Omitted fields stay unchanged; explicit null clears nullable columns only."""

    model_config = ConfigDict(extra="forbid")

    category: ExpenseCategory | None = None
    amount: AmountDecimal | None = None
    currency: Currency | None = None
    dayNo: int | None = Field(default=None, ge=1)
    itemId: int | None = Field(default=None, ge=1)
    spentAt: date | None = None
    paymentMethod: str | None = Field(default=None, max_length=24)
    note: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def reject_nonnullable_null(self) -> Self:
        for field in ("category", "amount", "currency"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不能为空")
        return self


class ExpenseVO(WireModel):
    id: int
    itineraryId: int
    userId: int
    category: ExpenseCategory
    amount: str
    currency: Currency
    dayNo: int | None = None
    itemId: int | None = None
    spentAt: str | None = None
    paymentMethod: str | None = None
    note: str | None = None
    createdAt: str


class ExpenseCategoryTotal(BaseModel):
    category: ExpenseCategory
    currency: Currency
    amount: str


class ExpenseListVO(BaseModel):
    expenses: list[ExpenseVO]
    totals: list[ExpenseCategoryTotal]
