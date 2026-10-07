"""M5-c 迁移验证：生成编排（建壳 → 整段流式 → 逐日修复 → 终态 → 自动续跑）。

全离线：agent 的三个入口（plan-context / generate-day / generate-stream）与富化的两个
模型调用都在编排模块的名字上被打桩，线程池在测试里同步执行，所以断言的是**编排语义**，
不是模型输出质量。最容易被迁移弄丢的六条都钉住了：

1. 校验阶梯的十句中文文案与顺序（先 DTO 再服务层规则）；
2. 生成池满 → 429 **且壳数据落 FAILED**（不能留在 GENERATING 被恢复任务误当僵尸重拉）；
3. 整段流式失败时静默退回逐日循环；单天落库失败不中断整段流；
4. 流事件契约：未知类型忽略且不计错，违规超 3 条才放弃整段；
5. 已成功的天只补登记不重写；hotel 项在超过入住晚数那天跳过且不占 sort_no；
6. 恢复任务的判定（补齐终态 / 僵尸重拉 / 失败续跑一次 / 活跃或已续跑则跳过）；
7. PR-3 断点续跑：僵尸行程先从 checkpoint 续（断在图里的天续完、研究上下文找回），
   续不动才整段重排——≥6 天流断恢复不丢天、不整段重跑。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
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
    ItineraryVersion,
    SysUser,
)
from app.schemas.trip import DailyPlan, FactEvidence, TripItem
from app.services import generation_gate, generation_recovery, itinerary_generation, state_and_sessions, user_service

JWT_MATERIAL = "example-only-hs256-test-signing-material"
PASSWORD = "example123"
CITY = "杭州"


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm5c.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    with db_session.session_scope() as session:
        session.add(SysUser(username="alice", password=user_service.hash_password(PASSWORD), status=1, role="user"))
    # 事件与幂等锁都走进程内兜底：Redis 指向不可达端口
    itinerary_generation.reset_active_planning_for_tests()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(itinerary_router)
    test = TestClient(app, follow_redirects=False)
    test.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    return test


def _plan(
    day_no: int,
    names: list[str],
    *,
    theme: str = "湖山线",
    trip_theme: str | None = None,
    suggestions: list | None = None,
) -> DailyPlan:
    return DailyPlan(
        day_no=day_no,
        note=f"第 {day_no} 天",
        theme=theme,
        trip_theme=trip_theme,
        items=[
            TripItem(
                poi_name=name,
                item_type="hotel" if name.startswith("酒店") else "attraction",
                cost=300 if name.startswith("酒店") else 45,
                # 景点给足 4 小时窗口：过稀已是落库硬伤（day_delivery_gate），
                # stub 也必须给出"可交付"形状，与 _wire_plan 的默认形状同基线
                start_time="24:00" if name.startswith("酒店") else "09:30",
                end_time=None if name.startswith("酒店") else "13:30",
                why_this="离西湖步行十分钟" if name == "西湖" else None,
                image="https://example.com/a.jpg" if name == "西湖" else None,
            )
            for name in names
        ],
        suggestions=suggestions or [],
    )


def _wire_plan(day_no: int, names: list[str]) -> dict:
    """整段流式事件里的 plan 是 camel 化 wire dict，测试必须喂真形状。

    默认形状必须过整段终检（≥1 景点 + 餐饮、有效活动 ≥240 分钟、无时间重叠），
    否则违规天会被重置进逐日循环——需要违规天时在用例里自构。
    """
    return {
        "dayNo": day_no,
        "note": f"第 {day_no} 天",
        "theme": "湖山线",
        "tripTheme": None,
        "items": [
            {"itemType": "attraction", "poiName": name, "cost": 45, "startTime": "09:00", "endTime": "11:30"}
            for name in names
        ]
        + [
            {
                "itemType": "food",
                "poiName": f"知味观{day_no}",
                "cost": 40,
                "startTime": "11:30",
                "endTime": "13:00",
            }
        ],
    }


def _fake_agents(monkeypatch, per_day: list[DailyPlan], stream_events: list[dict] | None = None) -> dict:
    """把三个 agent 入口打桩，并记录调用。`stream_events=None` 表示整段流式直接抛错。"""
    calls: dict = {"day": [], "stream": 0, "context": 0}

    monkeypatch.setattr(
        itinerary_generation,
        "run_plan_context",
        # start_date/days 为 C3.1 天气新增的可选入参，替身用 **kwargs 兼容
        lambda city, prefs, itinerary_id=None, **kwargs: (
            calls.__setitem__("context", calls["context"] + 1),
            {"candidates": [], "foods": [], "hotels": [], "consumption": None},
        )[1],
    )

    def fake_day(request):
        calls["day"].append(request)
        return per_day[min(request.day_no, len(per_day)) - 1]

    def fake_stream(request, cancel=None):
        calls["stream"] += 1
        if stream_events is None:
            raise RuntimeError("整段流式炸了")
        yield from stream_events

    monkeypatch.setattr(itinerary_generation, "run_generate_day", fake_day)
    monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", fake_stream)
    monkeypatch.setattr(itinerary_generation, "_submit_budget_recalculate", lambda itinerary_id: None)
    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", lambda task, *args: None)  # 富化留到单独用例验证
    return calls


def _run_inline(monkeypatch):
    """让"异步"提交在当前线程里同步跑完，断言才确定。"""

    def inline(task, *args):
        task(*args)
        return None

    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", inline)


def _trip(client: TestClient, body: dict) -> dict:
    response = client.post("/api/itinerary/generate", json=body)
    assert response.status_code == 200, response.json()
    return response.json()["data"]


def _rows(trip_id: int) -> tuple[ItineraryMain, list[ItineraryDay]]:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        days = (
            session.execute(
                select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id).order_by(ItineraryDay.day_no)
            )
            .scalars()
            .all()
        )
        session.expunge(main)
        for day in days:
            session.expunge(day)
        return main, list(days)


def _items(day_id: int) -> list[ItineraryItem]:
    with db_session.session_scope() as session:
        rows = (
            session.execute(select(ItineraryItem).where(ItineraryItem.day_id == day_id).order_by(ItineraryItem.sort_no))
            .scalars()
            .all()
        )
        for row in rows:
            session.expunge(row)
        return list(rows)


# ---------- 校验阶梯 ----------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"days": 2, "startDate": "2026-04-20"}, "目的地不能为空"),
        ({"city": "  ", "days": 2}, "目的地不能为空"),
        ({"city": CITY}, "出行天数不能为空"),
        ({"city": CITY, "days": 0}, "天数至少为 1 天"),
        ({"city": CITY, "days": 8}, "天数最多为 7 天"),
        ({"city": CITY, "days": 2, "persons": 0}, "人数至少为 1 人"),
        ({"city": CITY, "days": 2, "persons": 21}, "人数最多为 20 人"),
        ({"city": CITY, "days": 2, "stayNights": -1}, "住宿晚数不能为负数"),
        ({"city": CITY, "days": 2, "stayNights": 8}, "住宿晚数最多为 7 晚"),
        ({"city": CITY, "days": 2, "budget": -1}, "预算不能为负数"),
        # API-2：预算上界与 requirements 长度上界（10^10/超长文本此前会 500）
        ({"city": CITY, "days": 2, "budget": 10**10}, "预算必须在 0 到 1000000 之间"),
        ({"city": CITY, "days": 2, "budget": "1e30"}, "预算必须在 0 到 1000000 之间"),
        ({"city": CITY, "days": 2, "requirements": "要" * 10001}, "补充要求最多 10000 字"),
        ({"city": CITY, "days": 2, "intent": "意" * 801}, "旅行意图最多 800 字"),
        ({"city": "杭州!!", "days": 2}, "目的地名称格式不正确，请输入城市或景点所在城市"),
        ({"city": CITY, "days": 2, "startDate": "2026-04-21", "endDate": "2026-04-20"}, "结束日期不能早于开始日期"),
        ({"city": CITY, "days": 2, "startDate": "2026-04-20", "endDate": "2026-04-23"}, "日期范围与行程天数不一致"),
        ({"city": CITY, "days": 2, "stayNights": 3}, "住宿晚数必须在 0 到行程天数之间"),
    ],
)
def test_generate_validation_messages_match_java(client: TestClient, body, expected) -> None:
    response = client.post("/api/itinerary/generate", json=body).json()
    assert response["code"] == 400 and response["message"] == expected


# ---------- 建壳 + 编排 ----------


def test_generate_creates_shell_then_completes(client: TestClient, monkeypatch) -> None:
    calls = _fake_agents(monkeypatch, [_plan(1, ["西湖", "酒店A"], trip_theme="西子湖畔慢行"), _plan(2, ["灵隐寺"])])
    _run_inline(monkeypatch)
    detail = _trip(
        client,
        {
            "city": CITY,
            "days": 2,
            "persons": 2,
            "budget": 3000,
            "startDate": "2026-04-20",
            "endDate": "2026-04-21",
            "preferences": ["亲子"],
            "hotelTier": "舒适型",
        },
    )
    main, days = _rows(detail["id"])

    assert main.days == 2 and main.stay_nights == 1 and main.status == 2
    assert main.title == "杭州2日游" and main.gen_state == "COMPLETED"
    assert main.trip_theme == "西子湖畔慢行"
    assert str(days[1].travel_date) == "2026-04-21" and days[1].generation_status == "SUCCEEDED"
    # 整段流式被打桩成"直接抛错"，因此走的是逐日修复路径
    assert calls["stream"] == 1 and [req.day_no for req in calls["day"]] == [1, 2]
    assert calls["context"] == 1, "上下文只构建一次，逐日复用"
    assert calls["day"][1].used_names == ["西湖", "酒店A"], "跨天去重要把前一天的点位带上"
    # BIZ-2（2026-10-06 拍板改基线）：逐日链补传真实 days 修预算分档（此前
    # ¥7000/7 天被当 ¥7000/天 选奢华档）；重试预算改由 per_day_chain 独立信号
    # 保证，不再靠「不传 days」这一互斥手段保住
    assert calls["day"][1].days == 2, "逐日调用传真实 days 修预算分档"
    assert calls["day"][1].per_day_chain is True, "独立重试信号保住单日反思重试预算"
    assert calls["day"][0].needs_hotel is True and calls["day"][1].needs_hotel is False
    # 版本轨迹：建壳一条 create + 生成完成一条 generate（A5 抽屉要看的就是这两条）
    assert _versions(detail["id"]) == ["generate", "create"]


def test_hotel_only_on_stay_nights_and_sort_keeps_dense(client: TestClient, monkeypatch) -> None:
    _fake_agents(monkeypatch, [_plan(1, ["酒店A", "西湖"]), _plan(2, ["酒店B", "灵隐寺"])])
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 2, "stayNights": 1})
    _main, days = _rows(detail["id"])
    second_day = [item.poi_name for item in _items(days[1].id)]
    assert second_day == ["灵隐寺"], "第 2 天超出入住晚数 → 酒店项跳过"
    assert [item.sort_no for item in _items(days[1].id)] == [0], "跳过项不占 sort_no"


@pytest.mark.parametrize("source_day", [1, 2, 3])
def test_per_day_fallback_spreads_all_three_nights(client: TestClient, monkeypatch, source_day) -> None:
    plans = [_plan(day_no, [f"景点{day_no}"] + (["酒店A"] if day_no == source_day else [])) for day_no in range(1, 5)]
    hotel = next(item for item in plans[source_day - 1].items if item.item_type == "hotel")
    hotel.fact_evidence = {"cost": FactEvidence(value_kind="estimated", verification_status="unverified")}
    hotel.source = "llm_knowledge"
    _fake_agents(monkeypatch, plans)
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 4, "stayNights": 3})
    _main, days = _rows(detail["id"])
    for day in days[:3]:
        items = _items(day.id)
        hotels = [item for item in items if item.item_type == "hotel"]
        assert len(hotels) == 1
        assert hotels[0].poi_name == "酒店A" and hotels[0].cost == Decimal("300.00")
        assert hotels[0].source == "llm_knowledge"
        assert json.loads(hotels[0].fact_evidence_json)["cost"]["valueKind"] == "estimated"
    assert [item.sort_no for item in items] == list(range(len(items)))
    assert all(item.item_type != "hotel" for item in _items(days[3].id))
    assert itinerary_generation.stay_hotels.spread_stay_hotels(detail["id"], 3) == 0


@pytest.mark.parametrize("streaming", [False, True])
def test_generation_cannot_silently_switch_selected_hotel(client: TestClient, monkeypatch, streaming):
    plans = [_plan(day, [f"景点{day}", f"酒店{day}"]) for day in range(1, 5)]
    for day, plan in enumerate(plans):
        hotel = next(item for item in plan.items if item.item_type == "hotel")
        hotel.poi_name = "广州大厦" if day == 0 else "白天鹅宾馆"
        hotel.cost = 300 if day == 0 else 1100
        hotel.source = "llm.baseline" if day == 0 else "llm.switched"
    events = []
    for plan in plans:
        raw = _wire_plan(plan.day_no, [f"景点{plan.day_no}"])
        hotel = next(item for item in plan.items if item.item_type == "hotel")
        raw["items"].append({**hotel.model_dump(by_alias=True), "startTime": "21:00"})
        events.append({"type": "day", "plan": raw})
    events.append(
        {
            "type": "done",
            "daysExpected": 4,
            "daysEmitted": [1, 2, 3, 4],
            "complete": True,
            "tripTheme": None,
            "message": None,
        }
    )
    calls = _fake_agents(monkeypatch, plans, stream_events=events if streaming else None)
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 4, "persons": 2})
    hotels = [item for day in detail["dayList"] for item in day["items"] if item["itemType"] == "hotel"]
    assert len(hotels) == 3
    assert {(row["poiName"], row["cost"], row["source"]) for row in hotels} == {("广州大厦", 300, "llm.baseline")}
    if streaming:
        assert calls["day"] == [], "健康的流式天必须直接交付，不能靠重生成绕过断言"
    assert all(item["itemType"] != "hotel" for item in detail["dayList"][3]["items"])
    assert itinerary_generation.stay_hotels.spread_stay_hotels(detail["id"], 3) == 0


def test_regeneration_keeps_only_hotel_before_soft_deleting_old_day(client: TestClient, monkeypatch) -> None:
    initial = _wire_plan(1, [f"过满景点{i}" for i in range(7)])
    initial["items"].append({"itemType": "hotel", "poiName": "酒店A", "cost": 300, "startTime": "21:00"})
    events = (
        [{"type": "day", "plan": initial}]
        + [{"type": "day", "plan": _wire_plan(day_no, [f"景点{day_no}"])} for day_no in range(2, 5)]
        + [
            {
                "type": "done",
                "daysExpected": 4,
                "daysEmitted": [1, 2, 3, 4],
                "complete": True,
                "tripTheme": None,
                "message": None,
            }
        ]
    )
    calls = _fake_agents(monkeypatch, [_plan(1, ["修复后景点"])], stream_events=events)
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 4, "stayNights": 3})
    assert [req.day_no for req in calls["day"]] == [1]
    _main, days = _rows(detail["id"])
    assert [day.generation_status for day in days] == ["SUCCEEDED"] * 4
    assert [item.poi_name for item in _items(days[0].id)] == ["修复后景点", "酒店A"]
    assert [sum(item.item_type == "hotel" for item in _items(day.id)) for day in days] == [1, 1, 1, 0]


def test_hotel_spreading_does_not_invent_source_or_ignore_zero_nights(client: TestClient, monkeypatch) -> None:
    _fake_agents(monkeypatch, [_plan(day_no, [f"景点{day_no}"]) for day_no in range(1, 5)])
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 4, "stayNights": 3})
    _main, days = _rows(detail["id"])
    assert all(item.item_type != "hotel" for day in days for item in _items(day.id))
    _fake_agents(monkeypatch, [_plan(day_no, [f"景点{day_no}", "酒店A"]) for day_no in range(1, 5)])
    detail = _trip(client, {"city": CITY, "days": 4, "stayNights": 0})
    _main, days = _rows(detail["id"])
    assert all(item.item_type != "hotel" for day in days for item in _items(day.id))


def test_item_field_mapping_and_time_normalization(client: TestClient, monkeypatch) -> None:
    _fake_agents(monkeypatch, [_plan(1, ["西湖", "酒店A"])])
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 1, "stayNights": 1})
    _main, days = _rows(detail["id"])
    items = _items(days[0].id)
    xihu, hotel = items[0], items[1]
    assert xihu.why_note == "离西湖步行十分钟" and xihu.image_url == "https://example.com/a.jpg"
    assert xihu.start_time == datetime.strptime("09:30", "%H:%M").time()
    assert hotel.start_time.strftime("%H:%M") == "00:00", "Java 的 24:00 → 00:00 归一"
    assert hotel.cost == Decimal("300.00") and hotel.item_type == "hotel"
    assert xihu.verification_status == "unverified" and xihu.value_kind == "generated"
    metadata = json.loads(days[0].metadata_json)
    assert metadata["theme"] == "西湖" and "miniRoute" not in metadata, "空集合不落库"
    with db_session.session_scope() as session:
        trip_theme = session.get(ItineraryMain, detail["id"]).trip_theme
    assert trip_theme is None or trip_theme == "西子湖畔慢行"


def test_day_lock_prevents_concurrent_writes_for_same_day(client: TestClient, monkeypatch) -> None:
    _fake_agents(monkeypatch, [_plan(1, ["西湖"]), _plan(2, ["灵隐寺"])])
    _run_inline(monkeypatch)
    monkeypatch.setattr(itinerary_generation.generation_gate, "try_day_lock", lambda itinerary_id, day_no: day_no != 2)
    detail = _trip(client, {"city": CITY, "days": 2})
    _main, days = _rows(detail["id"])
    assert days[1].generation_status == "PENDING", "日锁被占用时该天留给下一轮，不并发写"
    assert days[0].generation_status == "SUCCEEDED"


def test_already_succeeded_day_is_not_rewritten(client: TestClient, monkeypatch) -> None:
    _fake_agents(monkeypatch, [_plan(1, ["被重写的天"])])
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 1})
    _main, days = _rows(detail["id"])
    with db_session.session_scope() as session:
        day = session.get(ItineraryDay, days[0].id)
        day.generation_status = "SUCCEEDED"
    calls = _fake_agents(monkeypatch, [_plan(1, ["第二次"])])
    _run_inline(monkeypatch)
    itinerary_generation.plan_days(1, detail["id"], _command(detail["id"]))
    assert [item.poi_name for item in _items(days[0].id)] == ["被重写的天"]
    assert calls["day"] == [], "已成功的天不再调模型"


def test_stream_path_persists_days_and_suggestion_pool(client: TestClient, monkeypatch) -> None:
    events = [
        {"type": "day", "plan": _wire_plan(1, ["西湖"])},
        {"type": "day_patch", "plan": _wire_plan(2, ["灵隐寺"])},
        {"type": "suggestions", "items": [{"name": "河坊街", "category": "attraction"}]},
        {
            "type": "done",
            "daysExpected": 2,
            "daysEmitted": [1, 2],
            "tripTheme": "西子湖畔慢行",
            "complete": True,
            "message": None,
        },
    ]
    calls = _fake_agents(monkeypatch, [_plan(1, ["不该被用到"])], stream_events=events)
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 2, "stayNights": 1})
    _main, days = _rows(detail["id"])
    assert [item.poi_name for item in _items(days[0].id)] == ["西湖", "知味观1"]
    assert [item.poi_name for item in _items(days[1].id)] == ["灵隐寺", "知味观2"]
    assert calls["day"] == [], "整段流式已覆盖全部天 → 逐日循环只做幂等登记"
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, detail["id"])
        assert json.loads(main.suggestions_json)[0]["name"] == "河坊街"
        assert main.trip_theme == "西子湖畔慢行"


def test_stream_contract_violations_are_tolerated_until_limit(client: TestClient, monkeypatch) -> None:
    # 4 条缺必填键的 day 事件 > 上限 3 → 放弃整段，落到逐日循环
    bad = [{"type": "day"} for _ in range(4)]
    calls = _fake_agents(monkeypatch, [_plan(1, ["西湖"]), _plan(2, ["灵隐寺"])], stream_events=bad)
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 2})
    assert [req.day_no for req in calls["day"]] == [1, 2]
    _main, days = _rows(detail["id"])
    assert [days[0].generation_status, days[1].generation_status] == ["SUCCEEDED", "SUCCEEDED"]


def test_unknown_stream_event_type_is_ignored_without_counting(client: TestClient, monkeypatch) -> None:
    events = [{"type": "metrics_tick"} for _ in range(9)] + [
        {"type": "day", "plan": _wire_plan(1, ["西湖"])},
        {"type": "done", "daysExpected": 1, "daysEmitted": [1], "tripTheme": None, "complete": True, "message": None},
    ]
    calls = _fake_agents(monkeypatch, [_plan(1, ["不该出现"])], stream_events=events)
    _run_inline(monkeypatch)
    _trip(client, {"city": CITY, "days": 1})
    assert calls["day"] == [], "未知类型是前向兼容事件，不该被当成协议破坏"


def test_stream_violating_day_resets_to_pending_and_regenerates(client: TestClient, monkeypatch) -> None:
    """整段流式后置终检：违规天重置 PENDING 交既有逐日循环重生成，其余天不动。"""
    # 第 1 天塞 7 个景点 > 上限 6，还伴随时间重叠/行程过满 → validate_plans 必违规
    events = [
        {"type": "day", "plan": _wire_plan(1, [f"超量点{i}" for i in range(7)])},
        {"type": "day", "plan": _wire_plan(2, ["灵隐寺"])},
        {
            "type": "done",
            "daysExpected": 2,
            "daysEmitted": [1, 2],
            "tripTheme": None,
            "complete": True,
            "message": None,
        },
    ]
    calls = _fake_agents(monkeypatch, [_plan(1, ["西湖"]), _plan(2, ["不该被用到"])], stream_events=events)
    _run_inline(monkeypatch)
    detail = _trip(client, {"city": CITY, "days": 2, "stayNights": 2})
    _main, days = _rows(detail["id"])
    assert [req.day_no for req in calls["day"]] == [1], "只有终检违规的第 1 天进逐日循环重生成"
    assert [day.generation_status for day in days] == ["SUCCEEDED", "SUCCEEDED"]
    assert [item.poi_name for item in _items(days[0].id)] == ["西湖"], "违规天被逐日循环的产出覆盖"
    assert [item.poi_name for item in _items(days[1].id)] == ["灵隐寺", "知味观2"], "合规天不被重写"


def test_day_failure_marks_day_and_trip_failed(client: TestClient, monkeypatch) -> None:
    def boom(request):
        raise ValueError("模型返回畸形 JSON" * 300)

    _fake_agents(monkeypatch, [_plan(1, ["西湖"])])
    _run_inline(monkeypatch)
    monkeypatch.setattr(itinerary_generation, "run_generate_day", boom)
    monkeypatch.setattr(itinerary_generation.generation_gate, "try_day_lock", lambda *_a: True)
    detail = _trip(client, {"city": CITY, "days": 2})
    main, days = _rows(detail["id"])
    assert main.status == 3 and main.gen_state == "FAILED"
    # 旧断言 plan_note.startswith("生成失败：")（str(exc) 原样入库）在 R2-F3 下是错的：
    # 原始异常文本（生产里是含上游 URL 的 httpx 报错）经 plan_note 直达用户副标题与
    # 聊天回显——原文只进日志，plan_note 存中性用户文案。API-1 起追加稳定错误码
    # （envelope.error_code，只含异常类别），统一文案在通道级故障时不再零诊断价值。
    assert main.plan_note == "行程生成失败，请稍后重试或调整条件重新生成（错误码 ValueError）"
    # 单天异常会中止整个逐日循环（同 Java）：第二天根本没被尝试，停在 PENDING
    assert days[0].generation_status == "FAILED" and days[1].generation_status == "PENDING"
    assert len(days[0].generation_error) <= 500, "日级失败原因按列宽截断"


def test_queue_full_returns_429_and_fails_shell(client: TestClient, monkeypatch) -> None:
    _fake_agents(monkeypatch, [_plan(1, ["西湖"])])

    def reject(task, *args):
        raise itinerary_generation.TaskRejected("queue full")

    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", reject)
    response = client.post("/api/itinerary/generate", json={"city": CITY, "days": 1}).json()
    assert response["code"] == 429 and response["message"] == "行程生成任务已满，系统繁忙，请稍后再试"
    with db_session.session_scope() as session:
        shell = session.execute(select(ItineraryMain)).scalar_one()
        # 壳不能留在 GENERATING，否则恢复任务会把它当僵尸重拉
        assert (shell.status, shell.gen_state) == (3, "FAILED")
        assert shell.plan_note == "生成失败：系统繁忙，生成队列已满"


def test_bounded_pool_rejects_after_capacity(monkeypatch) -> None:
    gate = threading.Event()
    pool = itinerary_generation._SlotExecutor("t", 1, 2)
    pool.submit(lambda: gate.wait(5))
    pool.submit(lambda: gate.wait(5))
    with pytest.raises(itinerary_generation.TaskRejected):
        pool.submit(lambda: None)
    gate.set()
    pool.shutdown()


# ---------- 自动续跑 ----------


def _command(trip_id: int) -> itinerary_generation.GenerateCommand:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
    return generation_recovery.rebuild_request(main)


def _versions(trip_id: int) -> list[str]:
    with db_session.session_scope() as session:
        return [
            row.operation
            for row in session.execute(
                select(ItineraryVersion)
                .where(ItineraryVersion.itinerary_id == trip_id)
                .order_by(ItineraryVersion.version_no.desc())
            )
            .scalars()
            .all()
        ]


@pytest.fixture
def broken_trip(client: TestClient, monkeypatch) -> int:
    """造一条"壳在、天没生成完、状态还挂着 GENERATING"的行程（不触发编排）。

    造完后清活跃注册表：broken_trip 语义是"当年提交它的进程已经死了"——
    注册表是进程内状态，死进程的注册表就是空的（P1-1 存活探测的前件）。
    """
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: None)
    _fake_agents(monkeypatch, [_plan(1, ["西湖"])])
    detail = _trip(client, {"city": CITY, "days": 2})
    itinerary_generation.reset_active_planning_for_tests()
    with db_session.session_scope() as session:
        for day in session.execute(select(ItineraryDay)).scalars().all():
            day.generation_status = "PENDING"
        main = session.get(ItineraryMain, detail["id"])
        main.status, main.gen_state = 1, "GENERATING"
        main.updated_at = datetime.now() - timedelta(minutes=30)
    return detail["id"]


def test_recovery_resubmits_zombie_generation(broken_trip: int, monkeypatch) -> None:
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    # Java 的 recoverOne 只在 failedResume 分支返回 true，僵尸重拉不改计数（也就不会触发
    # 那趟全量缓存清理）；这里保持同口径，可观察效果是"重新提交了一次"。
    assert generation_recovery.recover() == 0
    assert submitted == [broken_trip]
    with db_session.session_scope() as session:
        assert session.get(ItineraryMain, broken_trip).gen_resumed is False, "僵尸分支不算续跑，不该占用那一次性标记"


def test_regenerating_early_day_excludes_retained_later_day_not_failed_or_deleted_drafts(broken_trip, monkeypatch):
    with db_session.session_scope() as session:
        days = session.scalars(
            select(ItineraryDay).where(ItineraryDay.itinerary_id == broken_trip).order_by(ItineraryDay.day_no)
        ).all()
        days[1].generation_status = "SUCCEEDED"
        for day, name, deleted in [(days[0], "失败草稿", 0), (days[1], "后天已安排", 0), (days[1], "已删除", 1)]:
            session.add(
                ItineraryItem(
                    itinerary_id=broken_trip, day_id=day.id, item_type="attraction", poi_name=name, deleted=deleted
                )
            )
    calls = _fake_agents(monkeypatch, [_plan(1, ["新景点", "酒店A"])])
    command = _command(broken_trip)
    itinerary_generation._generate_day_with_trace(
        broken_trip,
        command,
        {},
        1,
        ["前面已安排"],
        None,
        f"day-{broken_trip}-1",
        generation_gate.request_fingerprint(command),
    )
    assert calls["day"][0].used_names == ["前面已安排", "后天已安排"]
    assert all(item.poi_name != "失败草稿" for item in _items(days[0].id))
    assert [item.poi_name for item in _items(days[1].id)] == ["后天已安排"]


def test_recovery_resumes_failed_trip_only_once(broken_trip: int, monkeypatch) -> None:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, broken_trip)
        main.status, main.gen_state = 3, "FAILED"
        for day in session.execute(select(ItineraryDay)).scalars().all():
            day.generation_status, day.generation_error = "FAILED", "模型超时"
        # 显式重写 stale 时间戳：onupdate=func.now() 在 SQLite 里是 UTC（CI 时区），
        # 不重写的话这次 UPDATE 会把 fixture 的 30 分钟前刷成“刚刚”，recover 不再命中
        main.updated_at = datetime.now() - timedelta(minutes=30)
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    generation_recovery.recover()
    assert submitted == [broken_trip]
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, broken_trip)
        assert main.gen_resumed is True and main.status == 1 and main.gen_state == "GENERATING"

    # 旧版断言在 _ACTIVE_PLANNING stub 下假绿：fixture 的 submit 是 no-op，plan_days 永不
    # 运行、注册表永不注销，"第二次不重拉"其实是存活探测挡的（resume 锁 10 分钟 TTL 内也在
    # 挡）。这里按生产路径收尾第二次尝试的前件：plan_days 真跑完（finally 注销注册表）、
    # 重拉的生成再次失败（fail_trip 有意不清 gen_resumed）、resume 锁已过期释放——
    # 此时唯一能挡住重拉的就是 gen_resumed 读取闸（防死循环护栏本体）。
    itinerary_generation._unregister_planning(broken_trip)
    generation_gate.release_resume_lock(broken_trip)
    submitted.clear()
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, broken_trip)
        main.status, main.gen_state = 3, "FAILED"
        for day in session.execute(select(ItineraryDay)).scalars().all():
            day.generation_status, day.generation_error = "FAILED", "模型超时"
        main.updated_at = datetime.now() - timedelta(minutes=30)
    generation_recovery.recover()
    assert submitted == [], "gen_resumed=1 后再失败不许重拉（防死循环护栏必须有读取方）"


def test_recovery_completes_trip_whose_days_are_done(broken_trip: int, monkeypatch) -> None:
    with db_session.session_scope() as session:
        for day in session.execute(select(ItineraryDay)).scalars().all():
            day.generation_status = "SUCCEEDED"
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    assert generation_recovery.recover() == 1
    assert submitted == [], "数据齐了只需补终态，不该重新花钱生成"
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, broken_trip)
        assert main.status == 2 and main.gen_state == "COMPLETED" and main.title == "杭州2日游"


def test_recovery_skips_trip_with_active_day(broken_trip: int, monkeypatch) -> None:
    with db_session.session_scope() as session:
        first = session.execute(select(ItineraryDay).order_by(ItineraryDay.day_no)).scalars().first()
        first.generation_status = "RUNNING"
        first.updated_at = datetime.now()
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    generation_recovery.recover()
    assert submitted == [], "有天正在生成（5 分钟内心跳未过期）就不该抢跑"


def test_recovery_skips_trip_registered_active_in_process(broken_trip: int, monkeypatch) -> None:
    """P1-1 存活探测：注册表里的行程即使 updated_at 超时也不许二次放行。"""
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    itinerary_generation._register_planning(broken_trip)
    try:
        assert generation_recovery.recover() == 0
        assert submitted == [], "研究段静默 19 分钟 ≫ 5 分钟阈值：活着 ≠ 僵尸，recovery 不许双跑"
    finally:
        itinerary_generation._unregister_planning(broken_trip)
    assert generation_recovery.recover() == 0, "注销后僵尸分支照常接手（Java 口径返回 0）"
    assert submitted == [broken_trip]


def test_recovery_is_deduplicated_by_resume_lock(broken_trip: int, monkeypatch) -> None:
    from app.services import generation_gate

    assert generation_gate.try_resume_lock(broken_trip)
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    generation_recovery.recover()
    assert submitted == []


def test_recovery_six_day_stream_break_resumes_without_whole_rerun(client: TestClient, monkeypatch) -> None:
    """≥6 天 trip 流断恢复（PR-3 验收）：不丢天、不整段重跑。

    场景：6 天行程的整段流式只吐出 1-3 天（长行程 8k 截断的实况）后转逐日补齐，
    第 4 天的图跑到反思节点时"进程死亡"。恢复后：第 4 天从检查点续完（生成节点
    不重跑）、5-6 天逐日补齐；**整段流式不重跑，研究上下文从第 4 天的检查点找回**
    （run_plan_context 全程只跑一次——"僵尸整段续跑"的成本主体就此消失）。
    """
    from app.agent.generation.orchestration import day_workflow, trip_graph

    class _ProcessDeath(KeyboardInterrupt):
        """进程死亡模拟：BaseException 穿过节点的 except Exception 兜底。"""

    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: None)
    # 富化池是进程级单例，前面带 main lifespan 的用例退出时会把它关掉（本文件其他用例
    # 经 _run_inline 绕开提交点）；预算重算/行程富化与本用例断言无关，提交一律空转，
    # 免得撞上 "cannot schedule new futures after shutdown"（_submit_budget_recalculate
    # 只挡 TaskRejected，关停期的 RuntimeError 会炸穿 plan_days 主循环）。
    monkeypatch.setattr(itinerary_generation.enricher_pool, "submit", lambda task, *args: None)
    detail = _trip(client, {"city": CITY, "days": 6, "stayNights": 5})
    trip_id = detail["id"]

    counted = {"context": 0, "stream": 0, "days": []}

    def fake_context(*args, **kwargs):
        counted["context"] += 1
        return {"candidates": [{"name": "西湖"}], "foods": [], "hotels": [], "consumption": {}, "research_report": {}}

    def fake_stream(request):
        counted["stream"] += 1
        for day_no in (1, 2, 3):
            yield {"type": "day", "plan": _wire_plan(day_no, [f"流点{day_no}"])}
        yield {
            "type": "done",
            "daysExpected": 6,
            "daysEmitted": [1, 2, 3],
            "tripTheme": None,
            "complete": False,
            "message": "长行程截断：已产出 1-3 天",
        }

    def fake_once(req, *, force_fallback=False):
        counted["days"].append(req.day_no)
        return _plan(req.day_no, [f"点{req.day_no}"]), "llm"

    deaths = {"left": 1}

    def validate_plans(plans, **kwargs):
        if deaths["left"]:
            deaths["left"] -= 1
            raise _ProcessDeath()
        return [], []

    monkeypatch.setattr(itinerary_generation, "run_plan_context", fake_context)
    monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", fake_stream)
    monkeypatch.setattr(day_workflow, "generate_day_once", fake_once)
    monkeypatch.setattr(trip_graph, "validate_plans", validate_plans)

    with pytest.raises(_ProcessDeath):
        itinerary_generation.plan_days(1, trip_id, _command(trip_id))
    assert counted == {"context": 1, "stream": 1, "days": [4]}, "流断在第 3 天后：1-3 天落库，第 4 天生成完死在反思"

    # 僵尸化：把活跃心跳拨老（天 + 主表），恢复任务才会接（同 broken_trip 的口径）
    with db_session.session_scope() as session:
        for row in session.execute(select(ItineraryDay)).scalars().all():
            row.updated_at = datetime.now() - timedelta(minutes=30)
        session.get(ItineraryMain, trip_id).updated_at = datetime.now() - timedelta(minutes=30)

    # 重启后续跑：兜底排程在池里同步执行
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args))
    assert generation_recovery.recover() == 1

    _main, days = _rows(trip_id)
    assert [day.generation_status for day in days] == ["SUCCEEDED"] * 6, "不丢天：6/6 全部补齐"
    assert _main.status == 2 and _main.gen_state == "COMPLETED"
    assert counted["stream"] == 1, "不整段重跑：整段流式只跑过一次"
    assert counted["context"] == 1, "不整段研究重跑：上下文从第 4 天的检查点找回，run_plan_context 不再跑"
    assert counted["days"] == [4, 5, 6], "第 4 天从检查点续完（生成节点不重跑），5-6 天逐日补齐"
    assert [item.poi_name for item in _items(days[3].id)] == ["点4"], "第 4 天续出来的正是死前那份产出"
    assert [item.poi_name for item in _items(days[0].id)] == ["流点1", "知味观1"], "已产出天不重写"


def test_recovery_resumes_partially_failed_trip_once(broken_trip: int, monkeypatch) -> None:
    """R2-F7（2026-10-05 拍板：恢复侧收窄）：部分成功的失败行程允许恢复一次增量补天。

    旧预期为什么是错的：_resumable_failed_trip 旧口径要求「每一天都 FAILED 且错误
    非空」，day1 成 day2 败的行程两分支都不命中、永不恢复，用户唯一出路是整趟重建
    （夜审 trip 89/90/91 全是此形状）。放宽后 SUCCEEDED 天在 plan_days 幂等跳过、
    不覆盖既有内容，只补失败天；重复失败与全失败行程共用 gen_resumed 防死循环闸。
    """
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, broken_trip)
        main.status, main.gen_state = 3, "FAILED"
        days = session.execute(select(ItineraryDay).order_by(ItineraryDay.day_no)).scalars().all()
        days[0].generation_status, days[0].generation_error = "SUCCEEDED", None
        days[1].generation_status, days[1].generation_error = "FAILED", "模型超时"
        # onupdate=func.now() 会把 updated_at 刷成"刚刚"，重写回 stale 窗口（同上条测试）
        main.updated_at = datetime.now() - timedelta(minutes=30)
    submitted: list[int] = []
    monkeypatch.setattr(itinerary_generation.generation_pool, "submit", lambda task, *args: submitted.append(args[1]))
    generation_recovery.recover()
    assert submitted == [broken_trip], "部分失败行程必须命中恢复（旧口径两分支都不命中）"
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, broken_trip)
        assert main.gen_resumed is True and main.gen_state == "GENERATING"

    # 恢复后再失败：与全失败行程同一面闸——gen_resumed=1 封顶，不重拉
    itinerary_generation._unregister_planning(broken_trip)
    generation_gate.release_resume_lock(broken_trip)
    submitted.clear()
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, broken_trip)
        main.status, main.gen_state = 3, "FAILED"
        main.updated_at = datetime.now() - timedelta(minutes=30)
    generation_recovery.recover()
    assert submitted == [], "gen_resumed=1 封顶：部分失败行程同样只恢复一次"
