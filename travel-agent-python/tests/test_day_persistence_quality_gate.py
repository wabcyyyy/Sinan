"""day_persistence.persist 的初版硬伤门禁（2026-09-30 评审遗留项）。

评审实录：行程 #7 DAY04 初版仅 2 条目，校验已报「过稀+无餐饮」却标 SUCCEEDED
展示，靠异步重生成才自愈。修复后：命中不可交付硬伤（无景点/过稀/无餐饮/空天）
的天标 PENDING、错误原因入库，条目照写；正常天行为不变（SUCCEEDED）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.common import cache_store
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain
from app.schemas.trip import DailyPlan, TripItem
from app.services import day_persistence, stay_hotels


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gate.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    cache_store.reset_for_tests()
    with db_session.session_scope() as session:
        session.add(ItineraryMain(user_id=1, title="成都4日", city="成都", days=4, persons=2, status=2))
        session.flush()
        session.add(ItineraryDay(itinerary_id=1, day_no=1, note="d1"))
        session.add(
            ItineraryDay(
                itinerary_id=1,
                day_no=2,
                note="d2",
                generation_action_id="day-1-2",
                generation_fingerprint="fp",
            )
        )
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


class _Request:
    """persist 只读 request 的 stay_nights/days（鸭子类型，与 GenerateCommand 对齐）。"""

    days = 4
    stay_nights = 3


def test_persist_summary_is_derived_after_hotel_and_checkout_normalization(db):
    plan = _healthy_plan()
    plan.note = "晚餐火锅后合江亭夜游"
    plan.theme = "黄昏登城墙"
    plan.items.append(TripItem(item_type="hotel", poi_name="酒店", start_time="18:00", end_time="18:30"))
    request = type("Request", (), {"days": 1, "stay_nights": 0})()
    with db_session.session_scope() as session:
        day = session.get(ItineraryDay, 1)
        assert day is not None
        day.generation_action_id, day.generation_fingerprint = "summary", "fp"
    day_persistence.persist(1, request, 1, plan, "summary", "fp")
    with db_session.session_scope() as session:
        day = session.get(ItineraryDay, 1)
        assert day is not None and day.note is not None and day.metadata_json is not None
        assert day.note == plan.note
        assert "合江亭" not in day.note and "火锅" not in day.note and "酒店" not in day.note
        assert "黄昏" not in day.metadata_json
    assert all(item.item_type != "hotel" for item in plan.items)


def test_late_hotel_spread_updates_previously_saved_summary_and_preserves_metadata(db):
    first = _healthy_plan()
    first.items = [item for item in first.items if item.item_type != "hotel"]
    first.practical_notes = ["出发前核实"]
    day_persistence.persist(1, _Request(), 1, first, "day-1-1", "fp")
    second = _healthy_plan()
    second.day_no = 2
    day_persistence.persist(1, _Request(), 2, second, "day-1-2", "fp")
    assert "酒店戊" not in (first.note or "")
    assert stay_hotels.spread_stay_hotels(1, 3) == 1
    with db_session.session_scope() as session:
        day = session.get(ItineraryDay, 1)
        assert day is not None and day.note and day.metadata_json
        assert "18:30–19:00 酒店戊" in day.note
        hotel = session.execute(
            select(ItineraryItem).where(ItineraryItem.day_id == 1, ItineraryItem.item_type == "hotel")
        ).scalar_one()
        assert str(hotel.start_time) == "18:30:00" and str(hotel.end_time) == "19:00:00"
        assert hotel.duration_min == 30
        assert '"出发前核实"' in day.metadata_json
    assert _item_count(1) == 5


def _sparse_plan() -> DailyPlan:
    """评审同款稀日：2 个景点、无餐饮、有效活动约 120 分钟（< MIN_ACTIVE_MINUTES）。"""
    return DailyPlan(
        day_no=1,
        note="稀的一天地铁打卡",
        items=[
            TripItem(item_type="attraction", poi_name="点位甲", start_time="10:00", end_time="11:00", cost=0),
            TripItem(item_type="attraction", poi_name="点位乙", start_time="11:30", end_time="12:30", cost=0),
        ],
    )


def _healthy_plan() -> DailyPlan:
    def _clock(hour: int, minute: int) -> str:
        return f"{hour:02d}:{minute:02d}"

    return DailyPlan(
        day_no=1,
        note="正常的一天",
        items=[
            TripItem(item_type="attraction", poi_name="景点甲", start_time="09:00", end_time="11:00", cost=50),
            TripItem(item_type="food", poi_name="餐厅乙", start_time="11:30", end_time="12:30", cost=60),
            TripItem(item_type="attraction", poi_name="景点丙", start_time="13:00", end_time="15:00", cost=0),
            TripItem(item_type="food", poi_name="餐厅丁", start_time="17:30", end_time="18:30", cost=80),
            TripItem(item_type="hotel", poi_name="酒店戊", start_time="20:00", end_time="08:00", cost=300),
        ],
    )


def _day_status(day_no: int) -> tuple[str | None, str | None]:
    with db_session.session_scope() as session:
        day = session.execute(
            select(ItineraryDay).where(ItineraryDay.itinerary_id == 1, ItineraryDay.day_no == day_no)
        ).scalar_one()
        return day.generation_status, day.generation_error


def _item_count(day_no: int) -> int:
    with db_session.session_scope() as session:
        day = session.execute(
            select(ItineraryDay).where(ItineraryDay.itinerary_id == 1, ItineraryDay.day_no == day_no)
        ).scalar_one()
        return len(
            session.execute(select(ItineraryItem).where(ItineraryItem.day_id == day.id, ItineraryItem.deleted == 0))
            .scalars()
            .all()
        )


def test_sparse_initial_version_stays_pending(db):
    """过稀初版：条目落库但状态 PENDING，不冒充完成（评审 #7 DAY04 同款）。"""
    day_persistence.persist(1, _Request(), 1, _sparse_plan(), "day-1-1", "fp")

    status, error = _day_status(1)
    assert status == "PENDING", "稀初版不得进完成态"
    assert error and "安排过稀" in error
    assert _item_count(1) == 2, "条目照写：是真实产出，用户看得到并可重生成"


def test_foodless_dense_day_delivers_with_warning_only(db):
    """无餐饮不是硬伤：半日无餐仍可交付，reflect 以警告呈现（过稀才是结构性问题）。"""
    plan = DailyPlan(
        day_no=1,
        note="纯观光一日",
        items=[
            TripItem(item_type="attraction", poi_name="景点甲", start_time="09:00", end_time="11:00", cost=50),
            TripItem(item_type="attraction", poi_name="景点乙", start_time="13:00", end_time="18:00", cost=0),
        ],
    )
    day_persistence.persist(1, _Request(), 1, plan, "day-1-1", "fp")
    status, error = _day_status(1)
    assert status == "SUCCEEDED"
    assert error is None


def test_empty_initial_version_stays_pending(db):
    plan = DailyPlan(day_no=1, items=[])
    day_persistence.persist(1, _Request(), 1, plan, "day-1-1", "fp")
    status, error = _day_status(1)
    assert status == "PENDING"
    assert error and "没有生成任何条目" in error


def test_healthy_day_still_succeeds(db):
    day_persistence.persist(1, _Request(), 2, _healthy_plan(), "day-1-2", "fp", allow_overwrite=True)
    status, error = _day_status(2)
    assert status == "SUCCEEDED"
    assert error is None
    assert _item_count(2) == 5, "day 2 ≤ stay_nights 3：酒店项照常落库（退房日是第 4 天）"


def test_saved_summary_does_not_keep_clock_rejected_by_storage(db):
    plan = _healthy_plan()
    plan.items[-1].end_time = "次日08:00"
    day_persistence.persist(1, _Request(), 1, plan, "day-1-1", "fp")
    with db_session.session_scope() as session:
        day = session.get(ItineraryDay, 1)
        hotel = session.execute(
            select(ItineraryItem).where(ItineraryItem.day_id == 1, ItineraryItem.item_type == "hotel")
        ).scalar_one()
        assert hotel.end_time is None
        assert day is not None and day.note is not None
        assert "次日08:00" not in day.note and "20:00 酒店戊" in day.note


def test_regenerated_sparse_day_overwrites_old_items_and_stays_pending(db):
    """同一天先成功后重生成出稀版：软删旧条目、新条目写入、状态退回 PENDING。"""
    day_persistence.persist(1, _Request(), 2, _healthy_plan(), "day-1-2", "fp", allow_overwrite=True)
    assert _day_status(2)[0] == "SUCCEEDED"

    sparse = _sparse_plan().model_copy(update={"day_no": 2})
    day_persistence.persist(1, _Request(), 2, sparse, "day-1-2", "fp", allow_overwrite=True)

    status, error = _day_status(2)
    assert status == "PENDING"
    assert error and "安排过稀" in error
    assert _item_count(2) == 3, "旧景点软删、住宿沿用；仍过稀，不能因补住宿而变成完成态"


def test_time_conflict_alone_does_not_block_delivery(db):
    """软问题（时间冲突/预算）不拦：reflect 循环已尽力，剩余项走质量告警。"""
    plan = _healthy_plan().model_copy(
        update={
            "day_no": 2,
            "items": [
                TripItem(item_type="attraction", poi_name="景点甲", start_time="09:00", end_time="13:00", cost=50),
                TripItem(item_type="attraction", poi_name="景点乙", start_time="12:00", end_time="14:00", cost=0),
                TripItem(item_type="food", poi_name="餐厅丙", start_time="17:30", end_time="18:30", cost=80),
            ],
        }
    )
    day_persistence.persist(1, _Request(), 2, plan, "day-1-2", "fp", allow_overwrite=True)
    status, _error = _day_status(2)
    assert status == "SUCCEEDED", "时间冲突是告警级问题，不是不可交付硬伤"
