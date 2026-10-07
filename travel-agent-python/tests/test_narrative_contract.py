"""M3-① 契约叙事化（AD5）测试：schema / 清洗 / prompt / 装配透传。

对应改动：
- app/schemas/trip.py：PhotoSpot/BackupRule/DayOption 子模型 + why_this/
  day_options/trip_theme 叙事字段（超长截断不抛错）；
- app/prompts/open_generation.py：v1.1.narrative 契约升级；
- app/agent/generation/orchestration/day_stream.py：sanitize_narrative 轻量清洗 + max_tokens 上调；
- app/agent/generation/orchestration/workflow.py：叙事层装配透传。
"""

import json

from app.agent.generation.content import day_prompts, narrative
from app.agent.generation.content.reflect import (
    MAX_DAILY_ATTRACTIONS,
    MAX_DAILY_MINUTES,
    MIN_ACTIVE_MINUTES,
)
from app.agent.generation.orchestration import day_stream
from app.prompts import open_generation
from app.schemas.trip import (
    BackupRule,
    DailyPlan,
    DayOption,
    GenerateDayRequest,
    GenerateResponse,
    PhotoSpot,
    TripItem,
)

# ---------- 1. Schema：结构化子模型与叙事字段 ----------


def test_backup_rule_accepts_legacy_dict_shapes():
    """旧 dict 前向兼容：wire 键 if / python 键 if_ / 缺 key 都不炸。"""
    from_wire = BackupRule.model_validate({"if": "下雨", "action": "改室内"})
    assert from_wire.if_ == "下雨"
    assert from_wire.action == "改室内"

    from_python = BackupRule.model_validate({"if_": "闭馆", "action": "换景点"})
    assert from_python.if_ == "闭馆"

    legacy = BackupRule.model_validate({"name": "狮子林"})  # 旧残缺 dict
    assert legacy.if_ == ""
    assert legacy.action == ""

    empty = BackupRule.model_validate({})
    assert empty.if_ == "" and empty.action == ""


def test_backup_rule_serializes_if_alias_on_wire():
    rule = BackupRule(if_="雨天", action="改室内")
    assert rule.model_dump(mode="json", by_alias=True) == {"if": "雨天", "action": "改室内"}


def test_narrative_fields_serialize_camel_case_on_wire():
    plan = DailyPlan(
        day_no=1,
        theme="街区巡礼",
        trip_theme="京都·千恋万花圣地巡礼",
        items=[TripItem(poi_name="清水寺", why_this="巡礼核心点")],
        photo_spots=[PhotoSpot(name="清水寺舞台", tip="仰拍", best_time="清晨")],
        backup_plan=[BackupRule(if_="雨天", action="改室内")],
        day_options=[DayOption(label="暴走版", summary="八点连轴", tradeoff="体力消耗大")],
    )
    dump = plan.model_dump(mode="json", by_alias=True)
    assert dump["tripTheme"] == "京都·千恋万花圣地巡礼"
    assert dump["items"][0]["whyThis"] == "巡礼核心点"
    assert dump["photoSpots"][0] == {"name": "清水寺舞台", "tip": "仰拍", "bestTime": "清晨"}
    assert dump["backupPlan"][0] == {"if": "雨天", "action": "改室内"}
    assert dump["dayOptions"][0]["label"] == "暴走版"


def test_oversized_narrative_input_is_clipped_not_rejected():
    """超长不炸：由校验层截断，保证旧数据/模型超限不毁整份行程。"""
    plan = DailyPlan(
        day_no=1,
        theme="长" * 50,
        trip_theme="题" * 60,
        items=[TripItem(poi_name="清水寺", why_this="长" * 200)],
    )
    assert plan.theme == "长" * 40
    assert plan.trip_theme == "题" * 40
    assert plan.items[0].why_this == "长" * 120

    spot = PhotoSpot(name="长" * 100, tip="长" * 150, best_time="长" * 45)
    assert spot.name == "长" * 60 and spot.tip == "长" * 120 and spot.best_time == "长" * 40

    option = DayOption(label="长" * 45, summary="长" * 130, tradeoff="长" * 130)
    assert option.label == "长" * 40 and option.summary == "长" * 120

    response = GenerateResponse(city="京都", days=1, title="t", daily_plans=[], trip_theme="题" * 60)
    assert response.trip_theme == "题" * 40


# ---------- 2. 清洗函数：narrative.sanitize_narrative ----------


def test_sanitize_narrative_clips_lengths_and_counts():
    plan = narrative.sanitize_narrative(
        {
            "theme": "长" * 50,
            "trip_theme": "题" * 60,
            "practical_notes": [f"提示{i}" for i in range(6)],
            "photo_spots": [{"name": f"点{i}"} for i in range(6)],
            "backup_plan": [{"if": f"条件{i}", "action": f"动作{i}"} for i in range(5)],
            "day_options": [{"label": f"方案{i}", "summary": "s", "tradeoff": "t"} for i in range(4)],
            "items": [{"item_type": "attraction", "poi_name": "清水寺", "why_this": "长" * 200}],
        }
    )
    assert plan["theme"] == "长" * 40
    assert plan["trip_theme"] == "题" * 40
    assert len(plan["practical_notes"]) == 4
    assert len(plan["photo_spots"]) == 4
    assert len(plan["backup_plan"]) == 3
    assert len(plan["day_options"]) == 2
    assert plan["items"][0]["why_this"] == "长" * 120  # 非 attraction 同样保留（此为 attraction）


def test_sanitize_narrative_defaults_missing_keys_to_empty():
    plan = narrative.sanitize_narrative({"note": "只有概览"})
    assert plan["theme"] is None
    assert plan["trip_theme"] is None
    assert plan["practical_notes"] == []
    assert plan["photo_spots"] == []
    assert plan["backup_plan"] == []
    assert plan["day_options"] == []


def test_sanitize_narrative_tolerates_invalid_types():
    plan = narrative.sanitize_narrative(
        {
            "theme": 123,
            "trip_theme": 456,
            "practical_notes": "不是列表",
            "photo_spots": "不是列表",
            "backup_plan": "不是列表",
            "day_options": "不是列表",
            "items": [{"item_type": "food", "poi_name": "一兰拉面", "why_this": {"bad": 1}}],
        }
    )
    assert plan["theme"] is None
    assert plan["trip_theme"] == ""
    assert plan["practical_notes"] == []
    assert plan["photo_spots"] == []
    assert plan["backup_plan"] == []
    assert plan["day_options"] == []
    assert plan["items"][0]["why_this"] == ""  # 非法类型降级为空，不抛错


def test_sanitize_narrative_coerces_string_photo_spot_and_numeric_notes():
    plan = narrative.sanitize_narrative(
        {
            "practical_notes": ["穿运动鞋", 42, {"bad": "dict"}],
            "photo_spots": ["清水寺舞台", {"name": "伏见稻荷"}],
        }
    )
    assert plan["practical_notes"] == ["穿运动鞋", "42"]  # 数值标量转字符串，dict 丢弃
    assert plan["photo_spots"] == [{"name": "清水寺舞台"}, {"name": "伏见稻荷"}]


def test_sanitize_narrative_normalizes_camel_case_keys():
    plan = narrative.sanitize_narrative(
        {
            "tripTheme": "京都巡礼",
            "photoSpots": [{"name": "清水寺"}],
            "backupPlan": [{"if": "雨"}],
            "dayOptions": [{"label": "休闲版", "summary": "三点慢逛", "tradeoff": "少看两景"}],
            "practicalNotes": ["n1", "n2"],
            "items": [{"poi_name": "x", "whyThis": "长" * 130}],
        }
    )
    assert plan["trip_theme"] == "京都巡礼" and "tripTheme" not in plan
    assert plan["photo_spots"] == [{"name": "清水寺"}]
    assert plan["backup_plan"] == [{"if": "雨"}]
    assert plan["day_options"][0]["label"] == "休闲版"
    assert plan["practical_notes"] == ["n1", "n2"]
    assert plan["items"][0]["why_this"] == "长" * 120


def test_sanitize_narrative_keeps_why_this_of_non_attraction_items():
    plan = narrative.sanitize_narrative(
        {
            "items": [
                {"item_type": "food", "poi_name": "一兰拉面", "why_this": "汤头口碑第一"},
                {"item_type": "attraction", "poi_name": "清水寺", "why_this": None},
            ]
        }
    )
    assert plan["items"][0]["why_this"] == "汤头口碑第一"  # 非 attraction 只截不删
    assert plan["items"][1]["why_this"] is None


# ---------- 3. Prompt：语言一致性契约 ----------


def test_prompt_versions_current():
    # 2026-10-07（单日饱和度窗口入 prompt）：终检的 240-480 分钟窗口与景点上限
    # 写进两套 Prompt，两版本同步 bump
    assert open_generation.OPEN_DAY_PROMPT_VERSION == "v1.5.localized"
    assert open_generation.OPEN_TRIP_PROMPT_VERSION == "v1.4.localized"


class _Mem:
    """WorkingMemory 鸭子类型替身：仅需 as_sorted_list()。"""

    def as_sorted_list(self):
        return []


def _day_prompt(day_no: int) -> str:
    return open_generation.open_day_system_prompt(
        day_no=day_no,
        pace="",
        hotel_clause="",
        hotel_hint="",
        mem=_Mem(),
        min_active_minutes=MIN_ACTIVE_MINUTES,
        max_daily_minutes=MAX_DAILY_MINUTES,
        max_daily_attractions=MAX_DAILY_ATTRACTIONS,
    )


def test_day_prompt_compact_mode_shrinks_output_budget():
    """降级档（json_object）压缩 suggestions 税：24-40 条×60-100 字在 8000 token 下
    高频截断（2026-10-05 探针 3/3 截断在 suggestions 段），截断 → 修复重试产出缺
    items 的短对象 = P2 空天内层根因。守约网关（默认路径）口径一字不变。"""
    full = _day_prompt(2)
    compact = open_generation.open_day_system_prompt(
        day_no=2,
        pace="",
        hotel_clause="",
        hotel_hint="",
        mem=_Mem(),
        min_active_minutes=MIN_ACTIVE_MINUTES,
        max_daily_minutes=MAX_DAILY_MINUTES,
        max_daily_attractions=MAX_DAILY_ATTRACTIONS,
        compact_output=True,
    )
    assert "24-40" in full and "10-14" in compact and "24-40" not in compact
    assert "禁止返回空 items" in compact and "禁止返回空 items" not in full
    assert "60-100字" in full and "60-100字" not in compact
    assert "两三句亮点" in full and "一句话亮点" in compact


def test_open_day_contract_contains_narrative_fields():
    prompt = _day_prompt(1)
    assert '"trip_theme":' in prompt  # 第 1 天顶层输出整趟主题
    assert "why_this" in prompt
    assert "叙事句" in prompt
    assert "2-4 条" in prompt
    assert "120 字" in prompt
    assert "防啰嗦" in prompt
    assert "photo_spots" in prompt and "backup_plan" in prompt and "day_options" in prompt


def test_open_day_contract_omits_trip_theme_after_day_one():
    first = _day_prompt(1)
    second = _day_prompt(2)
    # 契约串里少一个键，模型就不会在第 2 天输出 trip_theme
    assert '"trip_theme":' in first
    assert '"trip_theme":' not in second
    assert "仅第 1 天输出" in second


def test_prompts_state_the_terminal_check_window():
    """两套 Prompt 都必须告知终检的单日时长窗口（单日档还要给景点上限与餐次规则）。

    2026-10-07 实测：不告知时 68 天里 20 天因「过满 11 + 过稀 9」被终检重置——
    Prompt 说「密度你看着办」、门禁按硬窗口判，模型无从命中。数字取自同一常量
    （reflect），所以改窗口即改正文，指纹与版本随之变化（刻意如此）。
    """
    trip = open_generation.open_trip_system_prompt(
        days=2,
        hotel_clause="",
        min_active_minutes=MIN_ACTIVE_MINUTES,
        max_daily_minutes=MAX_DAILY_MINUTES,
    )
    day = _day_prompt(1)
    for prompt in (trip, day):
        assert f"{MIN_ACTIVE_MINUTES}-{MAX_DAILY_MINUTES} 分钟" in prompt, "未告知单日时长窗口"
    assert f"最多 {MAX_DAILY_ATTRACTIONS} 个" in day, "单日档缺景点上限"
    assert "两顿午餐" in day, "单日档缺餐次规则"


def test_open_trip_contract_contains_narrative_fields():
    prompt = open_generation.open_trip_system_prompt(
        days=3, hotel_clause="", min_active_minutes=MIN_ACTIVE_MINUTES, max_daily_minutes=MAX_DAILY_MINUTES
    )
    assert '"trip_theme":' in prompt
    assert "why_this" in prompt
    assert "叙事句" in prompt
    assert "2-4 条" in prompt
    assert "120 字" in prompt
    assert "防啰嗦" in prompt
    assert "day_options" in prompt


# ---------- 4. 透传：LLM 输出 → plan dict → DailyPlan ----------


class _CapturingClient:
    """client.complete 替身：记录调用参数并返回固定 JSON。"""

    def __init__(self, payload):
        self.payload = json.dumps(payload, ensure_ascii=False)
        self.systems: list[str] = []
        self.kwargs: list[dict] = []

    def complete(self, user_prompt, system_prompt="", **kwargs):
        self.systems.append(system_prompt)
        self.kwargs.append(kwargs)
        return self.payload


def test_llm_open_day_returns_sanitized_narrative_fields(monkeypatch):
    client = _CapturingClient(
        {
            "trip_theme": "题" * 60,
            "theme": "长" * 50,
            "note": "第1天",
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "清水寺",
                    "why_this": "长" * 200,
                    "start_time": "09:00",
                    "end_time": "10:00",
                }
            ],
            "practical_notes": [f"n{i}" for i in range(6)],
            "photo_spots": [{"name": f"s{i}"} for i in range(6)],
            "backup_plan": [{"if": f"条件{i}", "action": f"动作{i}"} for i in range(5)],
            "day_options": [{"label": f"o{i}", "summary": "s", "tradeoff": "t"} for i in range(4)],
            "suggestions": [],
        }
    )
    monkeypatch.setattr(day_stream, "get_llm_client", lambda: client)
    req = GenerateDayRequest(city="京都", day_no=1, days=3)
    plan = day_stream.llm_open_day(req, set())

    # max_tokens 三次上调：2400 → 3200 → 8000。3200 实测装不下"叙事字段 +
    # 24-40 条 suggestions"的输出契约（2026-09-18 量测 6/6 城整段 JSON 被截断
    # → parse 失败 → 该日退化为草案），见 docs/量测-存在性接地-2026-09-18.md §5。
    assert client.kwargs[0]["max_tokens"] == 8000
    # 清洗后全字段到位
    assert plan["trip_theme"] == "题" * 40
    assert plan["theme"] == "长" * 40
    assert len(plan["practical_notes"]) == 4
    assert len(plan["photo_spots"]) == 4
    assert len(plan["backup_plan"]) == 3
    assert len(plan["day_options"]) == 2
    assert plan["items"][0]["why_this"] == "长" * 120


def test_llm_open_day_day1_has_trip_theme_day2_not(monkeypatch):
    client = _CapturingClient(
        {
            "theme": "花园漫步",
            "note": "第2天",
            "items": [{"poi_name": "花园", "item_type": "attraction"}],
            "suggestions": [],
        }
    )
    monkeypatch.setattr(day_stream, "get_llm_client", lambda: client)

    day1 = day_stream.llm_open_day(GenerateDayRequest(city="京都", day_no=1, days=2), set())
    assert day1["trip_theme"] is None  # 模型未输出 → 缺省即空，不报错

    day2 = day_stream.llm_open_day(GenerateDayRequest(city="京都", day_no=2, days=2), set())
    assert day2["trip_theme"] is None
    assert '"trip_theme":' in client.systems[0]  # 第 1 天契约携带
    assert '"trip_theme":' not in client.systems[1]  # 第 2 天契约省略


def test_generate_day_once_daily_plan_carries_all_narrative_fields(monkeypatch):
    monkeypatch.setattr(day_stream.settings, "llm_api_key", "configured")
    monkeypatch.setattr(
        day_stream,
        "llm_open_day",
        lambda req, used: {
            "note": "第1天",
            "theme": "街区巡礼",
            "trip_theme": "京都·千恋万花圣地巡礼",
            "day_options": [{"label": "暴走版", "summary": "八点连轴", "tradeoff": "体力消耗大"}],
            "backup_plan": [{"if": "雨天", "action": "改室内博物馆"}],
            "photo_spots": [{"name": "八坂塔", "tip": "仰拍", "best_time": "清晨"}],
            "practical_notes": ["提前预约"],
            "items": [
                {
                    "item_type": "attraction",
                    "poi_name": "清水寺",
                    "why_this": "圣地巡礼核心打卡点",
                    "start_time": "09:00",
                    "end_time": "11:00",
                    "cost": 0,
                }
            ],
            "suggestions": [],
        },
    )
    plan, source = day_stream.generate_day_once(GenerateDayRequest(city="京都", day_no=1, days=2, context={}))
    assert source == "open"
    assert plan.theme == "清水寺"  # 已排日程主题由最终 items 派生。
    assert plan.note is not None
    assert "09:00–11:00 清水寺" in plan.note
    assert plan.trip_theme == "京都·千恋万花圣地巡礼"  # day_no=1 含 trip_theme
    assert plan.items[0].why_this == "圣地巡礼核心打卡点"  # 随 TripItem 自然携带
    assert plan.day_options[0].label == "暴走版"
    assert plan.backup_plan[0].if_ == "雨天"
    assert plan.photo_spots[0].name == "八坂塔"
    assert plan.practical_notes == ["提前预约"]


def test_generate_day_once_day2_has_no_trip_theme(monkeypatch):
    monkeypatch.setattr(day_stream.settings, "llm_api_key", "configured")
    monkeypatch.setattr(
        day_stream,
        "llm_open_day",
        lambda req, used: {
            "note": "第2天",
            "theme": "岚山慢行",
            "items": [
                {"item_type": "attraction", "poi_name": "岚山", "start_time": "09:00", "end_time": "11:00", "cost": 0}
            ],
            "suggestions": [],
        },
    )
    plan, _ = day_stream.generate_day_once(GenerateDayRequest(city="京都", day_no=2, days=2, context={}))
    assert plan.trip_theme is None  # day_no=2 无 trip_theme


def test_llm_open_trip_sanitizes_plans_and_injects_trip_theme(monkeypatch):
    client = _CapturingClient(
        {
            "trip_theme": "题" * 60,
            "daily_plans": [
                {
                    "day_no": 1,
                    "theme": "长" * 50,
                    "note": "d1",
                    "items": [{"poi_name": "清水寺", "item_type": "attraction"}],
                    "practical_notes": ["n"] * 6,
                },
                {"day_no": 2, "note": "d2", "items": [{"poi_name": "岚山", "item_type": "attraction"}]},
            ],
            "suggestions": [],
        }
    )
    monkeypatch.setattr(day_prompts, "get_llm_client", lambda: client)
    req = GenerateDayRequest(city="京都", day_no=1, days=2, needs_hotel=True)
    plans, suggestions = day_prompts.llm_open_trip(req)

    # max_tokens 按天数等比：days*8000，上限 16000（2 天 → 16000；单日需 8000 实测口径）
    assert client.kwargs[0]["max_tokens"] == 2 * 8000
    assert plans[0]["trip_theme"] == "题" * 40  # 顶层主题截 40 后注入
    assert plans[1]["trip_theme"] == "题" * 40  # 其余天兜底继承，随装配透传
    assert plans[0]["theme"] == "长" * 40
    assert len(plans[0]["practical_notes"]) == 4
    assert suggestions == []
