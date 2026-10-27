"""确定性重规划；显式主动取证复用预算与来源工具，确认复用 apply-plans。"""

from fastapi import APIRouter, Depends, Path

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.schemas.replanning import ConstraintRepairBody, ReplanBody, ReplanParseBody, ReplanSelectBody
from app.services import constraint_planning, itinerary_replanning

router = APIRouter()


@router.post("/{id}/constraint-repairs")
def repair_requirements(
    body: ConstraintRepairBody, id: int = Path(ge=1), user: AuthUser = Depends(enforce_business_auth)
):
    return ok(constraint_planning.revise_requirements(user.id, id, body).model_dump(mode="json", by_alias=True))


@router.post("/{id}/replans/parse")
def parse_change(body: ReplanParseBody, id: int = Path(ge=1), user: AuthUser = Depends(enforce_business_auth)):
    return ok(itinerary_replanning.parse_intent(user.id, id, body))


@router.post("/{id}/replans/compare")
def compare_change(body: ReplanBody, id: int = Path(ge=1), user: AuthUser = Depends(enforce_business_auth)):
    return ok(itinerary_replanning.compare(user.id, id, body))


@router.post("/{id}/replans/select")
def select_change(body: ReplanSelectBody, id: int = Path(ge=1), user: AuthUser = Depends(enforce_business_auth)):
    return ok(itinerary_replanning.select_option(user.id, id, body))
