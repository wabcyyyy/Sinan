"""邀请码、赠额与费用查询的跨端契约。金额单位人民币元。"""

from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.common import WireModel


class TrialPolicyVO(WireModel):
    invite_required: bool
    billing_enabled: bool
    initial_credit_cny: float
    daily_ai_operations: int


class TrialEntryVO(WireModel):
    id: str
    kind: str
    status: str
    amount_cny: float
    reserved_cny: float
    model: str | None
    detail: dict[str, Any] | None
    created_at: str


class TrialWalletVO(WireModel):
    user_id: int
    balance_cny: float
    spent_cny: float
    entries: list[TrialEntryVO]


class TrialInviteBody(WireModel):
    count: int = Field(default=1, ge=1, le=100)


class TrialInvitesVO(WireModel):
    codes: list[str]


class TrialGrantBody(WireModel):
    amount_cny: float = Field(gt=0, le=100, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=200)
    request_id: UUID


class TrialReconcileBody(WireModel):
    amount_cny: float = Field(ge=0, le=100, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=200)


class TrialUserVO(WireModel):
    id: int
    username: str
    status: int | None
    role: str


class TrialUsersVO(WireModel):
    records: list[TrialUserVO]
    total: int
