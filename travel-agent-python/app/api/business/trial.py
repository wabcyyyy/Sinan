"""小规模试用管理。管理面复用默认拒绝鉴权；只公开无密钥的试用规则。"""

from fastapi import APIRouter, Depends, Query

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common import trial_billing
from app.common.config import settings
from app.common.envelope import ApiError, ok
from app.db.models import SysUser
from app.db.session import session_scope
from app.schemas.trial import TrialGrantBody, TrialInviteBody, TrialPolicyVO, TrialReconcileBody
from app.services import admin_service, trial_invites

router = APIRouter(dependencies=[Depends(enforce_business_auth)])


@router.get("/api/auth/trial-policy")
def policy() -> dict:
    return ok(
        TrialPolicyVO(
            invite_required=settings.registration_invite_required,
            billing_enabled=settings.trial_billing_enabled,
            initial_credit_cny=settings.trial_initial_credit_cny,
            daily_ai_operations=settings.user_daily_llm_runs,
        ).model_dump(by_alias=True)
    )


@router.get("/api/user/trial-wallet")
def my_wallet(user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(trial_billing.wallet_view(user.id))


@router.get("/api/admin/trial/users")
def users(page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100)) -> dict:
    result = admin_service.page_users(page, size, None)
    return ok(
        {
            "total": result["total"],
            "records": [{k: row[k] for k in ("id", "username", "status", "role")} for row in result["records"]],
        }
    )


@router.post("/api/admin/trial/invites")
def invites(body: TrialInviteBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok({"codes": trial_invites.issue(body.count, user.username)})


@router.get("/api/admin/trial/users/{user_id}/wallet")
def wallet(user_id: int) -> dict:
    return ok(trial_billing.wallet_view(user_id))


@router.post("/api/admin/trial/users/{user_id}/grants")
def grant(user_id: int, body: TrialGrantBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    with session_scope() as session:
        if session.get(SysUser, user_id) is None:
            raise ApiError(404, "用户不存在")
    trial_billing.grant(user_id, body.amount_cny, body.reason, user.username, str(body.request_id))
    return ok(trial_billing.wallet_view(user_id))


@router.post("/api/admin/trial/charges/{charge_id}/reconcile")
def reconcile(charge_id: str, body: TrialReconcileBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    trial_billing.reconcile(charge_id, body.amount_cny, body.reason, user.username)
    return ok()
