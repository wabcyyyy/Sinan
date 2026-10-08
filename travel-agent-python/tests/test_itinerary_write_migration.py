"""M4 写路径迁移验证：行程项 CRUD、跨天移动、排序、级联删除、版本与 Diff。

四条最容易在移植中丢掉的语义，这里各有专项断言：
1. MyBatis-Plus @TableLogic 下用户侧 `delete` 是**软删**（行仍在库里、普通查询读不到）；
   唯一的例外是 restore 重建明细——软删的旧日行会继续占住 `uk_itinerary_day_no`，
   Java 侧正因此从未成功恢复过一次；
2. 每次写操作前后各一条版本快照，restore 之后还能再 restore（恢复本身也打快照）；
3. 跨天移动的快照记 `move_item` 而非 `update_item`（v2.2 §6.8）；
4. 预算口径：门票/餐饮 = 条目单价 × 人数，酒店按房型容量算房间数。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.itinerary import router as itinerary_router
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.db import session as db_session
from app.db.models import (
    Base,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.services import user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm4.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


def _seed() -> None:
    hashed = user_service.hash_password("x")
    with db_session.session_scope() as session:
        session.add_all(
            [
                SysUser(username="alice", password=hashed, status=1, role="user"),
            ]
        )
        trip = ItineraryMain(
            user_id=1,
            title="杭州2日游",
            city="杭州",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 2),
            days=2,
            persons=2,
            budget=Decimal("2000.00"),
            status=2,
        )
        session.add(trip)
        session.flush()
        first = ItineraryDay(itinerary_id=trip.id, day_no=1, generation_status="SUCCEEDED")
        second = ItineraryDay(itinerary_id=trip.id, day_no=2, generation_status="SUCCEEDED")
        session.add_all([first, second])
        session.flush()
        session.add(
            ItineraryItem(
                day_id=first.id,
                itinerary_id=trip.id,
                item_type="attraction",
                poi_name="西湖",
                cost=Decimal("0.00"),
                sort_no=0,
                verification_status="verified",
                value_kind="observed",
                freshness_status="fresh",
                review_requirement="none",
            )
        )
        session.add(
            ItineraryItem(
                day_id=first.id,
                itinerary_id=trip.id,
                item_type="food",
                poi_name="楼外楼",
                cost=Decimal("100.00"),
                sort_no=1,
            )
        )


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    client = TestClient(app, follow_redirects=False)
    client.post("/api/auth/login", json={"username": "alice", "password": "x"})
    return client


def _trip_id(client: TestClient) -> int:
    return client.get("/api/itinerary").json()["data"][0]["id"]


def _day_ids(trip_id: int) -> list[int]:
    with db_session.session_scope() as session:
        return [
            row.id
            for row in session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        ]


def _item_ids(trip_id: int, day_id: int) -> list[int]:
    with db_session.session_scope() as session:
        return [
            row.id
            for row in session.execute(
                select(ItineraryItem)
                .where(ItineraryItem.itinerary_id == trip_id, ItineraryItem.day_id == day_id)
                .order_by(ItineraryItem.sort_no)
            )
            .scalars()
            .all()
        ]


# ---------- 新增 ----------


def test_add_item_appends_to_day_end_and_recalculates_budget(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _day2 = _day_ids(trip_id)
    before = client.get(f"/api/itinerary/{trip_id}").json()["data"]["totalAmount"]

    response = client.post(
        f"/api/itinerary/{trip_id}/items",
        json={
            "dayId": day1,
            "itemType": "attraction",
            "poiName": "灵隐寺",
            "cost": 45,
        },
    )
    data = response.json()["data"]
    items = data["dayList"][0]["items"]
    assert [item["poiName"] for item in items][-1] == "灵隐寺"
    assert items[-1]["sortNo"] == 2, "新增必须落到当日末尾"
    # 餐饮 100×2人 + 新增门票 45×2人（西湖免费 0 元也计入人数口径）
    assert data["totalAmount"] > before


def test_add_item_validation_messages(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    cases = [
        ({"itemType": "attraction", "poiName": "x"}, "dayId、itemType、poiName 必填"),
        ({"dayId": day1, "itemType": "spa", "poiName": "x"}, "行程项类型不合法"),
        ({"dayId": day1, "itemType": "attraction", "poiName": "名" * 129}, "地点名称过长"),
        ({"dayId": day1, "itemType": "attraction", "poiName": "x", "cost": -1}, "费用必须在 0 到 1000000 之间"),
        (
            {"dayId": day1, "itemType": "attraction", "poiName": "x", "durationMin": 1441},
            "时长必须在 0 到 1440 分钟之间",
        ),
        ({"dayId": 99999, "itemType": "attraction", "poiName": "x"}, "日期不存在"),
    ]
    for payload, expected in cases:
        response = client.post(f"/api/itinerary/{trip_id}/items", json=payload)
        assert response.status_code in (400, 404), payload
        assert response.json()["message"] == expected, payload


def test_add_item_writes_two_snapshots(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    client.post(f"/api/itinerary/{trip_id}/items", json={"dayId": day1, "itemType": "attraction", "poiName": "断桥"})
    ops = [row["operation"] for row in client.get(f"/api/itinerary/{trip_id}/versions").json()["data"]]
    assert ops.count("add_item") == 2, "写前/写后各一条快照（同 Java）"
    versions = client.get(f"/api/itinerary/{trip_id}/versions").json()["data"]
    assert [v["versionNo"] for v in versions] == sorted([v["versionNo"] for v in versions], reverse=True)
    assert versions[0]["parentVersionId"] is not None


# ---------- 部分更新与跨天移动 ----------


def test_partial_update_does_not_clear_untouched_fact_fields(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    xihu = _item_ids(trip_id, day1)[0]
    client.put(f"/api/itinerary/items/{xihu}", json={"remark": "改备注", "startTime": "09:30:00"})
    item = client.get(f"/api/itinerary/{trip_id}").json()["data"]["dayList"][0]["items"][0]
    assert item["remark"] == "改备注"
    assert item["verificationStatus"] == "verified" and item["valueKind"] == "observed", (
        "null 表示不改：不能因局部 PUT 清空既有事实字段"
    )
    assert item["startTime"] == "09:30"


def test_put_with_day_id_moves_item_to_target_day(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, day2 = _day_ids(trip_id)
    moved = _item_ids(trip_id, day1)[1]
    client.put(f"/api/itinerary/items/{moved}", json={"dayId": day2})
    data = client.get(f"/api/itinerary/{trip_id}").json()["data"]
    assert [i["poiName"] for i in data["dayList"][0]["items"]] == ["西湖"]
    moved_row = data["dayList"][1]["items"][0]
    # 空日落到 0：先改 day_id 再取 max(sort_no) 会被 autoflush 把自己算进去
    assert moved_row["poiName"] == "楼外楼" and moved_row["sortNo"] == 0
    ops = [row["operation"] for row in client.get(f"/api/itinerary/{trip_id}/versions").json()["data"]]
    assert ops.count("move_item") == 2, "跨天移动写自己的版本操作名（v2.2 §6.8）"
    assert "update_item" not in ops


def test_move_to_day_of_another_itinerary_is_400(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    other = ItineraryMain(user_id=1, title="别的热程", city="苏州", days=1, persons=1, status=2)
    with db_session.session_scope() as session:
        session.add(other)
        session.flush()
        foreign_day = ItineraryDay(itinerary_id=other.id, day_no=1)
        session.add(foreign_day)
        session.flush()
        foreign_day_id = foreign_day.id
    item_id = _item_ids(trip_id, day1)[0]
    response = client.put(f"/api/itinerary/items/{item_id}", json={"dayId": foreign_day_id})
    assert response.status_code == 400 and response.json()["message"] == "目标日不属于该行程"
    assert client.get(f"/api/itinerary/{trip_id}/versions").json()["data"] == [], (
        "归属校验在写快照之前，被拒的请求不该留下版本历史"
    )


# ---------- 排序 ----------


def test_reorder_requires_full_day_membership(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    ids = _item_ids(trip_id, day1)
    assert (
        client.put(f"/api/itinerary/{trip_id}/days/{day1}/order", json=ids[:1]).json()["message"]
        == "行程项顺序必须包含该日期的全部行程项"
    )
    data = client.put(f"/api/itinerary/{trip_id}/days/{day1}/order", json=list(reversed(ids))).json()["data"]
    assert [i["sortNo"] for i in data["dayList"][0]["items"]] == [0, 1]


# ---------- 删除：软删等价 ----------


def test_delete_item_is_soft_delete_matching_mybatis_logic_delete(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    doomed = _item_ids(trip_id, day1)[0]
    client.delete(f"/api/itinerary/items/{doomed}")
    assert [i["id"] for i in client.get(f"/api/itinerary/{trip_id}").json()["data"]["dayList"][0]["items"]] != [doomed]
    with db_session.session_scope() as session:
        row = session.get(ItineraryItem, doomed, execution_options={"include_deleted": True})
        assert row is not None and row.deleted == 1, "@TableLogic 下 delete 是 UPDATE deleted=1，不是物理删除"


def test_delete_itinerary_soft_deletes_children(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    items_before = len(_item_ids(trip_id, day1))
    assert client.delete(f"/api/itinerary/{trip_id}").json()["code"] == 200
    assert client.get(f"/api/itinerary/{trip_id}").status_code == 404
    with db_session.session_scope() as session:
        alive = session.get(ItineraryMain, trip_id, execution_options={"include_deleted": True})
        assert alive is not None and alive.deleted == 1
        rows = (
            session.execute(
                select(ItineraryItem)
                .execution_options(include_deleted=True)
                .where(ItineraryItem.itinerary_id == trip_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == items_before and all(row.deleted == 1 for row in rows), (
            "子表同样只打软删标记，行仍在（普通查询按 @TableLogic 口径看不见它们）"
        )


# ---------- 版本 Diff 与恢复 ----------


def test_diff_reports_added_and_updated_items(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    before = client.post(
        f"/api/itinerary/{trip_id}/versions", json={"operation": "snapshot", "summary": "改动前"}
    ).json()["data"]
    client.post(
        f"/api/itinerary/{trip_id}/items",
        json={"dayId": day1, "itemType": "attraction", "poiName": "雷峰塔", "cost": 40},
    )
    after = client.get(f"/api/itinerary/{trip_id}/versions").json()["data"][0]

    diff = client.get(
        f"/api/itinerary/{trip_id}/versions/diff", params={"fromVersionId": before["id"], "toVersionId": after["id"]}
    ).json()["data"]
    assert diff["fromVersionId"] == before["id"] and diff["toVersionId"] == after["id"]
    kinds = {change["type"] for change in diff["changes"]}
    assert "added" in kinds, f"新增点位应出现在 diff 中：{diff['changes']}"


def test_restore_rebuilds_rows_and_is_itself_undoable(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    snapshot = client.post(
        f"/api/itinerary/{trip_id}/versions", json={"operation": "snapshot", "summary": "基线"}
    ).json()["data"]
    client.post(
        f"/api/itinerary/{trip_id}/items", json={"dayId": day1, "itemType": "attraction", "poiName": "后来加的"}
    )

    restored = client.post(f"/api/itinerary/{trip_id}/versions/{snapshot['id']}/restore").json()["data"]
    assert [i["poiName"] for i in restored["dayList"][0]["items"]] == ["西湖", "楼外楼"]
    ops = [row["operation"] for row in client.get(f"/api/itinerary/{trip_id}/versions").json()["data"]]
    assert "restore" in ops, "恢复本身也要打快照（可再回滚）"
    # 恢复后时间/核验状态应回到快照值
    item = restored["dayList"][0]["items"][0]
    assert item["verificationStatus"] == "verified"
    # 重建走物理删除：软删会让旧日行继续占住 uk_itinerary_day_no，restore 必然失败
    # （Java 侧 @TableLogic 正踩在这条唯一键上，该端点因此从未跑通过）
    with db_session.session_scope() as session:
        days = (
            session.execute(
                select(ItineraryDay).execution_options(include_deleted=True).where(ItineraryDay.itinerary_id == trip_id)
            )
            .scalars()
            .all()
        )
        assert [day.day_no for day in days] == [1, 2] and all(day.deleted == 0 for day in days)


# ---------- 对话草稿失效 ----------


def test_manual_edit_invalidates_pending_ai_drafts(client: TestClient) -> None:
    trip_id = _trip_id(client)
    day1, _ = _day_ids(trip_id)
    with db_session.session_scope() as session:
        from app.db.models import ItineraryChatMessage

        session.add(
            ItineraryChatMessage(
                itinerary_id=trip_id,
                user_id=1,
                role="ai",
                content="建议",
                plans_json='[{"day_no":1,"_baseRevision":"abc"}]',
                changed=1,
            )
        )
    client.post(
        f"/api/itinerary/{trip_id}/items", json={"dayId": day1, "itemType": "attraction", "poiName": "触发失效"}
    )
    history = client.get(f"/api/itinerary/{trip_id}/chat-history").json()["data"]
    assert history[0]["plans"] == [] and history[0]["changed"] is False
