"""format_output 输出口径的回归基线（characterization test）。

用手工构造的 UnifiedAgentState 直接驱动 format_output，锁住定价（联网实时价×季节系数、
人均价钳制）、权威事实回填与溯源标注、预算重算、质量结论这些没有其它覆盖的路径。
基线快照在 tests/golden/format_output.json；有意改口径时用
GOLDEN_REGENERATE=1 重写并复核 git diff。
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import pathlib

import pytest

from app.agent.data import pricing
from app.agent.generation.orchestration.workflow import format_output
from app.agent.grounding.grounding_evidence import issue_evidence
from app.agent.research.agent_state import UnifiedAgentState
from app.agent.tools import registry as tool_registry
from app.schemas.trip import GenerateRequest

# ---------------------------------------------------------------- 外部依赖打桩

LIVE_HOTEL_PRICES = {"杭州舒适酒店1": 420.0, "丽江悦榕庄": 1880.0}
LIVE_FOOD_PRICES = {"杭州本地餐厅1": 96.0}

ACTIVITY_ROWS = [
    {
        "id": "A1",
        "name": "西湖夜游",
        "category": "activity",
        "price": 120,
        "latitude": 30.24,
        "longitude": 120.15,
        "source": "web.search",
    }
]


def _query_live_price(city, name, _date=None):
    price = LIVE_HOTEL_PRICES.get(name)
    return None if price is None else {"price": price, "note": f"{city}·{name} 挂牌价"}


def _query_live_food_price(_city, name):
    price = LIVE_FOOD_PRICES.get(name)
    return None if price is None else {"price": price, "note": f"{name} 人均"}


def _fake_route_invoke(_name, params):
    items = params["items"]
    matrix = {}
    for i in range(len(items) - 1):
        a, b = items[i], items[i + 1]
        matrix[f"{a.get('poi_name')}->{b.get('poi_name')}"] = {
            "duration_min": 18 + i * 7,
            "distance_km": 2.5 + i,
            "source": "amap",
            "mode": "driving",
        }
    return matrix


def _fake_web_search_json(prompt, *_args, **_kwargs):
    """建议池补洞的确定性替身：真实实现会请求 LLM 联网检索。"""
    return {
        "items": [{"name": f"网搜地点{i}", "intro": "联网补池替身", "estimated_cost": 60 + 10 * i} for i in range(4)]
    }


@pytest.fixture()
def stubbed(monkeypatch):
    """打桩 DB / 外部 HTTP / 路线服务；同时在两个可能持有别名的模块上打，
    使本工具在 format_output 重构前后都能命中真正的调用点。"""
    import app.agent.data.web_search as web_search_mod
    import app.agent.generation.orchestration.workflow as wf

    monkeypatch.setattr(pricing, "query_live_price", _query_live_price)
    monkeypatch.setattr(pricing, "query_live_food_price", _query_live_food_price)
    # 实时价的绑定位置随重构迁移（workflow → formatting.prices）：凡当前持有
    # 该名字的宿主模块都就地替换，保证拆分前后命中同一调用点。
    holder_paths = [
        "app.agent.generation.orchestration.workflow",
        "app.agent.generation.output.prices",
        "app.agent.generation.output.costing",
    ]
    for path in holder_paths:
        try:
            holder = importlib.import_module(path)
        except ModuleNotFoundError:
            continue
        for name in ("query_live_price", "query_live_food_price"):
            if hasattr(holder, name):
                monkeypatch.setattr(holder, name, getattr(pricing, name))
    monkeypatch.setattr(web_search_mod, "web_search_json", _fake_web_search_json)

    # 体验类地板改走联网补池（POI 库退役）：在 web_search 缝位打桩保金样确定；
    # 非 activity 品类沿用 _fake_web_search_json 的替身语义（与真实映射一致）。
    def _fake_search_places_via_web(city, category, limit=4, **_kwargs):
        if category == "activity":
            return copy.deepcopy(ACTIVITY_ROWS)
        data = _fake_web_search_json(f"{city}{category}", schema_hint="")
        rows = (data.get("items") or []) if isinstance(data, dict) else []
        out = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            cost = row.get("estimated_cost")
            out.append(
                {
                    "name": name,
                    "category": category,
                    "intro": str(row.get("intro") or "").strip()[:80] or None,
                    "estimated_cost": float(cost) if isinstance(cost, (int, float)) and cost > 0 else None,
                    "source": "web.search",
                }
            )
        return out

    monkeypatch.setattr(web_search_mod, "search_places_via_web", _fake_search_places_via_web)
    monkeypatch.setattr(tool_registry.registry, "invoke", _fake_route_invoke)
    # 快照不能依赖本机 .env：显式固定所有影响输出的开关与预算
    for attr, value in {
        "llm_api_key": "golden-stub-key",
        "live_price_search": False,
        "live_food_price_search": False,
        "max_live_queries": 3,
        "max_live_food_queries": 2,
        "route_service_enabled": True,
        "route_mode": "driving",
        "budget_hard_constraint": True,
        "budget_overage_ratio": 0.15,
        "meal_price_hard_cap_ratio": 4.0,
        "meal_price_soft_cap_ratio": 2.0,
        "web_search_enabled": True,
    }.items():
        monkeypatch.setattr(wf.settings, attr, value)
    return monkeypatch


# ---------------------------------------------------------------- 素材


def _poi(name, lat, lon, *, ticket=None, open_time=None, source="opentripmap", fetched="2026-08-01T00:00:00Z"):
    row = {
        "id": f"poi-{name}",
        "name": name,
        "latitude": lat,
        "longitude": lon,
        "source": source,
        "source_updated_at": fetched,
    }
    if ticket is not None:
        row["ticket_price"] = ticket
    if open_time is not None:
        row["open_time"] = open_time
    # fixture 冒充的是"外部数据层查到的一行"，所以要连证据票一起签
    # （生产在 tools._as_candidate 当场签）。不签票的行会被当成调用方伪造。
    issue_evidence(row)
    return row


def _item(item_type, name, *, start, end, cost=None, lat=None, lon=None, duration=None, remark=None):
    return {
        "item_type": item_type,
        "poi_name": name,
        "start_time": start,
        "end_time": end,
        "duration_min": duration,
        "cost": cost,
        "latitude": lat,
        "longitude": lon,
        "remark": remark,
        "poi_id": None,
    }


def _state(req, *, plans, live_price=False, live_food=False, **overrides) -> tuple[UnifiedAgentState, bool, bool]:
    base = {
        "request": req,
        "daily_plans": plans,
        "budget_estimate": {"门票": 100.0, "餐饮": 200.0, "交通": 60.0, "酒店": 900.0},
        "candidates": [],
        "foods": [],
        "hotels": [],
        "consumption": {"meal_price": 65.0, "transport_price": 30.0},
        "schedule_report": {},
        "validation_log": [],
        "raw_suggestions": [],
        "fix_count": 0,
        "validation_issues": [],
    }
    base.update(copy.deepcopy(overrides))
    # PR-2：节点只收 UnifiedAgentState（属性访问），state 构造即模型构造
    return UnifiedAgentState(**base), live_price, live_food


# ---------------------------------------------------------------- 场景定义


def all_scenarios():
    """返回 [(名字, state, live_price, live_food)]。"""
    # 1 人杭州基础场景；start_date 是场景变量，显式展开构造（dict 双展开会丢字段级类型）。
    # req_lj 不带日期 → season_factor 按当天算，国庆/春节/暑期会把 golden 打红
    # （2026-09-30 实录）——钉死平季日期，保证场景确定性。
    req_aut = GenerateRequest(city="杭州", days=1, persons=2, budget=3000, start_date="2026-10-01")
    req_win = GenerateRequest(city="杭州", days=1, persons=2, budget=3000, start_date="2026-12-20")
    req_multi = GenerateRequest(city="杭州", days=3, persons=3, budget=5000, start_date="2026-10-01")
    req_lj = GenerateRequest(city="丽江", days=1, persons=2, budget=3000, start_date="2026-04-15")

    s: list[tuple[str, tuple[UnifiedAgentState, bool, bool]]] = []

    # 1 权威命中：坐标/票价/营业时间回填 + partially_verified/fresh
    s.append(
        (
            "01_authoritative_hit",
            _state(
                req_aut,
                plans=[
                    {
                        "day_no": 1,
                        "note": "西湖线",
                        "theme": "湖景",
                        "items": [
                            _item("attraction", "西湖", start="09:00", end="12:00", lat=30.0, lon=120.0),
                            _item("food", "楼外楼", start="12:40", end="13:40", cost=210, lat=30.1, lon=120.1),
                        ],
                    }
                ],
                candidates=[
                    _poi("西湖", 30.244, 120.145, ticket=0, open_time="00:00-24:00"),
                    _poi("楼外楼", 30.253, 120.141, ticket=180),
                ],
                foods=[_poi("楼外楼", 30.253, 120.141, ticket=180)],
            ),
        )
    )

    # 2 权威行坐标为 0/0 哨兵 → 不背书
    s.append(
        (
            "02_authoritative_missing_coords",
            _state(
                req_aut,
                plans=[
                    {
                        "day_no": 1,
                        "note": "山线",
                        "items": [
                            _item(
                                "attraction",
                                "灵隐寺",
                                start="09:00",
                                end="12:00",
                                cost=10,
                                lat=30.2,
                                lon=120.1,
                                duration=180,
                            ),
                            _item(
                                "food", "素斋", start="12:40", end="13:40", cost=60, lat=30.21, lon=120.11, duration=60
                            ),
                            _item(
                                "attraction",
                                "永福寺",
                                start="14:20",
                                end="16:20",
                                cost=0,
                                lat=30.22,
                                lon=120.12,
                                duration=120,
                            ),
                        ],
                    }
                ],
                candidates=[_poi("灵隐寺", 0.0, 0.0, ticket=45)],
            ),
        )
    )

    # 3 开放模式：池外条目 + 自带坐标 → amap-grounding
    s.append(
        (
            "03_open_mode_llm",
            _state(
                req_aut,
                plans=[
                    {
                        "day_no": 1,
                        "note": "自由探索",
                        "trip_theme": "慢游杭州",
                        "items": [
                            _item(
                                "attraction",
                                "小众码头",
                                start="09:00",
                                end="12:00",
                                cost=55,
                                lat=30.3,
                                lon=120.2,
                                duration=180,
                                remark="需提前电话确认",
                            ),
                            _item(
                                "food",
                                "社区面馆",
                                start="12:40",
                                end="13:40",
                                cost=45,
                                lat=30.31,
                                lon=120.21,
                                duration=60,
                            ),
                            _item("attraction", "旧仓库", start="14:10", end="16:10", cost=0, duration=120),
                        ],
                    }
                ],
                schedule_report={"open_research": True, "destination_status": "researched"},
            ),
        )
    )

    # 4 酒店联网实时价 × 冬季系数
    s.append(
        (
            "04_hotel_live_price",
            _state(
                req_win,
                live_price=True,
                plans=[
                    {
                        "day_no": 1,
                        "note": "含酒店",
                        "items": [
                            _item(
                                "attraction",
                                "西溪湿地",
                                start="09:00",
                                end="12:00",
                                cost=70,
                                lat=30.27,
                                lon=120.07,
                                duration=180,
                            ),
                            _item(
                                "food",
                                "农家菜",
                                start="12:40",
                                end="13:40",
                                cost=80,
                                lat=30.2705,
                                lon=120.0705,
                                duration=60,
                            ),
                            _item(
                                "attraction",
                                "福堤",
                                start="14:20",
                                end="16:20",
                                cost=0,
                                lat=30.271,
                                lon=120.071,
                                duration=120,
                            ),
                            _item(
                                "hotel",
                                "杭州舒适酒店1",
                                start="18:00",
                                end="18:30",
                                cost=350,
                                lat=30.26,
                                lon=120.16,
                                duration=30,
                            ),
                        ],
                    }
                ],
                hotels=[_poi("杭州舒适酒店1", 30.26, 120.16)],
            ),
        )
    )

    # 5 酒店无实时价 → 基准价 × 系数估算
    s.append(
        (
            "05_hotel_season_estimate",
            _state(
                req_win,
                live_price=False,
                plans=[
                    {
                        "day_no": 1,
                        "note": "无实时价",
                        "items": [
                            _item(
                                "attraction",
                                "玉龙雪山",
                                start="09:00",
                                end="14:00",
                                cost=100,
                                lat=27.1,
                                lon=100.18,
                                duration=300,
                            ),
                            _item(
                                "food",
                                "云雪丽",
                                start="14:40",
                                end="15:40",
                                cost=80,
                                lat=26.87,
                                lon=100.23,
                                duration=60,
                            ),
                            _item(
                                "hotel",
                                "丽江悦榕庄",
                                start="21:00",
                                end="08:00",
                                cost=2500,
                                lat=26.9,
                                lon=100.2,
                                duration=660,
                            ),
                        ],
                    }
                ],
                candidates=[_poi("丽江悦榕庄", 26.9, 100.2)],
            ),
        )
    )

    # 6 餐饮实时价 + 人均价钳制
    s.append(
        (
            "06_food_live_and_clamp",
            _state(
                req_aut,
                live_food=True,
                plans=[
                    {
                        "day_no": 1,
                        "note": "餐线",
                        "items": [
                            _item(
                                "attraction",
                                "河坊街",
                                start="09:00",
                                end="11:00",
                                cost=0,
                                lat=30.24,
                                lon=120.17,
                                duration=120,
                            ),
                            _item(
                                "food",
                                "杭州本地餐厅1",
                                start="11:40",
                                end="13:10",
                                cost=900,
                                lat=30.25,
                                lon=120.17,
                                duration=90,
                            ),
                            _item(
                                "food",
                                "无名小馆",
                                start="13:40",
                                end="14:40",
                                cost=None,
                                lat=30.2495,
                                lon=120.1695,
                                duration=60,
                            ),
                            _item(
                                "attraction",
                                "南宋官窑",
                                start="15:10",
                                end="17:10",
                                cost=20,
                                lat=30.249,
                                lon=120.169,
                                duration=120,
                            ),
                        ],
                    }
                ],
                consumption={"meal_price": 40.0, "transport_price": 25.0},
            ),
        )
    )

    # 7 多日：跨夜计数 + 部分日有餐价/部分日回落人均
    s.append(
        (
            "07_multi_day_budget",
            _state(
                req_multi,
                live_price=True,
                plans=[
                    {
                        "day_no": 1,
                        "note": "D1",
                        "items": [
                            _item("attraction", "西湖", start="09:00", end="12:00", lat=30.2, lon=120.1, duration=180),
                            _item(
                                "food",
                                "楼外楼",
                                start="12:40",
                                end="13:40",
                                cost=180,
                                lat=30.2005,
                                lon=120.1005,
                                duration=60,
                            ),
                            _item(
                                "hotel",
                                "杭州舒适酒店1",
                                start="18:00",
                                end="18:30",
                                cost=420,
                                lat=30.26,
                                lon=120.16,
                                duration=30,
                            ),
                        ],
                    },
                    {
                        "day_no": 2,
                        "note": "D2",
                        "items": [
                            _item(
                                "attraction",
                                "灵隐寺",
                                start="09:00",
                                end="13:00",
                                cost=45,
                                lat=30.24,
                                lon=120.1,
                                duration=240,
                            ),
                            _item(
                                "food",
                                "素斋",
                                start="13:40",
                                end="14:40",
                                cost=55,
                                lat=30.241,
                                lon=120.101,
                                duration=60,
                            ),
                        ],
                    },
                    {
                        "day_no": 3,
                        "note": "D3",
                        "items": [
                            _item(
                                "attraction",
                                "宋城",
                                start="10:00",
                                end="15:00",
                                cost=290,
                                lat=30.19,
                                lon=120.09,
                                duration=300,
                            ),
                            _item(
                                "hotel",
                                "杭州舒适酒店1",
                                start="19:00",
                                end="19:30",
                                cost=420,
                                lat=30.26,
                                lon=120.16,
                                duration=30,
                            ),
                        ],
                    },
                ],
                candidates=[_poi("西湖", 30.244, 120.145, ticket=0)],
                hotels=[_poi("杭州舒适酒店1", 30.26, 120.16)],
            ),
        )
    )

    # 8 扩展类型归一 + 时间窗反推时长 + 空日
    s.append(
        (
            "08_sanitize_and_empty_day",
            _state(
                req_aut,
                plans=[
                    {
                        "day_no": 1,
                        "note": "混合",
                        "items": [
                            _item("souvenir", "丝绸店", start="10:00", end="11:00", cost=0, lat=30.25, lon=120.15),
                            _item(
                                "activity",
                                "龙井采茶",
                                start=None,
                                end=None,
                                cost=120,
                                lat=30.22,
                                lon=120.12,
                                duration=150,
                            ),
                            _item("attraction", "未定", start="bad", end="worse", cost=None),
                        ],
                    },
                    {"day_no": 2, "note": "空日", "items": []},
                ],
            ),
        )
    )

    # 9 重试耗尽保留 LLM 结果（0 景点）→ BLOCKED + 降级理由
    s.append(
        (
            "09_retry_exhausted",
            _state(
                req_lj,
                plans=[
                    {
                        "day_no": 1,
                        "note": "只有酒店",
                        "items": [
                            _item(
                                "hotel",
                                "丽江悦榕庄",
                                start="21:00",
                                end="08:00",
                                cost=2500,
                                lat=26.9,
                                lon=100.2,
                                duration=660,
                            )
                        ],
                    }
                ],
                fix_count=9,
                validation_issues=["行程中没有安排任何景点"],
            ),
        )
    )

    # 10 零条目 → failed + BLOCKED
    s.append(
        (
            "10_no_items",
            _state(
                req_lj,
                plans=[{"day_no": 1, "note": "草案", "items": []}],
                schedule_report={"destination_status": "draft_only"},
            ),
        )
    )

    # 11 建议池 + research 透出 + degraded_reason
    s.append(
        (
            "11_suggestions_research",
            _state(
                req_aut,
                plans=[
                    {
                        "day_no": 1,
                        "note": "带建议",
                        "items": [
                            _item(
                                "attraction", "西湖", start="09:00", end="12:00", lat=30.24, lon=120.14, duration=180
                            ),
                            _item(
                                "food",
                                "知味观",
                                start="12:40",
                                end="13:40",
                                cost=90,
                                lat=30.25,
                                lon=120.15,
                                duration=60,
                            ),
                            _item(
                                "attraction",
                                "柳浪闻莺",
                                start="14:10",
                                end="16:10",
                                cost=0,
                                lat=30.2505,
                                lon=120.1505,
                                duration=120,
                            ),
                        ],
                    }
                ],
                degraded_reason="研究阶段部分证据源超时",
                research_report={"evidence_count": 7, "confidence": 0.6, "gaps": ["营业时间"]},
                raw_suggestions=[
                    {
                        "poi_name": "太子湾公园",
                        "category": "attraction",
                        "reason": "人少",
                        "latitude": 30.23,
                        "longitude": 120.14,
                        "cost": 0,
                    }
                ],
                candidates=[_poi("西湖", 30.244, 120.145, ticket=0)],
            ),
        )
    )
    return s


# ---------------------------------------------------------------- 采集 / 比对


def build_snapshot(stubbed) -> dict:
    import app.agent.generation.orchestration.workflow as wf

    snapshot = {}
    for name, (state, live_price, live_food) in all_scenarios():
        stubbed.setattr(wf.settings, "live_price_search", live_price)
        stubbed.setattr(wf.settings, "live_food_price_search", live_food)
        payload = format_output(state)["result"].model_dump(mode="json")
        qr = payload.get("quality_report") or {}
        if "validated_at" in qr:
            qr["validated_at"] = "<normalized>"
        snapshot[name] = payload
    return snapshot


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


_GOLDEN = pathlib.Path(__file__).parent / "golden" / "format_output.json"


def test_format_output_matches_golden_snapshot(stubbed):
    """format_output 的输出与基线快照逐字段一致。

    拆出 app/agent/formatting 三个阶段模块时以此护栏验证等价性；定价、溯源标注、
    预算重算这些没有其它覆盖的路径也一并锁住。确有意图改变输出口径时，用
    `GOLDEN_REGENERATE=1 pytest tests/test_format_output_golden.py` 重写基线并复核 diff。
    """
    actual = build_snapshot(stubbed)
    assert len(actual) == 11, f"场景数异常：{sorted(actual)}"
    if os.environ.get("GOLDEN_REGENERATE") == "1":
        with _GOLDEN.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(actual, fh, ensure_ascii=False, indent=2, sort_keys=True)
        pytest.skip(f"基线已重写：{_GOLDEN}（请 git diff 复核后取消本 skip）")
    with _GOLDEN.open(encoding="utf-8") as fh:
        expected = json.load(fh)
    diffs = []
    for key in sorted(set(expected) | set(actual)):
        before, after = dict(_flatten(expected.get(key, {}))), dict(_flatten(actual.get(key, {})))
        for path in sorted(set(before) | set(after)):
            if before.get(path) != after.get(path):
                diffs.append(f"{key}{path}: {before.get(path)!r} -> {after.get(path)!r}")
    assert not diffs, f"format_output 输出与基线不一致（{len(diffs)} 处）：\n" + "\n".join(diffs[:40])
