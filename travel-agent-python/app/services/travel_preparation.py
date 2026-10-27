"""仅归属人可见的资料；独立 CAS，预订与官方/平台核验明确分开。"""

from sqlalchemy import select, update

from app.common.clock import PRODUCT_ZONE, shanghai_now
from app.common.envelope import ApiError
from app.db.models import ItineraryItem, ItineraryPreparation
from app.db.session import session_scope
from app.schemas.business.preparation import PreparationData, PreparationPutBody, PreparationVO
from app.schemas.business.scenario import ItemActionBody, RestoreVersionBody
from app.services import itinerary_item_action, itinerary_query


def get(user_id: int, itinerary_id: int) -> PreparationVO:
    with session_scope() as session:
        itinerary_query.require_owned_main(session, user_id, itinerary_id)
        row = session.get(ItineraryPreparation, itinerary_id)
        return PreparationVO(
            revision=row.revision if row else 0,
            data=PreparationData.model_validate(row.data_json) if row else PreparationData(),
            updatedAt=row.updated_at.replace(tzinfo=PRODUCT_ZONE).isoformat() if row else None,
        )


def put(user_id: int, itinerary_id: int, request: PreparationPutBody) -> PreparationVO:
    with session_scope() as session:
        main = itinerary_query.require_owned_main(session, user_id, itinerary_id)
        # 同时串行化首次创建与现有行写入；内容编辑仍由其自身 planningRevision 管理。
        session.refresh(main, with_for_update=True)
        row = session.get(ItineraryPreparation, itinerary_id)
        if request.revision != (row.revision if row else 0):
            raise ApiError(409, "准备资料已在另一处更新，请重新载入后再修改")
        previous = PreparationData.model_validate(row.data_json) if row else PreparationData()
        items = {
            p.id: p for p in session.scalars(select(ItineraryItem).where(ItineraryItem.itinerary_id == itinerary_id))
        }
        old_bookings = {b.id: b for b in previous.bookings}
        old_confirmations = {c.id: c for c in previous.confirmations}
        now = shanghai_now()
        data = request.data.model_copy(deep=True)
        for booking in data.bookings:
            old = old_bookings.get(booking.id)
            if booking.itemId is not None and booking.itemId not in items and (not old or old.itemId != booking.itemId):
                raise ApiError(400, "关联的安排不属于当前行程或已被删除")
        for task in data.tasks:
            if task.dayNo is not None and task.dayNo > main.days:
                raise ApiError(400, "清单关联日期超出行程")
        for record in data.confirmations:
            old = old_confirmations.get(record.id)
            if old and record.model_dump(exclude={"recordedAt"}) == old.model_dump(exclude={"recordedAt"}):
                record.recordedAt = old.recordedAt
                continue
            item = items.get(record.itemId)
            if not item or item.poi_name != record.name:
                raise ApiError(400, "核实对象已变化，请重新选择当前安排")
            record.recordedAt = now.isoformat()
        if any(item_id not in items for item_id in set(data.completedItemIds) - set(previous.completedItemIds)):
            raise ApiError(400, "完成标记只能关联当前行程的安排")
        payload = data.model_dump(mode="json", by_alias=True)
        if row is None:
            session.add(
                ItineraryPreparation(
                    itinerary_id=itinerary_id, revision=1, data_json=payload, updated_at=now.replace(tzinfo=None)
                )
            )
        else:
            result = session.execute(
                update(ItineraryPreparation)
                .where(
                    ItineraryPreparation.itinerary_id == itinerary_id, ItineraryPreparation.revision == request.revision
                )
                .values(revision=request.revision + 1, data_json=payload, updated_at=now.replace(tzinfo=None))
            )
            if getattr(result, "rowcount", 0) != 1:
                raise ApiError(409, "准备资料已更新，请重新载入")
        return PreparationVO(revision=request.revision + 1, data=data, updatedAt=now.isoformat())


def fix_booking(user_id: int, itinerary_id: int, booking_id: str, request: RestoreVersionBody) -> dict:
    with session_scope() as session:
        itinerary_query.require_owned_main(session, user_id, itinerary_id)
        booking = next((b for b in get(user_id, itinerary_id).data.bookings if b.id == booking_id), None)
        if not booking or booking.itemId is None:
            raise ApiError(400, "请先关联这份预订对应的行程安排")
        return itinerary_item_action.act(
            user_id,
            itinerary_id,
            booking.itemId,
            ItemActionBody(action="fix", planningRevision=request.planningRevision),
        )
