"""M5 主题化评测单测：evaluate_narrative 指标边界 + themed_cases.json 结构 + intent 透传。

叙事指标的目标口径（契约 v1.1.narrative）：
- 叙事句 theme（非空、不含 "→"）才计入 theme_sentence_rate；
- why_this/practical_notes 缺失按 0 计；
- 意图命中类指标（theme_hit_rate/poi_relevance）只对带 intent 的 case 有值，
  无 intent 时为 None（基线 case 不该被惩罚）。
"""

import json
from pathlib import Path
from typing import Literal

from app.prompts.open_generation import OPEN_DAY_PROMPT_VERSION, OPEN_TRIP_PROMPT_VERSION
from app.schemas.trip import DailyPlan, GenerateResponse, TripItem
from tests.agent_eval.eval_agent import build_generate_request, run_case
from tests.agent_eval.llm_eval import build_generate_request as build_llm_request
from tests.agent_eval.metrics import evaluate_narrative

THEMED_CASES_PATH = Path(__file__).with_name("themed_cases.json")


def _item(
    poi_name: str,
    *,
    item_type: str = "attraction",
    why_this: str | None = None,
    remark: str | None = None,
    lat: float | None = 30.1,
    lon: float | None = 120.2,
    verification: Literal["verified", "partially_verified", "unverified"] = "verified",
) -> TripItem:
    return TripItem(
        item_type=item_type,
        poi_name=poi_name,
        start_time="09:00",
        end_time="10:00",
        why_this=why_this,
        remark=remark,
        latitude=lat,
        longitude=lon,
        verification_status=verification,
    )


def _response(plans: list[DailyPlan], trip_theme: str | None = None) -> GenerateResponse:
    return GenerateResponse(
        city="京都", days=len(plans), title="测试行程", trip_theme=trip_theme, daily_plans=plans, budget_estimate={}
    )


def test_full_narrative_scores_one():
    """叙事字段全齐备 + 语料覆盖全部意图关键词 → 各率 = 1、待复核 = 0。"""
    plan = DailyPlan(
        day_no=1,
        items=[
            _item("清水寺", why_this="《千恋万花》巡礼的京都第一站", remark="住柏悦，步行可达"),
            _item("二年坂", why_this="京都老街，黄昏光线最出片"),
        ],
        theme="节奏松弛地循着作品足迹漫步东山",
        practical_notes=["穿好走的鞋"],
    )
    response = _response([plan], trip_theme="《千恋万花》京都圣地巡礼")
    metrics = evaluate_narrative(
        response,
        {
            "city": "京都",
            "intent": "京都 3 日《千恋万花》圣地巡礼，住柏悦，节奏松弛",
        },
    )
    assert metrics["theme_sentence_rate"] == 1.0
    assert metrics["why_coverage"] == 1.0
    assert metrics["practical_notes_rate"] == 1.0
    assert metrics["coord_available_rate"] == 1.0
    assert metrics["pending_review_count"] == 0
    # 语料（trip_theme+theme+why_this+remark）覆盖全部意图关键词
    assert metrics["theme_hit_rate"] == 1.0
    # 每个 attraction 的 why_this 都含意图关键词「京都」
    assert metrics["poi_relevance"] == 1.0


def test_path_string_and_missing_fields_do_not_count():
    """「A→B→C」路径串与空/None theme 不计入叙事句；缺失字段按 0 计。"""
    plans = [
        DailyPlan(
            day_no=1,
            items=[_item("清水寺", why_this="巡礼起点")],
            theme="清水寺→伏见稻荷→二年坂",
            practical_notes=["带伞"],
        ),
        DailyPlan(day_no=2, items=[_item("岚山", why_this=None, lat=0.0, lon=None)], theme=None, practical_notes=[]),
    ]
    metrics = evaluate_narrative(_response(plans), {"city": "京都"})
    assert metrics["theme_sentence_rate"] == 0.0
    assert metrics["why_coverage"] == 0.5
    assert metrics["practical_notes_rate"] == 0.5
    # 岚山 lat=0/lon=None：坐标哨兵不算有效
    assert metrics["coord_available_rate"] == 0.5
    # 无 intent：主题命中类指标为 None 而不是 0
    assert metrics["theme_hit_rate"] is None
    assert metrics["poi_relevance"] is None
    # 两个 attraction 均为默认 verified → 待复核为 0
    assert metrics["pending_review_count"] == 0


def test_remark_joins_intent_corpus_and_pending_review_counts():
    """remark 参与意图命中语料；unverified item 计入待复核分子。"""
    plan = DailyPlan(
        day_no=1,
        items=[
            _item("锦市场", why_this=None, remark="京都本地人的厨房", verification="unverified"),
            _item("鸭川", why_this=None, remark=None, verification="partially_verified"),
        ],
        theme="沿鸭川慢走的一日",
    )
    # intent 含空格分隔，确保「京都」独立成词
    metrics = evaluate_narrative(_response([plan]), {"city": "京都", "intent": "京都 本地生活"})
    # 锦市场 why_this 为空但 remark 命中「京都」→ 仍算主题相关点
    assert metrics["poi_relevance"] == 0.5
    assert metrics["pending_review_count"] == 1
    # 语料含「京都」（remark）但不含「本地生活」→ 命中一半
    assert metrics["theme_hit_rate"] == 0.5


def test_themed_cases_structure():
    """三套同题 = 每城一对（无主题基线 + 主题版）：同城同参数，只差 intent。"""
    cases = json.loads(THEMED_CASES_PATH.read_text(encoding="utf-8"))
    assert len(cases) == 6
    expected = [("京都", "kyoto"), ("杭州", "hangzhou"), ("成都", "chengdu")]
    for (city, prefix), index in zip(expected, range(0, 6, 2), strict=False):
        baseline, themed = cases[index], cases[index + 1]
        assert baseline["city"] == city and themed["city"] == city
        assert baseline["name"] == f"{prefix}-baseline"
        assert themed["name"] == f"{prefix}-themed"
        # 同城同参数：days/persons/budget/preferences 完全一致
        for key in ("days", "persons", "budget", "preferences"):
            assert baseline.get(key) == themed.get(key)
        # 基线无 intent，主题版带 intent；prompt_version 占位待运行时填充
        assert not baseline.get("intent")
        assert themed.get("intent")
        assert baseline["prompt_version"] == "" and themed["prompt_version"] == ""


def test_case_to_request_filters_meta_and_passes_intent():
    """case → 请求：name/prompt_version 被过滤，intent 透传（eval_agent mock 路径）。"""
    themed = {
        "name": "kyoto-themed",
        "prompt_version": "v1.1.narrative",
        "city": "京都",
        "days": 3,
        "persons": 2,
        "budget": 50000,
        "preferences": ["二次元", "文化"],
        "intent": "京都 3 日《千恋万花》圣地巡礼，住柏悦，节奏松弛",
    }
    request = build_generate_request(themed)
    assert request.intent == themed["intent"]
    dumped = request.model_dump()
    assert "name" not in dumped and "prompt_version" not in dumped
    # 基线（无 intent）也构造成功，intent 保持 None（llm_eval 真跑路径同实现）
    baseline = {
        "name": "kyoto-baseline",
        "prompt_version": "",
        "city": "京都",
        "days": 3,
        "persons": 2,
        "budget": 50000,
        "preferences": ["二次元", "文化"],
    }
    assert build_llm_request(baseline).intent is None


def test_run_case_accepts_themed_case():
    """带 name/prompt_version/intent 的主题 case 可走完整 mock 评测链路。"""
    result = run_case(
        {
            "name": "kyoto-themed",
            "prompt_version": "",
            "city": "京都",
            "days": 2,
            "persons": 2,
            "budget": 50000,
            "preferences": ["二次元", "文化"],
            "intent": "京都 2 日《千恋万花》圣地巡礼",
        }
    )
    # prompt_version 占位在运行时填充实际契约版本
    assert result["case"]["prompt_version"] == f"{OPEN_DAY_PROMPT_VERSION}/{OPEN_TRIP_PROMPT_VERSION}"
    assert result["fallback_success"] is True
