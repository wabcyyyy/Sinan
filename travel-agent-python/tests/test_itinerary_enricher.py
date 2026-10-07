"""生成后富化（itinerary_enricher）的行为钉：三段增强彼此独立、失败只降级。

纪律来源（模块 docstring）：A 管家讲解 / B 景点介绍 / C 备选池验证+补介绍，
任何一段失败都不影响已生成的行程本体；写回一律单列 UPDATE；收尾无条件按
(userId, itineraryId) 精确失效详情缓存。这里逐条钉住这些纪律与降级路径。
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

import app.agent  # noqa: F401  先完成 agent 门面导入：存量导入环（同 test_llm_route.py 的口径）。
from app.common.envelope import ApiError
from app.db import session as db_session
from app.db.models import Base, ItineraryDay, ItineraryItem, ItineraryMain
from app.schemas.trip import Suggestion
from app.services import generation_events, itinerary_query, llm_gateway_service
from app.services import itinerary_enricher as enricher

_REQUEST = SimpleNamespace(intent="亲子游", requirements="别太赶", region_hint="浙江")


@pytest.fixture
def sqlite_env(monkeypatch, tmp_path):
    from app.common import cache_store
    from app.services import state_and_sessions

    # 缓存/会话表是进程内状态：SQLite 自增 id 跨用例重号会串键（同 test_llm_route.py）
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    engine = create_engine(f"sqlite:///{tmp_path / 'enricher.db'}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    yield
    db_session.init_engine(None, None)
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()


def _seed_main(**overrides) -> int:
    with db_session.session_scope() as session:
        main = ItineraryMain(
            user_id=7,
            title="杭州 2 日游",
            city="杭州",
            days=2,
            persons=2,
            preferences="亲子, 步行友好",
            hotel_tier="舒适型",
            status=2,
            **overrides,
        )
        session.add(main)
        session.flush()
        return int(main.id)


def _seed_days_and_items(itinerary_id: int, names_by_day: dict[int, list[str]]) -> None:
    with db_session.session_scope() as session:
        for day_no, names in names_by_day.items():
            day = ItineraryDay(itinerary_id=itinerary_id, day_no=day_no, city="杭州")
            session.add(day)
            session.flush()
            for sort_no, name in enumerate(names, start=1):
                session.add(
                    ItineraryItem(
                        day_id=day.id,
                        itinerary_id=itinerary_id,
                        item_type="attraction",
                        poi_name=name,
                        sort_no=sort_no,
                    )
                )


def _get_main(itinerary_id: int) -> ItineraryMain:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, itinerary_id)
        assert main is not None, f"行程 {itinerary_id} 未种子化"
        return main


def _plan_note(itinerary_id: int) -> str | None:
    return _get_main(itinerary_id).plan_note


def _item_intros(itinerary_id: int) -> dict[str, str | None]:
    with db_session.session_scope() as session:
        rows = session.execute(
            select(ItineraryItem.poi_name, ItineraryItem.intro).where(ItineraryItem.itinerary_id == itinerary_id)
        ).all()
    return {name: intro for name, intro in rows}


def _stored_rows(itinerary_id: int) -> list[dict]:
    raw = _get_main(itinerary_id).suggestions_json
    return json.loads(raw) if raw else []


def _spy_events(monkeypatch) -> tuple[list, list]:
    notes: list[tuple] = []
    degraded: list[tuple] = []

    def _note(iid, length, preview):
        notes.append((iid, length, preview))

    def _degraded(iid, scope, reason, fallback):
        degraded.append((iid, scope, reason, fallback))

    monkeypatch.setattr(generation_events, "butler_note", _note)
    monkeypatch.setattr(generation_events, "degraded", _degraded)
    return notes, degraded


def _spy_evict(monkeypatch) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []
    real = itinerary_query.evict_detail
    monkeypatch.setattr(
        itinerary_query,
        "evict_detail",
        lambda user_id, itinerary_id: (calls.append((user_id, itinerary_id)), real(user_id, itinerary_id))[1],
    )
    return calls


# ---------- resolve_intent ----------


def test_resolve_intent_prefers_intent_then_requirements() -> None:
    assert enricher.resolve_intent(None) == ""
    assert enricher.resolve_intent(SimpleNamespace(intent=" 亲子游 ", requirements="别太赶")) == "亲子游"
    assert enricher.resolve_intent(SimpleNamespace(intent="  ", requirements="别太赶")) == "别太赶"
    assert enricher.resolve_intent(SimpleNamespace(intent=None, requirements=None)) == ""


# ---------- persist_suggestions ----------


def test_persist_suggestions_serializes_models_by_alias(sqlite_env) -> None:
    itinerary_id = _seed_main()

    enricher.persist_suggestions(itinerary_id, [Suggestion(name="灵隐寺", poi_id="x1")])

    rows = _stored_rows(itinerary_id)
    assert rows[0]["poiId"] == "x1", "库里形状是 camelCase（by_alias 序列化）"
    assert rows[0]["name"] == "灵隐寺" and rows[0]["category"] == "attraction"


def test_persist_suggestions_accepts_plain_dicts_and_noop_on_empty(sqlite_env) -> None:
    itinerary_id = _seed_main(suggestions_json="[]")

    enricher.persist_suggestions(itinerary_id, [{"name": "河坊街"}])
    assert _stored_rows(itinerary_id) == [{"name": "河坊街"}]

    enricher.persist_suggestions(itinerary_id, [])
    enricher.persist_suggestions(itinerary_id, None)
    assert _stored_rows(itinerary_id) == [{"name": "河坊街"}], "空池不得清掉已有内容"


def test_persist_suggestions_encode_failure_only_logs(sqlite_env, caplog) -> None:
    itinerary_id = _seed_main(suggestions_json="keep")

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        enricher.persist_suggestions(itinerary_id, [{"bad": object()}])

    assert "persist suggestions failed" in caplog.text
    assert _get_main(itinerary_id).suggestions_json == "keep", "编码失败不落库"


# ---------- enrich_itinerary 编排 ----------


def test_enrich_skips_silently_when_itinerary_missing(sqlite_env, monkeypatch) -> None:
    butler_calls: list = []
    monkeypatch.setattr(enricher, "run_butler_note", lambda *a: butler_calls.append(a))
    evict_calls = _spy_evict(monkeypatch)

    enricher.enrich_itinerary(7, 999_999, _REQUEST)

    assert butler_calls == []
    assert evict_calls == [], "行程不存在时不做缓存失效（提前返回）"


def test_enrich_happy_path_writes_note_intros_verification_and_evicts(sqlite_env, monkeypatch) -> None:
    itinerary_id = _seed_main(budget=Decimal("1000"))
    _seed_days_and_items(itinerary_id, {1: ["灵隐寺", "西湖"], 2: ["宋城", "灵隐寺", "   "]})
    with db_session.session_scope() as session:
        session.execute(
            update(ItineraryMain)
            .where(ItineraryMain.id == itinerary_id)
            .values(
                suggestions_json=json.dumps(
                    [
                        {"name": "雷峰塔", "category": "attraction", "latitude": None, "longitude": None},
                        {"name": "河坊街", "category": "attraction", "latitude": None, "longitude": None},
                    ],
                    ensure_ascii=False,
                )
            )
        )

    butler_payloads: list[dict] = []
    intro_calls: list[tuple[str, list[str], str | None]] = []
    notes, degraded = _spy_events(monkeypatch)
    evict_calls = _spy_evict(monkeypatch)

    def _butler(payload: dict) -> str:
        butler_payloads.append(payload)
        return "这是管家讲解。"

    def _intros(city: str, names: list[str], intent: str | None = None) -> dict:
        intro_calls.append((city, names, intent))
        return {n: f"{n}的介绍" for n in names}

    monkeypatch.setattr(enricher, "run_butler_note", _butler)
    monkeypatch.setattr(enricher, "run_poi_intros", _intros)

    def _verify(rows, city):
        assert city == "杭州"
        filled = [dict(row, latitude=30.25, longitude=120.16) for row in rows]
        return filled, {"filled": 2, "dropped": 0, "unresolved": 0, "skipped": 0}

    monkeypatch.setattr(enricher, "verify_suggestion_rows", _verify)

    enricher.enrich_itinerary(7, itinerary_id, _REQUEST)

    assert degraded == []
    # A 段：管家讲解写回 plan_note，并发布 butler_note 事件（长度 + 60 字预览）
    assert _plan_note(itinerary_id) == "这是管家讲解。"
    assert notes == [(itinerary_id, len("这是管家讲解。"), "这是管家讲解。")]
    payload = butler_payloads[0]
    assert payload["city"] == "杭州" and payload["days"] == 2 and payload["persons"] == 2
    assert payload["budget"] == Decimal("1000")
    assert payload["preferences"] == "亲子, 步行友好", "偏好列是逗号拼接串，迁移口径原样传字符串"
    assert payload["hotel_tier"] == "舒适型" and payload["region_hint"] == "浙江"
    assert payload["requirements"] == "别太赶" and payload["intent"] == "亲子游"
    assert payload["validation_log"] is None
    assert payload["plans"] == [
        {"day_no": 1, "items": ["灵隐寺", "西湖"]},
        {"day_no": 2, "items": ["宋城", "灵隐寺"]},
    ], "摘要按天聚合点位名、空名过滤、按 sort_no 排序"
    # B 段：介绍按 poi_name 覆盖同名列（灵隐寺跨天两行同写），空名不进批量
    assert next(names for _city, names, _intent in intro_calls) == ["灵隐寺", "西湖", "宋城"]
    intros = _item_intros(itinerary_id)
    assert intros["灵隐寺"] == "灵隐寺的介绍" and intros["西湖"] == "西湖的介绍" and intros["宋城"] == "宋城的介绍"
    assert intros["   "] is None
    assert intro_calls[0][2] == "亲子游", "意图经 resolve_intent 传给介绍生成"
    # C 段：备选池先验证回填坐标，再补介绍（两行都没有有效介绍 → 都进批量）
    rows = _stored_rows(itinerary_id)
    assert all(row["latitude"] == 30.25 for row in rows)
    assert all(row["intro"] == f"{row['name']}的介绍" for row in rows)
    # 收尾：验证写回与编排收尾各失效一次
    assert evict_calls.count((7, itinerary_id)) >= 2


def test_enrich_butler_failure_degrades_and_enrich_still_runs(sqlite_env, monkeypatch) -> None:
    itinerary_id = _seed_main()
    _seed_days_and_items(itinerary_id, {1: ["灵隐寺"]})
    notes, degraded = _spy_events(monkeypatch)
    evict_calls = _spy_evict(monkeypatch)

    def _boom(payload):
        raise RuntimeError("llm down")

    monkeypatch.setattr(enricher, "run_butler_note", _boom)
    monkeypatch.setattr(enricher, "run_poi_intros", lambda city, names, intent=None: {})

    enricher.enrich_itinerary(7, itinerary_id, _REQUEST)

    assert degraded == [(itinerary_id, "butler", "llm down", "跳过讲解")]
    assert notes == [] and _plan_note(itinerary_id) is None
    assert (7, itinerary_id) in evict_calls, "收尾无条件精确失效详情缓存"


def test_enrich_butler_skipped_when_itinerary_has_no_items(sqlite_env, monkeypatch) -> None:
    itinerary_id = _seed_main()
    butler_calls: list = []
    monkeypatch.setattr(enricher, "run_butler_note", lambda payload: butler_calls.append(payload))
    _spy_events(monkeypatch)

    enricher.enrich_itinerary(7, itinerary_id, _REQUEST)

    assert butler_calls == []
    assert _plan_note(itinerary_id) is None


def test_butler_empty_note_not_written(sqlite_env, monkeypatch) -> None:
    itinerary_id = _seed_main()
    _seed_days_and_items(itinerary_id, {1: ["灵隐寺"]})
    notes, _degraded = _spy_events(monkeypatch)
    monkeypatch.setattr(enricher, "run_butler_note", lambda payload: "   ")

    enricher._write_butler_note(7, _get_main(itinerary_id), _REQUEST, itinerary_id)

    assert notes == [] and _plan_note(itinerary_id) is None, "空白讲解不写库、不发事件"


# ---------- _fill_item_intros ----------


def test_item_intros_batch_by_eight_and_survive_batch_failure(sqlite_env, monkeypatch) -> None:
    itinerary_id = _seed_main()
    _seed_days_and_items(itinerary_id, {1: [f"名{i}" for i in range(1, 10)]})
    _notes, degraded = _spy_events(monkeypatch)
    calls: list[list[str]] = []

    def _intros(city, names, intent=None):
        calls.append(list(names))
        if len(calls) == 1:
            raise RuntimeError("first batch boom")
        return {names[0]: f"{names[0]}的介绍"}

    monkeypatch.setattr(enricher, "run_poi_intros", _intros)

    enricher._fill_item_intros(7, _get_main(itinerary_id), _REQUEST, itinerary_id)

    assert [len(chunk) for chunk in calls] == [8, 1], "9 个名字按 8 个一批切两批"
    assert calls[1] == ["名9"]
    intros = _item_intros(itinerary_id)
    assert intros["名9"] == "名9的介绍"
    assert all(intros[f"名{i}"] is None for i in range(1, 9))
    assert degraded == [], "单批失败只记日志，不算整体降级"


def test_item_intros_batch_failures_stay_quiet(sqlite_env, monkeypatch, caplog) -> None:
    """批内失败在循环内逐批捕获：只记日志，不发 degraded 事件（有后续批次就继续）。"""
    itinerary_id = _seed_main()
    _seed_days_and_items(itinerary_id, {1: ["灵隐寺"]})
    _notes, degraded = _spy_events(monkeypatch)

    def _boom(city, names, intent=None):
        raise RuntimeError("llm down")

    monkeypatch.setattr(enricher, "run_poi_intros", _boom)

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        enricher._fill_item_intros(7, _get_main(itinerary_id), _REQUEST, itinerary_id)

    assert "poi intros batch failed" in caplog.text
    assert degraded == []
    assert _item_intros(itinerary_id)["灵隐寺"] is None


def test_item_intros_write_back_failure_publishes_degraded(sqlite_env, monkeypatch) -> None:
    """degraded 的 poi_intros 帧只由外层失败（如回写异常）发布，批内失败不算。"""
    from contextlib import contextmanager

    itinerary_id = _seed_main()
    _seed_days_and_items(itinerary_id, {1: ["灵隐寺"]})
    _notes, degraded = _spy_events(monkeypatch)
    monkeypatch.setattr(enricher, "run_poi_intros", lambda city, names, intent=None: {"灵隐寺": "灵隐寺的介绍"})

    real_scope = enricher.session_scope
    entered = {"n": 0}

    @contextmanager
    def _flaky_scope():
        entered["n"] += 1
        if entered["n"] == 1:
            with real_scope() as session:
                yield session
            return
        raise RuntimeError("db write failed")

    monkeypatch.setattr(enricher, "session_scope", _flaky_scope)

    enricher._fill_item_intros(7, _get_main(itinerary_id), _REQUEST, itinerary_id)

    assert degraded == [(itinerary_id, "poi_intros", "db write failed", "跳过景点介绍")]


def test_enrich_survives_both_suggestion_leg_failures(sqlite_env, monkeypatch, caplog) -> None:
    """C 段两条腿（验证 / 补介绍）的异常都被编排层兜住：只记日志，行程收尾照常失效缓存。"""
    itinerary_id = _seed_main(suggestions_json=_suggestion_rows_json())
    _seed_days_and_items(itinerary_id, {1: ["灵隐寺"]})
    _notes, degraded = _spy_events(monkeypatch)
    evict_calls = _spy_evict(monkeypatch)
    monkeypatch.setattr(enricher, "run_butler_note", lambda payload: "讲解")
    monkeypatch.setattr(enricher, "run_poi_intros", lambda city, names, intent=None: {})

    def _boom(*_a):
        raise RuntimeError("suggestion leg down")

    monkeypatch.setattr(enricher, "verify_suggestion_rows", _boom)
    monkeypatch.setattr(enricher, "_enrich_suggestion_intros", _boom)

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        enricher.enrich_itinerary(7, itinerary_id, _REQUEST)

    assert "suggestion verification failed" in caplog.text, "验证腿异常被兜住"
    assert "suggestion intros failed" in caplog.text, "补介绍腿异常被兜住"
    assert degraded == []
    assert (7, itinerary_id) in evict_calls, "C 段失败不影响收尾缓存失效"


# ---------- 备选池：加载 / 验证 / 补介绍 ----------


def _suggestion_rows_json() -> str:
    return json.dumps(
        [
            {"name": "雷峰塔", "category": "attraction", "latitude": None, "longitude": None},
            {"name": "河坊街", "category": "attraction", "latitude": None, "longitude": None},
        ],
        ensure_ascii=False,
    )


def test_load_suggestion_rows_rejects_blank_invalid_and_non_list(sqlite_env) -> None:
    blank = _seed_main(suggestions_json=None)
    whitespace = _seed_main(suggestions_json="   ")
    broken = _seed_main(suggestions_json="not json")
    not_list = _seed_main(suggestions_json='{"name": "x"}')
    empty_list = _seed_main(suggestions_json="[]")
    valid = _seed_main(suggestions_json='[{"name": "雷峰塔"}]')
    missing = 999_999

    assert enricher._load_suggestion_rows(blank) is None
    assert enricher._load_suggestion_rows(whitespace) is None
    assert enricher._load_suggestion_rows(broken) is None
    assert enricher._load_suggestion_rows(not_list) is None
    assert enricher._load_suggestion_rows(empty_list) is None
    assert enricher._load_suggestion_rows(missing) is None
    assert enricher._load_suggestion_rows(valid) == [{"name": "雷峰塔"}]


def test_verify_suggestions_writes_back_only_when_stats_nonzero(sqlite_env, monkeypatch) -> None:
    itinerary_id = _seed_main(suggestions_json=_suggestion_rows_json())
    evict_calls = _spy_evict(monkeypatch)
    verify_calls: list[tuple[list, str]] = []

    def _verify(rows, city):
        verify_calls.append((rows, city))
        filled = [dict(row, latitude=30.25, longitude=120.16) for row in rows]
        return filled, {"filled": 1, "dropped": 1, "unresolved": 0, "skipped": 0}

    monkeypatch.setattr(enricher, "verify_suggestion_rows", _verify)

    stats = enricher._verify_suggestions(7, itinerary_id)

    assert stats == {"filled": 1, "dropped": 1, "unresolved": 0, "skipped": 0}
    assert verify_calls and verify_calls[0][1] == "杭州"
    rows = _stored_rows(itinerary_id)
    assert len(rows) == 2 and all(row["latitude"] == 30.25 for row in rows)
    assert evict_calls == [(7, itinerary_id)], "有变更才回写并失效缓存"


def test_verify_suggestions_no_writeback_on_zero_stats(sqlite_env, monkeypatch) -> None:
    raw = _suggestion_rows_json()
    itinerary_id = _seed_main(suggestions_json=raw)
    evict_calls = _spy_evict(monkeypatch)
    monkeypatch.setattr(
        enricher,
        "verify_suggestion_rows",
        lambda rows, city: (rows, {"filled": 0, "dropped": 0, "unresolved": 0, "skipped": 2}),
    )

    stats = enricher._verify_suggestions(7, itinerary_id)

    assert stats["filled"] == 0 and stats["skipped"] == 2
    assert _get_main(itinerary_id).suggestions_json == raw, "无变更不回写"
    assert evict_calls == []


def test_verify_suggestions_noop_when_pool_empty(sqlite_env, monkeypatch) -> None:
    itinerary_id = _seed_main()
    verify_calls: list = []
    monkeypatch.setattr(enricher, "verify_suggestion_rows", lambda rows, city: verify_calls.append((rows, city)))

    assert enricher._verify_suggestions(7, itinerary_id) == {}
    assert verify_calls == []


def test_enrich_suggestion_intros_only_refills_short_or_missing(sqlite_env, monkeypatch) -> None:
    long_intro = "这" * 60
    rows_json = json.dumps(
        [
            {"name": "够长", "intro": long_intro},
            {"name": "没介绍", "intro": None},
            {"name": "太短", "intro": "短短短"},
            {"name": "空串", "intro": ""},
            {"name": "null", "intro": None},
            {"name": "  ", "intro": None},
        ],
        ensure_ascii=False,
    )
    itinerary_id = _seed_main(suggestions_json=rows_json)
    main = _get_main(itinerary_id)
    calls: list[list[str]] = []

    def _intros(city, names, intent=None):
        calls.append(list(names))
        return {n: f"{n}的新介绍" * 10 for n in names}

    monkeypatch.setattr(enricher, "run_poi_intros", _intros)

    enricher._enrich_suggestion_intros(main, _REQUEST, itinerary_id)

    assert calls == [["没介绍", "太短", "空串"]], "≥50 字视为有介绍；'null'/空白名不进批量"
    stored = _stored_rows(itinerary_id)
    by_name = {row["name"]: row for row in stored}
    assert by_name["够长"]["intro"] == long_intro
    assert by_name["没介绍"]["intro"].startswith("没介绍的新介绍")
    assert by_name["太短"]["intro"].startswith("太短的新介绍")
    assert by_name["空串"]["intro"].startswith("空串的新介绍")


def test_enrich_suggestion_intros_no_store_when_nothing_changed(sqlite_env, monkeypatch) -> None:
    raw = json.dumps([{"name": "雷峰塔", "intro": None}], ensure_ascii=False)
    itinerary_id = _seed_main(suggestions_json=raw)
    main = _get_main(itinerary_id)
    monkeypatch.setattr(enricher, "run_poi_intros", lambda city, names, intent=None: {"对不上号": "介绍"})
    evict_calls = _spy_evict(monkeypatch)

    enricher._enrich_suggestion_intros(main, _REQUEST, itinerary_id)

    assert _get_main(itinerary_id).suggestions_json == raw
    assert evict_calls == [], "介绍没对上任何行就不回写、不失效缓存"


def test_enrich_suggestion_intros_batch_failure_skips_store(sqlite_env, monkeypatch, caplog) -> None:
    """补介绍批全失败：只记日志，不回写（备选池保持原样）。"""
    raw = json.dumps([{"name": "雷峰塔", "intro": None}], ensure_ascii=False)
    itinerary_id = _seed_main(suggestions_json=raw)
    main = _get_main(itinerary_id)
    evict_calls = _spy_evict(monkeypatch)

    def _boom(city, names, intent=None):
        raise RuntimeError("llm down")

    monkeypatch.setattr(enricher, "run_poi_intros", _boom)

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        enricher._enrich_suggestion_intros(main, _REQUEST, itinerary_id)

    assert "suggestion intros batch failed" in caplog.text
    assert _get_main(itinerary_id).suggestions_json == raw
    assert evict_calls == []


def test_enrich_suggestion_intros_nameless_row_left_untouched(sqlite_env, monkeypatch) -> None:
    """库里可能有无 name 键的残行：回写循环跳过它，不冒充匹配。"""
    raw = json.dumps([{"name": "雷峰塔", "intro": None}, {"intro": None}], ensure_ascii=False)
    itinerary_id = _seed_main(suggestions_json=raw)
    main = _get_main(itinerary_id)
    monkeypatch.setattr(enricher, "run_poi_intros", lambda city, names, intent=None: {"雷峰塔": "塔的新介绍"})

    enricher._enrich_suggestion_intros(main, _REQUEST, itinerary_id)

    stored = _stored_rows(itinerary_id)
    assert stored[0]["intro"] == "塔的新介绍"
    assert stored[1] == {"intro": None}, "无名残行原样保留"


def test_enrich_suggestion_intros_non_dict_row_passed_through(sqlite_env, monkeypatch) -> None:
    """JSON 数组里混入非对象元素（脏数据）：跳过不炸，随批次原样写回。"""
    raw = '["残行", {"name": "雷峰塔", "intro": null}]'
    itinerary_id = _seed_main(suggestions_json=raw)
    main = _get_main(itinerary_id)
    monkeypatch.setattr(enricher, "run_poi_intros", lambda city, names, intent=None: {"雷峰塔": "塔的新介绍"})

    enricher._enrich_suggestion_intros(main, _REQUEST, itinerary_id)

    stored = _stored_rows(itinerary_id)
    assert stored == ["残行", {"name": "雷峰塔", "intro": "塔的新介绍"}]


def test_store_suggestion_rows_encode_failure_only_logs(sqlite_env, monkeypatch, caplog) -> None:
    itinerary_id = _seed_main(suggestions_json="keep")
    evict_calls = _spy_evict(monkeypatch)

    with caplog.at_level("WARNING", logger="app.services.itinerary_enricher"):
        enricher._store_suggestion_rows(7, itinerary_id, [{"bad": object()}])

    assert "suggestions json encode failed" in caplog.text
    assert _get_main(itinerary_id).suggestions_json == "keep"
    assert evict_calls == [], "编码失败不写库也不失效缓存"


# ---------- BYOK 路由 ----------


def test_enrich_skips_when_byok_cipher_broken(sqlite_env, monkeypatch) -> None:
    """密文解不开（ApiError 409）只跳过富化：行程本体已成功，不能让 409 裸穿线程池。"""
    butler_calls: list = []
    monkeypatch.setattr(enricher, "_write_butler_note", lambda *a: butler_calls.append(a))

    def _broken(user_id):
        raise ApiError(409, "cipher broken")

    monkeypatch.setattr(llm_gateway_service, "resolve_route", _broken)

    enricher.enrich_itinerary(7, 424_242, _REQUEST)

    assert butler_calls == []
