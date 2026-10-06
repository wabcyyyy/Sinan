"""整段流式生成（PR-4 归一后：统一图 mode=stream 分支）单元测试。

覆盖：
- run_generate_trip_stream：事件序列（day / day_patch / suggestions / done）、
  同日/跨天重复的边判丢弃（统一去重语义）、城市不符建议过滤、wire 形状；
- 截断/失败兜底：整段 llm_open_trip 失败 → 缺口天 llm_open_day 逐日兜底，两侧
  都失败 done 才如实带错（suggestions 随天收集，不可能从截断文本里丢——PR-4）；
- LLM 未配置抛 ValueError（业务侧据此降级逐日）。

旧旁路 trip_stream + 手写 JSON 状态机已删除，解析器测试随之退役——增量解析已
不存在，逐天产出由生成期挂点（open_plans.on_day）保证；poi_identity 原语测试
移居 tests/test_poi_identity.py。
"""

import pytest

from app.agent.generation.content import landing
from app.agent.generation.content.narrative import sanitize_narrative
from app.agent.generation.content.suggestion_city import filter_suggestions_by_city
from app.agent.generation.orchestration import open_plans, stream_branch
from app.agent.generation.orchestration.stream_branch import run_generate_trip_stream
from app.agent.grounding.grounding_evidence import issue_evidence
from app.common.config import settings
from app.schemas.trip import GenerateDayRequest

# 与旧 TRIP_JSON 同一批语义负载：day1 圣家堂（含模型自报坐标，落地边界剥离）、
# day3 圣家堂大教堂（名称变体，接地后坐标通道判重命中）、东京建议（城市过滤）。
PLANS = [
    {
        "day_no": 1,
        "theme": "高迪代表作日：圣家堂与格拉西亚大道",
        "note": "经典地标日",
        "trip_theme": "巴塞罗那·高迪之光",
        "items": [
            {
                "item_type": "attraction",
                "poi_name": "圣家堂（Sagrada Família）",
                "latitude": 41.4036,
                "longitude": 2.1744,
                "start_time": "09:00",
                "end_time": "11:30",
                "cost": 26,
                "tag": "地标",
                "why_this": "必看",
                "refs": [1],
            },
            {
                "item_type": "hotel",
                "poi_name": "文华东方酒店巴塞罗那",
                "start_time": "15:00",
                "end_time": "23:00",
                "cost": 1800,
            },
        ],
    },
    {
        "day_no": 2,
        "theme": "哥特区漫游",
        "note": "老城历史",
        "items": [
            {
                "item_type": "attraction",
                "poi_name": "巴塞罗那大教堂",
                "start_time": "10:00",
                "end_time": "11:00",
                "cost": 9,
                "tag": "人文",
                "why_this": "哥特区核心",
            },
            {
                "item_type": "food",
                "poi_name": "Los Toreros",
                "start_time": "13:00",
                "end_time": "14:00",
                "cost": 35,
            },
        ],
    },
    {
        "day_no": 3,
        "theme": "再访圣家堂周边",
        "note": "补漏",
        "items": [
            {
                "item_type": "attraction",
                "poi_name": "圣家堂大教堂",
                "latitude": 41.40362,
                "longitude": 2.17438,
                "start_time": "09:30",
                "end_time": "11:00",
                "cost": 26,
                "tag": "地标",
                "why_this": "变体重复",
            },
            {
                "item_type": "attraction",
                "poi_name": "古埃尔公园",
                "start_time": "14:00",
                "end_time": "16:00",
                "cost": 10,
            },
        ],
    },
]

TRIP_SUGGESTIONS = [
    {
        "poi_name": "米拉之家",
        "city": "巴塞罗那",
        "category": "attraction",
        "intro": "高迪代表作之一",
        "need_reservation": True,
        "estimated_cost": 24,
    },
    {
        "poi_name": "唐吉诃德涩谷",
        "city": "东京",
        "category": "shopping",
        "intro": "东京连锁免税店",
        "need_reservation": False,
    },
]


@pytest.fixture
def stream_env(monkeypatch):
    """通用桩：LLM key/整段生成注入，禁用联网补齐，点名解析按名字给坐标。

    解析桩必须给坐标：模型自报的经纬度在 LLM 输出边界被剥离（D6=C，替身按
    llm_open_trip 的输出契约先过 sanitize_narrative），坐标通道判重只可能由
    **接地后的坐标**触发——PLANS 里两条圣家堂带模型自报坐标，正好验证剥离生效。
    """
    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_generation_web_search", False)

    # 服务端解析出的权威坐标（与 PLANS 里模型自填的那对不同），两个名称变体同点
    grounded = (41.40362, 2.17438)

    def _ground(item, city):
        if "圣家堂" in str(item.get("poi_name") or ""):
            item["latitude"], item["longitude"] = grounded
            item["source"] = "nominatim"
            # 与真实 local_ground 同形状：解析成功当场签票
            issue_evidence({**item, "name": item["poi_name"], "city": city})

    monkeypatch.setattr(landing, "local_ground", _ground)
    monkeypatch.setattr(stream_branch, "fill_suggestion_gaps", lambda rows, city, **kw: rows)

    def _install(plans=None, sugg=None):
        def open_trip(req):
            rows = plans if plans is not None else PLANS
            cleaned = [sanitize_narrative(_copy(plan)) for plan in rows]
            return cleaned, [dict(s) for s in (sugg if sugg is not None else TRIP_SUGGESTIONS)]

        monkeypatch.setattr(open_plans, "llm_open_trip", open_trip)

    return _install


def _copy(plan: dict) -> dict:
    items = [dict(it) for it in plan.get("items") or []]
    return {**{k: v for k, v in plan.items() if k != "items"}, "items": items}


def _req(days: int = 3) -> GenerateDayRequest:
    return GenerateDayRequest(city="巴塞罗那", persons=2, days=days, day_no=1, needs_hotel=True, hotel_tier="豪华型")


class TestRunGenerateTripStream:
    def test_event_sequence_and_dedup(self, stream_env):
        stream_env(PLANS)
        events = list(run_generate_trip_stream(_req()))
        types = [e["type"] for e in events]
        assert types.count("day") == 3
        assert types[-1] == "done"
        done = events[-1]
        assert done["daysExpected"] == 3
        assert done["daysEmitted"] == [1, 2, 3]
        assert done["tripTheme"] == "巴塞罗那·高迪之光"

        # 跨天变体重复（圣家堂大教堂）由**接地后的坐标**通道拦截：模型自报的
        # 经纬度已在 LLM 输出边界剥离（D6=C），所以下面 day1 的坐标必须是桩给的
        # 那一对，而不是 PLANS 里的 41.4036/2.1744。
        day3 = next(e for e in events if e["type"] == "day" and e["plan"]["dayNo"] == 3)
        names = [it["poiName"] for it in day3["plan"]["items"]]
        assert "圣家堂大教堂" not in names
        assert "古埃尔公园" in names

        day1 = next(e for e in events if e["type"] == "day" and e["plan"]["dayNo"] == 1)
        sagrada = next(it for it in day1["plan"]["items"] if "圣家堂" in it["poiName"])
        assert (sagrada["latitude"], sagrada["longitude"]) == (41.40362, 2.17438)
        assert sagrada["source"] == "nominatim"

        # 备选池：东京店铺被城市校验丢弃
        suggestions = next(e for e in events if e["type"] == "suggestions")
        names = [s["name"] for s in suggestions["items"]]
        assert "米拉之家" in names
        assert "唐吉诃德涩谷" not in names

    def test_day1_wire_shape_matches_generate_day_contract(self, stream_env):
        stream_env(PLANS)
        events = list(run_generate_trip_stream(_req()))
        day1 = next(e for e in events if e["type"] == "day")["plan"]
        assert day1["dayNo"] == 1
        item = day1["items"][0]
        assert item["itemType"] == "attraction"
        assert item["poiName"].startswith("圣家堂")
        assert item["startTime"] == "09:00"
        # refs 是模型内部引用编号，不应对外透传
        assert "refs" not in item

    def test_unpooled_items_get_landed_labels(self, stream_env, monkeypatch):
        """P1-3 标签统一：未命中 ref 池的落地项也按 day 链同口径拿标签。

        存在性行（服务端解析 source+票+坐标）→ partially_verified/observed；
        完全无证据的模型自选点 → llm.open_day 的生成/估算标签——不再裸落
        schema 默认值，业务主路径（业务 /generate 的整段流式）徽章可达。
        """

        def _ground(item, city):
            # 只接地"有证据的名字"；其余项走"查无证据"路径（无 source 无坐标）
            if "古埃尔公园" in str(item.get("poi_name") or ""):
                item["latitude"], item["longitude"] = 41.41449, 2.15269
                item["source"] = "nominatim"
                issue_evidence({**item, "name": item["poi_name"], "city": city})

        monkeypatch.setattr(landing, "local_ground", _ground)
        plans = [
            {
                "day_no": 1,
                "theme": "t",
                "items": [
                    {"item_type": "attraction", "poi_name": "古埃尔公园", "start_time": "09:00", "end_time": "11:00"},
                ],
            },
            {
                "day_no": 2,
                "theme": "t2",
                "items": [
                    {
                        "item_type": "attraction",
                        "poi_name": "查无此点咖啡馆",
                        "start_time": "11:00",
                        "end_time": "12:00",
                    },
                ],
            },
        ]
        stream_env(plans)
        events = list(run_generate_trip_stream(_req(2)))
        items = {}
        for ev in events:
            if ev["type"] == "day":
                items.update({it["poiName"]: it for it in ev["plan"]["items"]})
        grounded = items["古埃尔公园"]
        assert grounded["source"] == "nominatim"
        assert grounded["verificationStatus"] == "partially_verified"
        assert grounded["valueKind"] == "observed"
        unlabeled = items["查无此点咖啡馆"]
        assert unlabeled["source"] == "llm.open_day"
        assert unlabeled["verificationStatus"] == "unverified"
        assert unlabeled["valueKind"] == "estimated"

    def test_one_shot_failure_falls_back_to_per_day_without_losing_suggestions(self, stream_env, monkeypatch):
        """截断兜底（PR-4 定案）：整段失败 → 缺口天逐日兜底，天齐、建议不丢。"""
        stream_env(None)

        def boom_trip(req):
            raise RuntimeError("upstream truncated")

        per_day = {
            1: {
                "day_no": 1,
                "note": "兜底 1",
                "items": [{"item_type": "attraction", "poi_name": "古埃尔公园"}],
                "suggestions": [{"poi_name": "米拉之家", "city": "巴塞罗那", "category": "attraction"}],
            },
            2: {
                "day_no": 2,
                "note": "兜底 2",
                "items": [{"item_type": "attraction", "poi_name": "巴塞罗那大教堂"}],
                "suggestions": [],
            },
        }

        def fallback_day(req, used):
            return {**per_day[req.day_no], "items": [dict(it) for it in per_day[req.day_no]["items"]]}

        monkeypatch.setattr(open_plans, "llm_open_trip", boom_trip)
        monkeypatch.setattr(open_plans, "llm_open_day", fallback_day)
        events = list(run_generate_trip_stream(_req(days=2)))
        done = events[-1]
        assert done["daysEmitted"] == [1, 2], "截断后缺口天逐日兜底补齐"
        assert done["complete"] is True
        assert done["message"] is None
        suggestions = next(e for e in events if e["type"] == "suggestions")
        assert "米拉之家" in [s["name"] for s in suggestions["items"]], "兜底天携带的建议随天收集，不丢"

    def test_stream_failure_reports_error_for_repair(self, stream_env, monkeypatch):
        stream_env(None)

        def boom_trip(req):
            raise RuntimeError("upstream down")

        def boom_day(req, used):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(open_plans, "llm_open_trip", boom_trip)
        monkeypatch.setattr(open_plans, "llm_open_day", boom_day)
        events = list(run_generate_trip_stream(_req()))
        done = events[-1]
        assert done["type"] == "done"
        assert done["daysEmitted"] == []
        assert done["complete"] is False
        assert done["message"]

    def test_stream_failure_records_run_status_for_metrics(self, stream_env, monkeypatch):
        """报错的 run 必须留下三态事件，否则 metrics 把它计进 successes。

        `observability.record()` 只从 `run_status` 事件判 degraded/failed。
        """
        stream_env(None)

        def boom_trip(req):
            raise RuntimeError("upstream down")

        def boom_day(req, used):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(open_plans, "llm_open_trip", boom_trip)
        monkeypatch.setattr(open_plans, "llm_open_day", boom_day)
        recorded: list[tuple[str, dict]] = []
        monkeypatch.setattr(
            stream_branch,
            "record_event",
            lambda kind, name, **kw: recorded.append((name, {"status": kw.get("status")})),
        )
        list(run_generate_trip_stream(_req()))
        statuses = [kw["status"] for name, kw in recorded if name == "run_status"]
        assert statuses == ["failed"], f"一天都没出却未记 failed：{statuses}"

    def test_missing_llm_key_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_api_key", "")
        with pytest.raises(ValueError):
            list(run_generate_trip_stream(_req()))


class TestCityFilter:
    def test_mismatched_city_dropped(self):
        rows = [
            {"poi_name": "米拉之家", "city": "巴塞罗那"},
            {"poi_name": "唐吉诃德涩谷", "city": "东京"},
            {"poi_name": "无城市标注", "city": ""},
        ]
        kept = filter_suggestions_by_city(rows, "巴塞罗那")
        assert [r["poi_name"] for r in kept] == ["米拉之家", "无城市标注"]

    def test_containment_allows_bilingual_annotation(self):
        rows = [{"poi_name": "米拉之家", "city": "巴塞罗那（Barcelona）"}]
        assert filter_suggestions_by_city(rows, "Barcelona")

    def test_pure_foreign_city_name_not_falsely_dropped(self):
        """BIZ-3：纯外文城市名 vs 中文目的地——norm 无翻译不可比，宁可保留。
        代价是跨体系的外城建议（Paris vs 巴塞罗那）也一并保留：备选池质量让位
        于不误杀真建议，行程正确性另有终检兜底。"""
        rows = [
            {"poi_name": "米拉之家", "city": "Barcelona"},
            {"poi_name": "卢浮宫", "city": "Paris"},
            {"poi_name": "唐吉诃德涩谷", "city": "东京"},
        ]
        kept = filter_suggestions_by_city(rows, "巴塞罗那")
        assert [r["poi_name"] for r in kept] == ["米拉之家", "卢浮宫"], "跨体系不可判全保留；同体系（东京）照判照拦"

    def test_build_suggestions_filters_by_dest_city_single_source(self):
        """BIZ-3：过滤下沉 build_suggestions——dest_city 给定时模型建议按城市过滤，
        trip/day 链与流式链共用同一真源。"""
        from app.agent.generation.content.suggestions import build_suggestions

        raw = [
            {"poi_name": "圣家堂", "city": "巴塞罗那", "category": "attraction"},
            {"poi_name": "埃菲尔铁塔", "city": "巴黎", "category": "attraction"},
        ]
        rows = build_suggestions([], [], [], [], raw, allow_external=True, dest_city="巴塞罗那")
        names = [r["name"] for r in rows]
        assert "圣家堂" in names and "埃菲尔铁塔" not in names
        # 不给 dest_city 时不过滤（兼容显式跳过校验的调用面）
        rows_nofilter = build_suggestions([], [], [], [], raw, allow_external=True)
        assert [r["name"] for r in rows_nofilter] == ["圣家堂", "埃菲尔铁塔"]
