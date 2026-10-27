"""私人准备资料不进入公开分享、模板快照或 POI 库。"""

from fastapi import APIRouter, Depends

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.schemas.business.preparation import PreparationPutBody
from app.schemas.business.scenario import RestoreVersionBody
from app.services import travel_preparation

router = APIRouter(prefix="/api/itinerary", tags=["preparation"], dependencies=[Depends(enforce_business_auth)])


@router.get("/{id}/preparation")
def get_preparation(id: int, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(travel_preparation.get(user.id, id).model_dump(mode="json", by_alias=True))


@router.put("/{id}/preparation")
def put_preparation(id: int, body: PreparationPutBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(travel_preparation.put(user.id, id, body).model_dump(mode="json", by_alias=True))


@router.post("/{id}/preparation/bookings/{booking_id}/fix")
def fix_booking(
    id: int, booking_id: str, body: RestoreVersionBody, user: AuthUser = Depends(enforce_business_auth)
) -> dict:
    return ok(travel_preparation.fix_booking(user.id, id, booking_id, body))
