"""M6-a PDF 导出迁移验证（Java `ExportController` + `ExportServiceImpl` + `ExportTaskRunner`）。

迁移风险不在"能不能出片"，而在三处容易被弄丢的语义：

1. **任务终态机**：创建即 RUNNING、渲染成功 DONE+file_path+finished_at、失败 FAILED+
   error_msg（截 500 字），且后台异常**一律吞掉**——渲染线程抛出去就是静默丢任务；
2. **池满 CallerRuns**：Java 用默认 taskExecutor（max8 + queue50 + CallerRunsPolicy），
   任务不丢、代价是这次请求变慢；这里必须同样兜住，而不是 429 掉用户的导出；
3. **印刷口径**：类型/来源映射成用户能读的词、时间用 Jackson 的省略格式、
   日元数据损坏只丢栏目不丢整篇。

渲染用真 reportlab + 真字体（字体在 Java 资源目录，M7 随退役挪进本仓），
所以断言的是 `%PDF` 头与页数，不是 mock 调用次数。
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api.business.auth import auth_router
from app.api.business.export import router as export_router
from app.common import cache_store, event_publisher
from app.common.config import settings
from app.common.envelope import install_exception_handlers
from app.common.task_pool import TaskRejected
from app.db import session as db_session
from app.db.models import (
    Base,
    BudgetDetail,
    ExportTask,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.services import export_pdf, export_service, generation_events, state_and_sessions

JWT_MATERIAL = "example-only-hs256-test-signing-material"
PASSWORD = "example123"
VO_FIELDS = {"id", "itineraryId", "taskType", "status", "errorMsg", "createdAt", "finishedAt", "downloadUrl"}


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm6.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    monkeypatch.setattr(settings, "jwt_secret", JWT_MATERIAL)
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "export"))
    monkeypatch.setattr(export_service, "_font_ready", False)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    event_publisher.reset_event_publisher()
    _seed()
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()
    event_publisher.reset_event_publisher()


def _seed() -> None:
    from app.services import user_service

    hashed = user_service.hash_password(PASSWORD)
    with db_session.session_scope() as session:
        session.add_all(
            [
                SysUser(username="alice", password=hashed, status=1, role="user"),
                SysUser(username="mallory", password=hashed, status=1, role="user"),
            ]
        )
        trip = ItineraryMain(
            user_id=1,
            title="杭州2日游",
            city="杭州",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 21),
            days=2,
            persons=2,
            budget=Decimal("3000.00"),
            status=2,
            preferences="亲子,美食",
        )
        foreign = ItineraryMain(user_id=2, title="别人的行程", city="北京", days=1, persons=1, status=2)
        gone = ItineraryMain(user_id=1, title="已删除行程", city="杭州", days=1, persons=1, status=2, deleted=1)
        session.add_all([trip, foreign, gone])
        session.flush()
        day = ItineraryDay(
            itinerary_id=trip.id,
            day_no=1,
            travel_date=date(2026, 4, 20),
            note="湖山线",
            generation_status="SUCCEEDED",
            metadata_json='{"theme":"把西湖走成一条动线","practicalNotes":["出发前确认游船班次","带伞"],'
            '"backupPlan":[{"name":"浙江省博"},{"name":"灵隐"}]}',
        )
        empty_day = ItineraryDay(
            itinerary_id=trip.id, day_no=2, generation_status="PENDING", metadata_json="{ 这不是合法 JSON"
        )
        session.add_all([day, empty_day])
        session.flush()
        session.add_all(
            [
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=trip.id,
                    item_type="attraction",
                    poi_name="西湖",
                    start_time=time(9, 30),
                    end_time=time(11, 0),
                    duration_min=120,
                    cost=Decimal("0.00"),
                    source="mysql.poi_knowledge",
                    open_time="全天",
                    why_note="清晨的白堤几乎没有旅行团",
                    remark="早上人少",
                    sort_no=0,
                ),
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=trip.id,
                    item_type="food",
                    poi_name="楼外楼",
                    start_time=time(12, 0),
                    duration_min=90,
                    cost=Decimal("240.00"),
                    source="some-unknown-source",
                    sort_no=1,
                ),
            ]
        )
        session.add_all(
            [
                BudgetDetail(itinerary_id=trip.id, category="门票", amount=Decimal("45.00"), item_count=2),
                BudgetDetail(itinerary_id=trip.id, category="餐饮", amount=Decimal("480.00"), item_count=4),
            ]
        )


def _app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(auth_router)
    app.include_router(export_router)
    return app


@pytest.fixture
def client() -> TestClient:
    test = TestClient(_app(), follow_redirects=False)
    test.post("/api/auth/login", json={"username": "alice", "password": PASSWORD})
    return test


@pytest.fixture
def other_user() -> TestClient:
    test = TestClient(_app(), follow_redirects=False)
    test.post("/api/auth/login", json={"username": "mallory", "password": PASSWORD})
    return test


@pytest.fixture
def anon() -> TestClient:
    return TestClient(_app(), follow_redirects=False)


@pytest.fixture
def trip_id() -> int:
    with db_session.session_scope() as session:
        return session.execute(select(ItineraryMain).where(ItineraryMain.user_id == 1)).scalars().first().id


def _run_inline(monkeypatch) -> None:
    """把渲染池换成同步执行：测的是终态机，不是线程调度。"""
    monkeypatch.setattr(export_service.export_pool, "submit", lambda task, *args: task(*args))


def _no_render(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(export_service.export_pool, "submit", lambda task, *args: calls.append(args))
    return calls


def _set_status(task_id: int, status: str, file_path: str | None = None) -> None:
    with db_session.session_scope() as session:
        task = session.get(ExportTask, task_id)
        task.status = status
        task.file_path = file_path
        task.finished_at = datetime(2026, 9, 14, 8, 0) if status != "RUNNING" else None


# ---------- 创建 ----------


def test_create_returns_running_task_without_download_url(client: TestClient, trip_id: int, monkeypatch) -> None:
    _no_render(monkeypatch)
    body = client.post(f"/api/export/pdf/{trip_id}").json()
    assert body["code"] == 200
    data = body["data"]
    # 键恒在（Jackson 连 null 一起发），但 RUNNING 阶段必须是 null：
    # 前端拿到 URL 就跳去下载会撞上 400
    assert set(data) == VO_FIELDS
    assert data["status"] == "RUNNING" and data["taskType"] == "PDF"
    assert data["itineraryId"] == trip_id and data["downloadUrl"] is None
    with db_session.session_scope() as session:
        row = session.get(ExportTask, data["id"])
        assert (row.status, row.task_type, row.user_id) == ("RUNNING", "PDF", 1)


def test_create_rejects_foreign_and_soft_deleted_itinerary(client: TestClient, monkeypatch) -> None:
    _no_render(monkeypatch)
    with db_session.session_scope() as session:
        foreign = session.execute(select(ItineraryMain).where(ItineraryMain.user_id == 2)).scalars().first()
        gone = (
            session.execute(
                select(ItineraryMain).execution_options(include_deleted=True).where(ItineraryMain.title == "已删除行程")
            )
            .scalars()
            .first()
        )
    for doomed in (foreign.id, gone.id):
        resp = client.post(f"/api/export/pdf/{doomed}")
        # 归属与软删都收敛成同一个 404 文案，不暴露资源是否存在（同 Java）
        assert resp.status_code == 404 and resp.json()["message"] == "行程不存在", doomed
        assert resp.json()["code"] == 404


def test_export_endpoints_are_not_anonymous(anon: TestClient, trip_id: int) -> None:
    assert anon.post(f"/api/export/pdf/{trip_id}").status_code == 401
    assert anon.get("/api/export/tasks/1").status_code == 401
    assert anon.get("/api/export/download/1").status_code == 401


# ---------- 渲染终态 ----------


def test_render_produces_real_pdf_and_publishes_export_done(client: TestClient, trip_id: int, monkeypatch) -> None:
    _run_inline(monkeypatch)
    events: list = []
    monkeypatch.setattr(
        generation_events,
        "publish_event",
        lambda itinerary_id, event_type, data: events.append((itinerary_id, event_type, data)),
    )

    data = client.post(f"/api/export/pdf/{trip_id}").json()["data"]

    with db_session.session_scope() as session:
        task = session.get(ExportTask, data["id"])
        assert task.status == "DONE"
        assert task.error_msg is None and task.finished_at is not None
        target = Path(task.file_path)
    assert target.parent == Path(settings.export_dir) and target.name == f"itinerary_{data['id']}.pdf"
    raw = target.read_bytes()
    assert raw[:5] == b"%PDF-" and len(raw) > 5000, "必须真出片，不能只写任务行"
    assert events == [
        (
            trip_id,
            "export_done",
            {"taskId": data["id"], "status": "DONE", "downloadUrl": f"/api/export/download/{data['id']}"},
        )
    ]

    polled = client.get(f"/api/export/tasks/{data['id']}").json()["data"]
    assert polled["status"] == "DONE" and polled["downloadUrl"] == f"/api/export/download/{data['id']}"
    downloaded = client.get(f"/api/export/download/{data['id']}")
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"].startswith("application/pdf")
    # 下载走文件流而不是 Result 信封（Java 是 ResponseEntity<Resource>）
    assert downloaded.content[:5] == b"%PDF-"
    assert f"itinerary_{data['id']}.pdf" in downloaded.headers["content-disposition"]


def test_pool_saturation_renders_inline_instead_of_failing(client: TestClient, trip_id: int, monkeypatch) -> None:
    _no_render(monkeypatch)
    monkeypatch.setattr(export_service.export_pool, "submit", _raise_rejected)
    monkeypatch.setattr(generation_events, "publish_event", lambda *args: None)
    # CallerRunsPolicy：池满时退回请求线程就地渲染，任务不丢（代价是这次响应变慢）
    data = client.post(f"/api/export/pdf/{trip_id}").json()["data"]
    assert data["status"] == "DONE" and data["downloadUrl"] is None
    with db_session.session_scope() as session:
        assert session.get(ExportTask, data["id"]).status == "DONE"


def _raise_rejected(task, *args):
    raise TaskRejected("export pool saturated")


def test_render_failure_writes_failed_task_and_swallows_the_exception(
    client: TestClient, trip_id: int, monkeypatch
) -> None:
    _run_inline(monkeypatch)
    monkeypatch.setattr(generation_events, "publish_event", lambda *args: None)
    monkeypatch.setattr(export_pdf, "build_pdf", lambda model, target: (_ for _ in ()).throw(RuntimeError("x" * 900)))

    data = client.post(f"/api/export/pdf/{trip_id}").json()["data"]
    # 后台线程的异常以任务终态呈现，不能让 200 变 500
    assert data["status"] == "FAILED"
    assert data["errorMsg"] == "x" * 500, "error_msg 列宽 512，Java 截 500"
    polled = client.get(f"/api/export/tasks/{data['id']}").json()["data"]
    assert polled["status"] == "FAILED" and polled["downloadUrl"] is None


def test_messageless_exception_falls_back_to_unknown_error(client: TestClient, trip_id: int, monkeypatch) -> None:
    _run_inline(monkeypatch)
    monkeypatch.setattr(generation_events, "publish_event", lambda *args: None)
    monkeypatch.setattr(export_pdf, "build_pdf", lambda model, target: (_ for _ in ()).throw(RuntimeError()))
    data = client.post(f"/api/export/pdf/{trip_id}").json()["data"]
    # Java 判的是 `getMessage() == null`；Python 侧 `str(exc)` 对无消息异常是空串，
    # 用 `or` 收敛才能落「未知错误」——否则前端只剩一个空 toast。
    assert data["status"] == "FAILED" and data["errorMsg"] == "未知错误"


def test_blank_but_present_message_is_kept_verbatim(client: TestClient, trip_id: int, monkeypatch) -> None:
    _run_inline(monkeypatch)
    monkeypatch.setattr(generation_events, "publish_event", lambda *args: None)
    monkeypatch.setattr(export_pdf, "build_pdf", lambda model, target: (_ for _ in ()).throw(RuntimeError("   ")))
    data = client.post(f"/api/export/pdf/{trip_id}").json()["data"]
    # 「有消息但全空白」Java 原样落库（只有 null 才替换），这里不能顺手 strip 掉
    assert data["errorMsg"] == "   "


def test_missing_font_fails_the_task_with_an_actionable_message(client: TestClient, trip_id: int, monkeypatch) -> None:
    _run_inline(monkeypatch)
    monkeypatch.setattr(generation_events, "publish_event", lambda *args: None)
    monkeypatch.setattr(settings, "export_font_file", str(Path(settings.export_dir) / "no-such-font.ttf"))
    data = client.post(f"/api/export/pdf/{trip_id}").json()["data"]
    assert data["status"] == "FAILED"
    assert "EXPORT_FONT_FILE" in data["errorMsg"], "缺字体要给出可执行的修法，而不是裸异常"


# ---------- 任务归属与下载前置条件 ----------


def test_task_reads_are_scoped_to_the_owner(
    client: TestClient, other_user: TestClient, trip_id: int, monkeypatch
) -> None:
    _run_inline(monkeypatch)
    monkeypatch.setattr(generation_events, "publish_event", lambda *args: None)
    task_id = client.post(f"/api/export/pdf/{trip_id}").json()["data"]["id"]
    for resp in (other_user.get(f"/api/export/tasks/{task_id}"), other_user.get(f"/api/export/download/{task_id}")):
        assert resp.status_code == 404 and resp.json()["message"] == "导出任务不存在"


def test_download_requires_done_and_an_existing_file(client: TestClient, trip_id: int, monkeypatch) -> None:
    _no_render(monkeypatch)
    monkeypatch.setattr(generation_events, "publish_event", lambda *args: None)
    task_id = client.post(f"/api/export/pdf/{trip_id}").json()["data"]["id"]

    early = client.get(f"/api/export/download/{task_id}")
    assert early.status_code == 400 and early.json()["message"] == "导出任务尚未完成"

    # DONE 但文件已被清理：404 而不是抛 FileNotFoundError
    _set_status(task_id, "DONE", str(Path(settings.export_dir) / "gone.pdf"))
    missing = client.get(f"/api/export/download/{task_id}")
    assert missing.status_code == 404 and missing.json()["message"] == "导出文件不存在"


# ---------- 印刷模型 ----------


def test_build_model_maps_labels_times_and_metadata(trip_id: int) -> None:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
    model = export_service.build_model(main)
    assert model["title"] == "杭州2日游" and model["totalAmount"] == Decimal("525.00")
    assert model["startDate"] == "2026-04-20" and model["budget"] == Decimal("3000.00")

    first, degraded = model["dayList"]
    assert first["theme"] == "把西湖走成一条动线"
    assert first["practicalNotes"] == ["出发前确认游船班次", "带伞"], "提示逐条下发，不合并成一句"
    assert first["backupPlan"] == "浙江省博、灵隐"
    attraction, food = first["items"]
    assert (attraction["typeLabel"], attraction["srcLabel"]) == ("景点", "目的地知识库")
    # Jackson 的零分量省略：LocalTime 09:30 不带秒
    assert attraction["startTime"] == "09:30" and attraction["endTime"] == "11:00"
    assert attraction["whyThis"] == "清晨的白堤几乎没有旅行团"
    assert food["srcLabel"] == "AI 生成", "未知来源不外露内部 key"
    assert food["startTime"] == "12:00" and food["endTime"] is None
    # 元数据损坏的日：只丢可选栏目，基础行程照常出
    assert degraded["items"] == [] and "theme" not in degraded


def test_corrupt_metadata_and_blank_columns_do_not_break_the_model(trip_id: int) -> None:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        day = (
            session.execute(select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id, ItineraryDay.day_no == 1))
            .scalars()
            .one()
        )
        day.metadata_json = '{"theme":"", "practicalNotes":["", "  "], "backupPlan":[]}'
    model = export_service.build_model(main)
    row = model["dayList"][0]
    assert "theme" not in row and "practicalNotes" not in row and "backupPlan" not in row


def test_label_tables_match_java_switches() -> None:
    assert [export_pdf.type_label(v) for v in ("attraction", "food", "hotel", "transport", "other", None)] == [
        "景点",
        "美食",
        "酒店",
        "交通",
        "other",
        "",
    ]
    # 去高德后新增 local-grounding；amap* 仅剩存量数据标签
    assert [
        export_pdf.source_label(v)
        for v in (
            None,
            "client-context",
            "mysql.poi_knowledge",
            "llm.open_day",
            "local-grounding",
            "amap-grounding",
            "amap",
            "x",
        )
    ] == [
        "行程设定",
        "行程设定",
        "目的地知识库",
        "开放研究",
        "本地知识库",
        "高德地图（存量）",
        "高德地图（存量）",
        "AI 生成",
    ]


# ---------- 版式：跨页拆分（迁移期真实踩过的坑） ----------


def test_a_day_longer_than_one_page_splits_instead_of_crashing(tmp_path: Path) -> None:
    """整日套进一个不可拆的单元格会让 reportlab 直接 LayoutError。

    模板里的 `page-break-inside: avoid` 只在日块能放进一页时成立；长日程必须能续页，
    否则最常被导出的恰恰是那种 13 项的一天。
    """
    items = [
        {
            "typeLabel": "美食",
            "poiName": f"点位 {i}",
            "startTime": "12:00",
            "endTime": None,
            "durationMin": 90,
            "cost": Decimal("88.00"),
            "srcLabel": "AI 生成",
            "source": None,
            "openTime": None,
            "remark": "现场以官方渠道为准" * 3,
            "whyThis": "顺路且不用排队" * 4,
        }
        for i in range(40)
    ]
    model = {
        "title": "长日程",
        "city": "杭州",
        "days": 1,
        "persons": 1,
        "budget": Decimal("1000.00"),
        "startDate": "2026-04-20",
        "endDate": "2026-04-20",
        "preferences": None,
        "dayList": [
            {"dayNo": 1, "travelDate": None, "note": None, "items": items},
            {"dayNo": 2, "travelDate": None, "note": None, "items": items[:1]},
        ],
        "budgetList": [{"category": f"分类{i}", "amount": Decimal("10.00"), "itemCount": i} for i in range(30)],
        "totalAmount": Decimal("300.00"),
    }
    target = tmp_path / "long.pdf"
    pages = export_pdf.build_pdf(model, target)
    assert pages >= 2
    assert target.read_bytes()[:5] == b"%PDF-"
