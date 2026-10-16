"""单进程试用版的用户生成准入；建壳与提交串行化，工作期只保留小型登记表。"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select

from app.common.config import settings
from app.common.envelope import ApiError
from app.db.models import ItineraryMain
from app.db.session import session_scope

_lock = threading.RLock()
_users: dict[int, int] = {}
_BUSY = "你已有行程正在生成，请到「我的行程」查看进度，完成后再创建"


@contextmanager
def new_trip(user_id: int, replay_id: int | None) -> Iterator[None]:
    if not settings.user_generation_limit_enabled:
        yield
        return
    with _lock:
        with session_scope() as session:
            active = session.scalars(
                select(ItineraryMain.id).where(
                    ItineraryMain.user_id == user_id, ItineraryMain.gen_state.in_(("GENERATING", "CORE_READY"))
                )
            ).all()
        ids = {*active, *([_users[user_id]] if user_id in _users else [])}
        if any(itinerary_id != replay_id for itinerary_id in ids):
            raise ApiError(409, _BUSY)
        yield


def started(user_id: int, itinerary_id: int) -> None:
    if not settings.user_generation_limit_enabled:
        return
    with _lock:
        if user_id in _users:
            raise ApiError(409, _BUSY)
        _users[user_id] = itinerary_id


def finished(itinerary_id: int) -> None:
    with _lock:
        for user_id in [user for user, trip in _users.items() if trip == itinerary_id]:
            del _users[user_id]


def reset_for_tests() -> None:
    with _lock:
        _users.clear()
