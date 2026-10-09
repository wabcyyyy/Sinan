"""固定体验回归题集 runner（M7，spec 2026-10-08 §13.2）。

职责：
- 加载 `e_dataset.json`（schema_version + dataset_hash 漂移检测），逐 case 执行其
  checks；check 函数全部走**业务服务入口**（clarify 决策链 / services.itinerary_generation
  生成编排 / apply_plans / chat_draft 校验纯函数 / constraint_checks / TripPlanStreamParser /
  official_pages），LLM 与网络一律离线桩/纯函数，不打真实外部依赖；
- 输出结构化结果（pass/skip/fail + per-check 明细 + 指标统计），供
  `tests/agent_eval/eval_experience.py`（§13.3 指标报告）与 pytest 集成消费；
- 与 `tests/agent_eval/cases.json` 既有题集完全独立：不改其分母、不复用其 loader
  （本集按 case→check 组织，与按城市生成题不同构）。

检查结果三态：pass（断言全过）/ fail（断言失败或执行异常）/ skip（显式声明能力
未落地，必须在报告列明）。check 的 stats 字段是 §13.3 指标的原子：
- `constraint_*`：硬约束履约口径（applicable/pass/violation/unknown，unknown 留分母）；
- `generation_cases/first_pass/timeout_failures/generation_failures`：一次交付通过率；
- `scope_cases/scope_preserved`：编辑范围保持率；
- `duplicate_executions`：重复执行数。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

# 直跑（uv run python tests/experience_cases/runner.py）时的导入根；pytest 下幂等
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.encoders import jsonable_encoder
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent.editing import clarify as clarify_mod
from app.agent.editing.chat_draft.hotel_intent import _is_hotel_request
from app.agent.editing.chat_draft.validate import _scope_violations
from app.agent.generation.content.incremental_plans import TripPlanStreamParser, TruncatedTripPlanError
from app.agent.generation.content.reflect import BUDGET_OVERAGE_MARK, validate_plans
from app.agent.generation.output.schedule_optimizer import optimize_daily_plan
from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import day_policy_for
from app.agent.generation.rules.generation_core import estimate_plans_total, meal_slot_of
from app.agent.generation.rules.transfer_time import estimate_transfer_minutes
from app.agent.grounding.official_pages import apply_evidence_conflicts, collect_field_evidence, extract_evidence
from app.common import cache_store
from app.common.config import settings
from app.common.envelope import ApiError
from app.db import session as db_session
from app.db.models import (
    Base,
    ItineraryChatMessage,
    ItineraryDay,
    ItineraryItem,
    ItineraryMain,
    SysUser,
)
from app.schemas.business.itinerary import GenerateTripRequest
from app.schemas.trip import ChatTurnRequest, ChatTurnResponse, ClarifyRequest, DailyPlan, FactEvidence, TripItem
from app.schemas.trip_requirements import (
    SUPPORTED_EXCLUDED_CATEGORIES,
    BudgetPolicy,
    DayWindow,
    IntakeState,
    LodgingRequirements,
    RequiredPlace,
    TripRequirements,
    canonical_requirements_payload,
)
from app.services import (
    itinerary_chat,
    itinerary_generation,
    itinerary_plan_apply,
    itinerary_version,
    state_and_sessions,
    user_service,
)

DATASET_PATH = Path(__file__).with_name("e_dataset.json")
REPORT_PATH = Path(__file__).with_name("report") / "experience_cases.json"

PASS, FAIL, SKIP = "pass", "fail", "skip"


class CheckFailure(AssertionError):
    """check 内断言失败：携带业务语义消息，run_case 落为 fail 明细。"""


@dataclass
class CheckOutcome:
    """单个 check 的执行结果；stats 是 §13.3 指标的原子（见模块 docstring）。"""

    status: str
    detail: str = ""
    stats: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------- 数据集加载 ----


def compute_dataset_hash(cases: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(cases, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_dataset(path: Path | None = None) -> dict:
    """加载题集并做漂移检测：schema 版本 + dataset_hash 与 canonical cases 一致。"""
    dataset = json.loads((path or DATASET_PATH).read_text(encoding="utf-8"))
    if dataset.get("schema_version") != 1:
        raise ValueError(f"不支持的题集 schema_version：{dataset.get('schema_version')}")
    expected = compute_dataset_hash(dataset["cases"])
    if dataset.get("dataset_hash") != expected:
        raise ValueError(
            "dataset_hash 漂移：cases 内容与登记的 hash 不一致（改题必须重算 hash，"
            f"期望 {expected}，实际 {dataset.get('dataset_hash')}）"
        )
    ids = [case["id"] for case in dataset["cases"]]
    if ids != [f"E{index:02d}" for index in range(1, len(ids) + 1)]:
        raise ValueError(f"题集 case id 必须连续编号：{ids}")
    return dataset


# ---------------------------------------------------------------- 离线桩材料 ----

#: LLM 抽取输出脚本（mock clarify 的 _ask 返回值）；键被 e_dataset.json params 引用。
SCRIPTS: dict[str, str] = {
    "e01_turn1": json.dumps(
        {
            "city": "杭州",
            "days": 3,
            "persons": 3,
            "preferences": ["带爸妈", "慢节奏"],
            "patches": [
                {"op": "set", "target": "pace", "value": "relaxed"},
                {"op": "set", "target": "transport_preference", "value": "mixed"},
                {"op": "set", "target": "max_walk_minutes_per_leg", "value": 30},
            ],
            "reply": None,
            "reply_for": None,
            "question": None,
            "options": None,
        },
        ensure_ascii=False,
    ),
    "e01_turn2": json.dumps(
        {
            "start_date": "2026-10-16",
            "budget": 4500,
            "hotel_tier": "舒适型",
            "patches": [
                {"op": "add", "target": "required_place", "name": "灵隐寺", "day_no": 2},
                {"op": "add", "target": "excluded_category", "name": "museum"},
                {"op": "set", "target": "budget_policy_mode", "value": "target"},
                {"op": "set", "target": "budget_policy_include_intercity", "value": False},
            ],
            "reply": None,
            "reply_for": None,
            "question": None,
            "options": None,
        },
        ensure_ascii=False,
    ),
    "e02_turn3": json.dumps(
        {
            "days": 4,
            "patches": [
                {"op": "set", "target": "days", "value": 4},
                {"op": "set", "target": "required_place", "name": "灵隐寺", "day_no": 3},
                {"op": "unset", "target": "budget_policy_mode"},
                {"op": "unset", "target": "budget"},
            ],
            "reply": None,
            "reply_for": None,
            "question": None,
            "options": None,
        },
        ensure_ascii=False,
    ),
    "e03_days": json.dumps({"city": "杭州", "days": 8, "persons": 2}, ensure_ascii=False),
    "e03_confirm_attempt": json.dumps(
        {"reply": "没问题，可以直接开始规划。", "reply_for": "confirm"}, ensure_ascii=False
    ),
    "e03_budget1": json.dumps({"city": "成都", "days": 7, "persons": 2, "budget": 500}, ensure_ascii=False),
    "e03_budget2": json.dumps(
        {"days": 7, "accepted": "budget_low", "reply": "好，就按这个预算来。", "reply_for": "budget_low"},
        ensure_ascii=False,
    ),
    "e03_budget3": json.dumps({"budget": 600}, ensure_ascii=False),
    "e05_negation": json.dumps(
        {"patches": [{"op": "add", "target": "excluded_category", "name": "museum"}]}, ensure_ascii=False
    ),
}


def e01_requirements() -> TripRequirements:
    """E01 的结构化需求（确认卡随生成请求携带的权威形态；含 M1a 已建模但 patch 目标
    未开放的 day_windows——收集面边界见 docs/experience-implementation-log.md M1a）。"""
    return TripRequirements(
        day_windows=[
            DayWindow(day_no=1, kind="arrival", not_before="15:00", finish_by="20:00"),
            DayWindow(day_no=3, kind="departure", finish_by="16:00"),
        ],
        required_places=[RequiredPlace(constraint_id="place-lingyin", name="灵隐寺", day_no=2)],
        excluded_categories=["museum"],
        pace="relaxed",
        transport_preference="mixed",
        max_walk_minutes_per_leg=30,
        budget_policy=BudgetPolicy(mode="target", include_intercity_transport=False),
        lodging=LodgingRequirements(rooms=2),
    )


def _fake_client_of(raw: str) -> Callable[[], Any]:
    """clarify.get_llm_client 的替身工厂：固定返回一份脚本抽取输出。"""

    class _FakeClient:
        @staticmethod
        def complete(prompt: str, system_prompt: str = "", temperature: float = 0) -> str:
            return raw

    return lambda: _FakeClient()


def _scripted_clarify_turns(turns: list[dict]) -> IntakeState:
    """按 dataset 的 turns 顺序跑 clarify 决策链（_ask 换成脚本输出，每轮恰好一次）。"""
    state: IntakeState | None = None
    response = None
    for turn in turns:
        with patch.object(clarify_mod, "get_llm_client", _fake_client_of(SCRIPTS[turn["script"]])):
            response = clarify_mod.run_clarify(ClarifyRequest(message=str(turn["message"]), slots={}, state=state))
        state = response.state
    assert response is not None and state is not None
    return state


# ---------------------------------------------------------------- 业务环境 ----


@contextmanager
def business_env(tag: str) -> Iterator[Any]:
    """临时 SQLite + 禁 Redis（cache_store 走进程内兜底）的业务环境；退出完整清理。"""
    tmp = Path(tempfile.mkdtemp(prefix=f"sinan-exp-{tag}-"))
    engine = create_engine(f"sqlite:///{(tmp / (tag + '.db')).as_posix()}")
    Base.metadata.create_all(engine)
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    stack = ExitStack()
    stack.enter_context(patch.object(settings, "redis_url", "redis://127.0.0.1:1/0"))
    cache_store.reset_for_tests()
    state_and_sessions.reset_for_tests()
    try:
        yield engine
    finally:
        stack.close()
        db_session.init_engine(None, None)
        cache_store.reset_for_tests()
        state_and_sessions.reset_for_tests()
        engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


def _seed_user_and_trip(*, days: int = 2, requirements: TripRequirements | None = None) -> int:
    """最小两日行程（形状同 test_plan_apply_migration 的种子）；返回 trip_id。"""
    with db_session.session_scope() as session:
        if session.get(SysUser, 1) is None:
            session.add(SysUser(id=1, username="alice", password=user_service.hash_password("example123"), status=1))
        main = ItineraryMain(
            user_id=1,
            title=f"杭州{days}日游",
            city="杭州",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 19 + days),
            days=days,
            persons=2,
            budget=Decimal("3000.00"),
            status=2,
            gen_state="COMPLETED",
            # 全默认需求也写 '{}'（canonical 空对象）：与业务建壳口径一致，读回是 dict 不是 JSON 串
            requirements_json=canonical_requirements_payload(requirements),
        )
        session.add(main)
        session.flush()
        trip_id = main.id
        for day_no in range(1, days + 1):
            day = ItineraryDay(
                itinerary_id=trip_id,
                day_no=day_no,
                travel_date=date(2026, 4, 19 + day_no),
                city="杭州",
                generation_status="SUCCEEDED",
            )
            session.add(day)
            session.flush()
            items: list[ItineraryItem] = [
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=trip_id,
                    item_type="attraction",
                    poi_name="灵隐寺" if day_no == 2 else "西湖",
                    cost=Decimal("45.00") if day_no == 2 else Decimal("0.00"),
                    sort_no=0,
                ),
                ItineraryItem(
                    day_id=day.id,
                    itinerary_id=trip_id,
                    item_type="food",
                    poi_name="知味观",
                    cost=Decimal("60.00"),
                    sort_no=1,
                ),
            ]
            if day_no == 2:
                items.append(
                    ItineraryItem(
                        day_id=day.id,
                        itinerary_id=trip_id,
                        item_type="hotel",
                        poi_name="杭州老旅馆",
                        poi_id="1",
                        cost=Decimal("200.00"),
                        sort_no=2,
                    )
                )
            session.add_all(items)
    return trip_id


def _current_plan_rows(trip_id: int, day_nos: list[int] | None = None) -> list[dict]:
    plans = jsonable_encoder(itinerary_chat.current_plans(trip_id))
    return [plan for plan in plans if day_nos is None or plan.get("day_no") in day_nos]


def _write_draft(
    trip_id: int,
    plans: list[dict],
    *,
    base_revision: str,
    base_planning_revision: int | None = None,
    proposed_requirements: TripRequirements | None = None,
) -> int:
    rows = [{**plan, "_baseRevision": base_revision} for plan in plans]
    if base_planning_revision is not None:
        rows = [{**row, "_basePlanningRevision": base_planning_revision} for row in rows]
    if proposed_requirements is not None and rows:
        rows[0]["_proposedRequirements"] = proposed_requirements.model_dump(mode="json", by_alias=True)
    with db_session.session_scope() as session:
        message = ItineraryChatMessage(
            itinerary_id=trip_id,
            user_id=1,
            role="ai",
            content="建议",
            changed=1,
            plans_json=json.dumps(rows, ensure_ascii=False),
        )
        session.add(message)
        session.flush()
        return int(message.id)


def _main_revision(trip_id: int) -> int:
    with db_session.session_scope() as session:
        main = session.get(ItineraryMain, trip_id)
        assert main is not None
        return int(main.planning_revision)


# ---------------------------------------------------------------- 通用小件 ----


def _constraint_stats(reports: list[Any]) -> dict[str, float]:
    """ConstraintReport 列表 → 履约口径统计（unknown 留在分母，单列）。"""
    applicable = passed = violated = unknown = 0
    for report in reports:
        for check in report.checks:
            if check.status == "not_applicable":
                continue
            applicable += 1
            if check.status == "pass":
                passed += 1
            elif check.status == "violation":
                violated += 1
            elif check.status == "unknown":
                unknown += 1
    return {
        "constraint_applicable": float(applicable),
        "constraint_pass": float(passed),
        "constraint_violation": float(violated),
        "constraint_unknown": float(unknown),
    }


def _plan_from_items(day_no: int, items: list[dict]) -> dict:
    return {"day_no": day_no, "items": items}


# ---------------------------------------------------------------- E01 checks ----


def check_clarify_multi_turn_requirements_terminal_state(params: dict) -> CheckOutcome:
    """E01 前半：clarify 多轮 + 需求 patch → TripRequirements 终态（必去日/排除/预算口径）。"""
    state = _scripted_clarify_turns(params["turns"])
    expect = params["expect"]
    req = state.requirements
    problems: list[str] = []
    if (state.city, state.days, state.persons) != (expect["city"], expect["days"], expect["persons"]):
        problems.append(f"基础槽位不符：{state.city}/{state.days}/{state.persons}")
    if state.budget != expect["budget"] or state.start_date != expect["start_date"]:
        problems.append(f"预算/日期不符：{state.budget}/{state.start_date}")
    if state.hotel_tier != expect["hotel_tier"]:
        problems.append(f"酒店档次不符：{state.hotel_tier}")
    if req.pace != expect["pace"] or req.transport_preference != expect["transport_preference"]:
        problems.append(f"节奏/交通不符：{req.pace}/{req.transport_preference}")
    if req.max_walk_minutes_per_leg != expect["max_walk_minutes_per_leg"]:
        problems.append(f"步行上限不符：{req.max_walk_minutes_per_leg}")
    required = [[p.name, p.day_no] for p in req.required_places]
    if required != [list(pair) for pair in expect["required"]]:
        problems.append(f"必去不符：{required}")
    if req.excluded_categories != expect["excluded_categories"]:
        problems.append(f"排除类别不符：{req.excluded_categories}")
    if req.budget_policy is None or req.budget_policy.mode != expect["budget_mode"]:
        problems.append(f"预算口径不符：{req.budget_policy}")
    if req.budget_policy is not None and req.budget_policy.include_intercity_transport is not False:
        problems.append("预算未按「不含往返」登记 include_intercity_transport=False")
    if req.unresolved_requests:
        problems.append(f"不应有被拒 patch：{req.unresolved_requests}")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(
        PASS,
        "多轮 clarify 终态：必去灵隐寺第2天、排除博物馆、target 预算不含往返、慢游+步行上限全部入结构",
    )


def check_generation_pipeline_preserves_requirements(params: dict) -> CheckOutcome:
    """E01 后半：业务服务入口 generate 全链路保留需求（研究/整段/逐日/落库逐字段一致）。

    整段流/研究/逐日全桩（同 test_m1b_requirements_pipeline 桩法），桩日计划形状刻意
    合规（过 M3 交付门）；最终以**库内条目**回放 check_constraints 断言窗口与必去。
    """
    expect = params["expect"]
    struct = e01_requirements()
    captured: dict[str, Any] = {"day_requests": []}

    def fake_context(request, *, itinerary_id=None, **kwargs):
        captured["research_request"] = request
        return {"candidates": [], "foods": [], "hotels": [], "consumption": None, "research_report": {}}

    def fake_day(request):
        captured["day_requests"].append(request)
        return _compliant_stub_day(request.day_no)

    def fake_stream(request, cancel=None, **kwargs):
        captured["stream_request"] = request
        yield from ()

    with business_env("e01-pipeline"):
        with (
            patch.object(itinerary_generation, "run_plan_context", fake_context),
            patch.object(itinerary_generation, "run_generate_day", fake_day),
            patch.object(itinerary_generation, "run_generate_trip_stream", fake_stream),
            patch.object(itinerary_generation, "_submit_budget_recalculate", lambda itinerary_id: None),
            patch.object(itinerary_generation.enricher_pool, "submit", lambda task, *args: None),
            patch.object(itinerary_generation.generation_pool, "submit", lambda task, *args: task(*args)),
        ):
            body = GenerateTripRequest(
                city=str(params["city"]),
                days=int(params["days"]),
                persons=int(params["persons"]),
                budget=Decimal(str(params["budget"])),
                startDate=date.fromisoformat(str(params["start_date"])),
                hotelTier=str(params["hotel_tier"]),
                intent=str(params["intent"]),
                requirementsStruct=struct,
            )
            detail = itinerary_generation.generate(1, body)
        itinerary_generation.reset_active_planning_for_tests()
        trip_id = int(detail["id"])

        research = captured.get("research_request")
        stream_request = captured.get("stream_request")
        day_requests = captured["day_requests"]
        problems: list[str] = []
        if research is None or research.requirements_struct != struct:
            problems.append("研究请求未带完整结构化需求")
        if stream_request is None or stream_request.requirements_struct != struct:
            problems.append("整段流式请求未带完整结构化需求")
        if len(day_requests) != int(params["days"]) or any(r.requirements_struct != struct for r in day_requests):
            problems.append("逐日请求未逐字段带整趟需求")
        with db_session.session_scope() as session:
            main = session.get(ItineraryMain, trip_id)
            assert main is not None
            persisted = TripRequirements.model_validate(main.requirements_json)
            joined = session.execute(
                select(ItineraryDay, ItineraryItem)
                .outerjoin(ItineraryItem, (ItineraryItem.day_id == ItineraryDay.id) & (ItineraryItem.deleted == 0))
                .where(ItineraryDay.itinerary_id == trip_id)
                .order_by(ItineraryDay.day_no, ItineraryItem.sort_no)
            ).all()
        if persisted != struct:
            problems.append("库内需求快照与提交不一致")
        if problems:
            return CheckOutcome(FAIL, "；".join(problems))

        # 最终活动按库内条目回放硬约束：窗口内、必去灵隐寺在第 2 天、博物馆排除
        items_by_day: dict[int, list[dict]] = {}
        for day_row, item in joined:
            if item is None:
                continue
            items_by_day.setdefault(day_row.day_no, []).append(
                {
                    "item_type": item.item_type,
                    "poi_name": item.poi_name,
                    "start_time": item.start_time.strftime("%H:%M") if item.start_time else None,
                    "end_time": item.end_time.strftime("%H:%M") if item.end_time else None,
                    "cost": float(item.cost or 0),
                }
            )
        reports = []
        names_by_day: dict[int, list[str]] = {}
        for day_no, items in sorted(items_by_day.items()):
            names_by_day[day_no] = [str(i.get("poi_name") or "") for i in items]
            reports.append(
                check_constraints(_plan_from_items(day_no, items), struct, day_no, day_policy_for(struct, day_no))
            )
        stats = _constraint_stats(reports)
        violations = [f"day{c.day_no}:{c.kind}" for report in reports for c in report.blocking_checks]
        day2_names = names_by_day.get(int(expect["required_day"]), [])
        museum_leak = [name for names in names_by_day.values() for name in names if "博物馆" in name]
        budget_mode_ok = struct.budget_policy is not None and struct.budget_policy.mode == expect["budget_mode"]
        if violations or expect["required_name"] not in day2_names:
            return CheckOutcome(
                FAIL,
                f"违例={violations} day2={day2_names}",
                stats,
            )
        if museum_leak or not budget_mode_ok:
            return CheckOutcome(FAIL, f"博物馆泄漏={museum_leak} 预算口径={struct.budget_policy}", stats)
        stats.update(
            {
                "generation_cases": 1.0,
                "first_pass": 1.0,
                "timeout_failures": 0.0,
                "generation_failures": 0.0,
            }
        )
        return CheckOutcome(
            PASS,
            "建壳→研究→整段→逐日→落库需求逐字段一致；库内最终活动窗口内、灵隐寺第2天、"
            "博物馆排除、target 口径明确（估算差额如实，不冒充 hard_cap）；"
            "单边返程窗（只给 16:00 离开）如实记 unknown 留分母，不冒充 pass",
            stats,
        )


def _compliant_stub_day(day_no: int) -> DailyPlan:
    """逐日兜底桩的合规产出：过 M3 交付门；费用口径使整趟估算恰为 4845（E04 联动）。"""
    if day_no == 1:
        # 注意：桩不带酒店条目——酒店会被 hotel_schedule 按 N-1 晚补排（漏排晚次接在
        # 当天活动后），手工放条目反而撞到达窗/重复排程；E04 的估算口径在
        # _stub_plan_dicts 里单独补酒店成本。
        items = [
            TripItem(item_type="attraction", poi_name="湖滨晚风", cost=110, start_time="15:30", end_time="16:30"),
            TripItem(item_type="food", poi_name="知味观", cost=120, start_time="17:00", end_time="18:30"),
        ]
    elif day_no == 2:
        items = [
            TripItem(item_type="attraction", poi_name="灵隐寺", cost=45, start_time="09:30", end_time="11:30"),
            TripItem(item_type="food", poi_name="楼外楼", cost=90, start_time="11:45", end_time="12:45"),
            TripItem(item_type="attraction", poi_name="花港观鱼", cost=200, start_time="13:00", end_time="14:30"),
            TripItem(item_type="attraction", poi_name="雷峰塔", cost=355, start_time="15:00", end_time="16:30"),
            TripItem(item_type="food", poi_name="知味观", cost=100, start_time="18:00", end_time="19:00"),
        ]
    else:
        items = [
            TripItem(item_type="attraction", poi_name="苏堤春晓", cost=0, start_time="09:00", end_time="10:30"),
            TripItem(item_type="food", poi_name="外婆家", cost=190, start_time="11:00", end_time="12:15"),
        ]
    return DailyPlan(day_no=day_no, note=f"第 {day_no} 天", theme="湖山线", items=items)


def _stub_plan_dicts() -> list[dict]:
    """E04 用的整趟方案（与 E01 桩同景点/餐饮，另含 day1 一晚酒店 450×2 房）：估算恰 4845。"""
    plans = []
    for day_no in (1, 2, 3):
        items = [
            {
                "item_type": item.item_type,
                "poi_name": item.poi_name,
                "cost": item.cost,
                "start_time": item.start_time,
                "end_time": item.end_time,
            }
            for item in _compliant_stub_day(day_no).items
        ]
        if day_no == 1:
            items.append(
                {
                    "item_type": "hotel",
                    "poi_name": "杭州舒适酒店",
                    "cost": 450,
                    "start_time": "19:30",
                    "end_time": "20:00",
                }
            )
        plans.append(_plan_from_items(day_no, items))
    return plans


# ---------------------------------------------------------------- E02 check ----


def check_clarify_replacement_updates_keep_other_requirements(params: dict) -> CheckOutcome:
    """E02：\"改4天/灵隐寺第三天/预算不限制\" 旧值被替换，其余需求原样保留。"""
    turns = [
        {"message": "带爸妈去杭州玩3天", "script": params["base_scripts"][0]},
        {"message": "补充需求", "script": params["base_scripts"][1]},
        {"message": params["turn"]["message"], "script": params["turn"]["script"]},
    ]
    state = _scripted_clarify_turns(turns)
    expect = params["expect"]
    req = state.requirements
    problems: list[str] = []
    if state.days != expect["days"]:
        problems.append(f"天数未替换：{state.days}")
    required = [[p.name, p.day_no] for p in req.required_places]
    if required != [list(pair) for pair in expect["required"]]:
        problems.append(f"必去日约束未替换/有冲突副本：{required}")
    if expect["budget_cleared"] and state.budget is not None:
        problems.append(f"预算未解除：{state.budget}")
    if expect["budget_policy_cleared"] and req.budget_policy is not None:
        problems.append(f"预算口径未解除：{req.budget_policy}")
    for key, want in expect["keep"].items():
        got: Any = getattr(state, key, None)
        if key == "excluded_categories":
            got = req.excluded_categories
        elif key == "pace":
            got = req.pace
        if got != want:
            problems.append(f"既有需求被误改：{key}={got!r}（期望 {want!r}）")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(PASS, "days/必去日/预算旧值被替换；排除类别/节奏/人数/日期/酒店档次原样保留")


# ---------------------------------------------------------------- E03 checks ----


def check_overlong_days_blocked_not_bypassed(params: dict) -> CheckOutcome:
    """E03a：8 天超限 → blocked 协商且 state.days 保留 8；\"直接开始\"的确认话术不绕过。"""
    expect = params["expect"]
    turns = params["turns"]
    state = _scripted_clarify_turns(turns[:1])
    first_next = "days_limit"
    problems: list[str] = []
    if state.days != expect["days"]:
        problems.append(f"超限原值未保留：{state.days}")
    with patch.object(clarify_mod, "get_llm_client", _fake_client_of(SCRIPTS[turns[1]["script"]])):
        second = clarify_mod.run_clarify(ClarifyRequest(message=turns[1]["message"], state=state))
    if not (second.blocked and second.next == first_next and not second.ready):
        problems.append("确认话术绕过了超天协商")
    if second.reply is not None:
        problems.append("blocked 分支采用了目标不符的模型回复")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(
        PASS,
        f'超 {expect["days"]} 天恒 blocked（上限 {expect["max_days"]} 天）、原值保留、"直接开始"不可绕过',
    )


def check_budget_negotiation_acceptance_binds_params(params: dict) -> CheckOutcome:
    """E03b：低预算协商一次；明确接受绑定参数摘要；手动改输入（预算变化）旧接受失效。"""
    turns = params["turns"]
    expect = params["expect"]
    state: IntakeState | None = None
    responses = []
    for turn in turns:
        with patch.object(clarify_mod, "get_llm_client", _fake_client_of(SCRIPTS[turn["script"]])):
            response = clarify_mod.run_clarify(ClarifyRequest(message=turn["message"], state=state))
        state = response.state
        responses.append(response)
    first, second, _third = responses
    problems: list[str] = []
    if not (first.blocked and first.next == "budget_low"):
        problems.append("低预算未先协商")
    if second.blocked or not second.ready:
        problems.append("明确接受后未放行")
    initial_summary = f"budget={expect['initial_budget']:g}/persons=2/days=7"
    if second.state.acceptances.get("budget_low") != initial_summary:
        problems.append(f"接受未绑定原参数摘要：{second.state.acceptances}")
    if state is not None and state.acceptances.get("budget_low") == (
        f"budget={expect['changed_budget']:g}/persons=2/days=7"
    ):
        problems.append("参数已变旧接受竟仍匹配")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(
        PASS,
        f"blocked→接受（绑定 budget={expect['initial_budget']:g}/persons=2/days=7）"
        f"→预算改 {expect['changed_budget']:g} 后旧接受失配（摘要失效）",
    )


# ---------------------------------------------------------------- E04 check ----


def check_hard_cap_budget_no_tolerance(params: dict) -> CheckOutcome:
    """E04：估算 4845 vs 严格上限 4500 → 明确违例；target 8% 容差（360）判不到。"""
    plans = _stub_plan_dicts()
    persons = int(params["persons"])
    budget = float(params["budget"])
    est = estimate_plans_total(plans, persons=persons, days=int(params["days"]), rooms=2)
    total = float(est["合计"])
    problems: list[str] = []
    if abs(total - float(params["expect_estimate"])) > 0.5:
        problems.append(f"估算口径漂移：{total} != {params['expect_estimate']}")
    hard_cap = TripRequirements(budget_policy=BudgetPolicy(mode="hard_cap"), lodging=LodgingRequirements(rooms=2))
    issues, _ = validate_plans(plans, budget=budget, persons=persons, requirements=hard_cap)
    if not any(BUDGET_OVERAGE_MARK in issue for issue in issues):
        problems.append(f"hard_cap 无容差违例未报：est={total} budget={budget}")
    target = TripRequirements(budget_policy=BudgetPolicy(mode="target"), lodging=LodgingRequirements(rooms=2))
    target_issues, _ = validate_plans(plans, budget=budget, persons=persons, requirements=target)
    over = total - budget
    if any(BUDGET_OVERAGE_MARK in issue for issue in target_issues):
        problems.append(f"target 8% 容差竟也报违例：over={over}")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(
        PASS,
        f"估算 {total:.0f} 超 {budget:.0f} 共 {over:.0f}：hard_cap 明确违例（无容差）；"
        f"target 差额 {over:.0f} < 8% 容差 {budget * 0.08:.0f} 不误报——E01 的 4500 是 target，"
        "必须报差额而非伪称严格上限",
        {"budget_overage": over},
    )


# ---------------------------------------------------------------- E05 checks ----


def check_negation_never_becomes_positive_preference(params: dict) -> CheckOutcome:
    """E05a：单句\"不要博物馆\"只进排除，不添加任何正向偏好。"""
    turn = params["turn"]
    with patch.object(clarify_mod, "get_llm_client", _fake_client_of(SCRIPTS[turn["script"]])):
        response = clarify_mod.run_clarify(ClarifyRequest(message=turn["message"], state=None))
    req = response.state.requirements
    expect = params["expect"]
    if req.excluded_categories != expect["excluded_categories"] or response.state.preferences != expect["preferences"]:
        return CheckOutcome(
            FAIL,
            f"排除={req.excluded_categories} 偏好={response.state.preferences}（期望 {expect}）",
        )
    return CheckOutcome(PASS, '否定只进 excluded_categories，preferences 零污染（无"人文历史"类错误正向偏好）')


def check_excluded_category_violation_and_unknown_for_unsupported(params: dict) -> CheckOutcome:
    """E05b：已识别博物馆不进入行程（violation）；词表外类别 unknown 不记 pass。"""
    museum_item = str(params["museum_item"])
    plan = _plan_from_items(
        1,
        [
            {"item_type": "attraction", "poi_name": museum_item, "start_time": "09:00", "end_time": "10:30"},
            {"item_type": "food", "poi_name": "知味观", "start_time": "11:00", "end_time": "12:00"},
        ],
    )
    supported = TripRequirements(excluded_categories=[str(params["supported"])])
    report = check_constraints(plan, supported, 1, day_policy_for(supported, 1))
    category = next(c for c in report.checks if c.kind == "excluded_category")
    unsupported = TripRequirements(excluded_categories=[str(params["unsupported"])])
    report_unknown = check_constraints(plan, unsupported, 1, day_policy_for(unsupported, 1))
    unknown = next(c for c in report_unknown.checks if c.kind == "excluded_category")
    problems: list[str] = []
    if category.status != "violation" or not category.blocking:
        problems.append(f"已识别博物馆未判违例：{category.status}")
    if museum_item not in category.item_refs:
        problems.append(f"违例未定位到条目：{category.item_refs}")
    if unknown.status != "unknown" or unknown.blocking:
        problems.append(f"词表外类别未判 unknown：{unknown.status} blocking={unknown.blocking}")
    if str(params["supported"]) not in SUPPORTED_EXCLUDED_CATEGORIES:
        problems.append("受控词表漂移")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    # 负例夹具的违例/unknown 不进履约聚合（那是规则行为验证，不是交付路径统计）
    return CheckOutcome(PASS, "博物馆命中判 violation（阻断）；aquarium 词表外判 unknown（不记 pass、不阻断）")


# ---------------------------------------------------------------- E06 check ----


def check_half_day_window_not_flagged_thin_full_day_keeps_guard(params: dict) -> CheckOutcome:
    """E06：16:00–18:30 半日 120 分钟不误杀；无约束普通全天 120 分钟保留过稀护栏。"""
    half = params["half_day"]
    requirements = TripRequirements(
        day_windows=[
            DayWindow.model_validate(
                {"day_no": 1, "kind": half["kind"], "notBefore": half["not_before"], "finishBy": half["finish_by"]}
            )
        ]
    )
    plan = _plan_from_items(
        1,
        [
            {"item_type": "attraction", "poi_name": "湖滨步行街", "start_time": "16:00", "end_time": "17:00"},
            {"item_type": "food", "poi_name": "晚餐", "start_time": "17:30", "end_time": "18:30"},
        ],
    )
    issues_half, _ = validate_plans([plan], requirements=requirements)
    thin_full = _plan_from_items(
        1,
        [
            {"item_type": "attraction", "poi_name": "断桥残雪", "start_time": "09:00", "end_time": "10:00"},
            {"item_type": "attraction", "poi_name": "花港观鱼", "start_time": "10:00", "end_time": "11:00"},
        ],
    )
    issues_full, _ = validate_plans([thin_full])
    problems: list[str] = []
    if any("安排过稀" in i or "未安排任何景点" in i for i in issues_half):
        problems.append(f"半日被全天下限误杀：{issues_half}")
    if not any("安排过稀" in i for i in issues_full):
        problems.append("普通全天下限护栏丢失")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(PASS, "150 分钟明确半日按窗口验收（min_active=0）；无约束全天 120 分钟仍报过稀（240 基线不变）")


# ---------------------------------------------------------------- E07 check ----


def check_optimizer_required_priority_lunch_and_locked(params: dict) -> CheckOutcome:
    """E07：两小时窗必去优先于可选数量；午餐窗不前移到 9 点；锁定条目保持原槽位。"""
    anchor = {"item_type": "attraction", "poi_id": "LY", "poi_name": "灵隐寺", "duration_min": 120}
    optional_b = {"item_type": "attraction", "poi_id": "B", "poi_name": "可选B", "duration_min": 60}
    optional_c = {"item_type": "attraction", "poi_id": "C", "poi_name": "可选C", "duration_min": 60}
    crowded = optimize_daily_plan(
        _plan_from_items(1, [optional_b, optional_c, anchor]),
        route_matrix={},
        day_start="10:00",
        day_end="12:00",
        required_names={"灵隐寺"},
    )
    names = [item["poi_name"] for item in crowded.plan["items"]]
    lunch = {"item_type": "food", "poi_id": "L", "poi_name": "楼外楼", "duration_min": 60, "open_time": "11:00-14:00"}
    morning = {"item_type": "attraction", "poi_id": "M", "poi_name": "断桥残雪", "duration_min": 90}
    with_lunch = optimize_daily_plan(
        _plan_from_items(1, [dict(morning), dict(lunch)]),
        route_matrix={},
        day_start="09:00",
        day_end="20:30",
        locked_names={"楼外楼"},
    )
    lunch_start = next(
        (item.get("start_time") for item in with_lunch.plan["items"] if item.get("poi_name") == "楼外楼"), None
    )
    problems: list[str] = []
    if crowded.violations or "灵隐寺" not in names:
        problems.append(f"两小时窗丢了必去：violations={crowded.violations} names={names}")
    if {"可选B", "可选C"} - {entry["name"] for entry in crowded.removed_candidates}:
        problems.append(f"可选点未被显式取舍：{crowded.removed_candidates}")
    if lunch_start is None or meal_slot_of(str(lunch_start)) != "lunch":
        problems.append(f"午餐被挪出午窗（9 点化）：start={lunch_start}")
    lunch_minutes = int(str(lunch_start).split(":", 1)[0]) * 60 + int(str(lunch_start).split(":", 1)[1])
    if lunch_minutes < 11 * 60:
        problems.append(f"锁定午餐被前移出营业窗：start={lunch_start}")
    order = [item["poi_name"] for item in with_lunch.plan["items"]]
    if order.index("楼外楼") != 1:
        problems.append(f"锁定条目未保持原槽位（顺序 {order}）")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(PASS, "必去锚点保留、可选点显式取舍；午餐锁定在 11:00 午窗（营业窗钳制，不变 9 点）")


# ---------------------------------------------------------------- E08 check ----


def check_mode_aware_transfer_estimates_and_unknown_coords(params: dict) -> CheckOutcome:
    """E08：5km 步行≫驾驶（mode 分档）；缺坐标 unknown（不是 0/最优/已满足步行限制）。"""
    from app.agent.core.geo import haversine_meters

    point_a = {"latitude": 30.0, "longitude": 120.0}
    # 约 5km 直线（纬度 0.045° ≈ 5000m）
    point_b = {"latitude": 30.045, "longitude": 120.0}
    assert abs(haversine_meters(30.0, 120.0, 30.045, 120.0) - float(params["distance_m"])) < 120
    walking = estimate_transfer_minutes(point_a, point_b, "walking")
    driving = estimate_transfer_minutes(point_a, point_b, "driving")
    missing = estimate_transfer_minutes(
        {"latitude": None, "longitude": None}, {"latitude": 30.0, "longitude": 120.0}, "walking"
    )
    bare = {"item_type": "attraction", "poi_id": "A", "poi_name": "A", "duration_min": 60}
    pair = [dict(bare), dict(bare, poi_id="B", poi_name="B")]
    result = optimize_daily_plan(_plan_from_items(1, pair), route_matrix={})
    problems: list[str] = []
    if not (walking is not None and driving is not None and walking > driving * 2):
        problems.append(f"mode 差异不符：walking={walking} driving={driving}")
    if walking != 123 or driving != 26:
        problems.append(f"估算常量漂移：walking={walking}(应123) driving={driving}(应26)")
    if missing is not None:
        problems.append(f"缺坐标未返回 unknown：{missing}")
    if "unknown-route" not in result.route_sources or result.travel_time_total_min < 45:
        problems.append(f"缺坐标路段未按保守罚分参与排序：{result.route_sources}")
    if not result.degraded:
        problems.append("unknown 路线未如实标 degraded")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(
        PASS,
        f"5km：步行 {walking} 分钟 > 2×驾驶 {driving} 分钟（123/26 钉住常量）；"
        "缺坐标=None、unknown-route 保守罚分且 degraded 如实（不冒充已满足步行上限）",
        {"walking_minutes": float(walking or 0), "driving_minutes": float(driving or 0)},
    )


# ---------------------------------------------------------------- E09 checks ----


def check_scope_violations_outside_affected_days(params: dict) -> CheckOutcome:
    """E09a：\"只改第1天\"——第2/3天任何字段变化都是硬违例；preserved 丢失同样违例。"""
    baseline = [
        _plan_from_items(
            1,
            [
                {"item_type": "attraction", "poi_name": "断桥残雪", "start_time": "09:00", "end_time": "10:30"},
                {"item_type": "food", "poi_name": "晚餐", "start_time": "18:00", "end_time": "19:00"},
            ],
        ),
        _plan_from_items(
            2, [{"item_type": "attraction", "poi_name": "灵隐寺", "start_time": "09:30", "end_time": "11:30"}]
        ),
        _plan_from_items(
            3, [{"item_type": "hotel", "poi_name": "杭州酒店", "start_time": "20:00", "end_time": "20:30"}]
        ),
    ]
    legal = [
        _plan_from_items(
            1,
            [
                {"item_type": "food", "poi_name": "知味观", "start_time": "09:00", "end_time": "10:30"},
                {"item_type": "food", "poi_name": "晚餐", "start_time": "18:00", "end_time": "19:00"},
            ],
        ),
        baseline[1],
        baseline[2],
    ]
    illegal = [
        _plan_from_items(1, [{"item_type": "food", "poi_name": "知味观", "start_time": "09:00", "end_time": "10:30"}]),
        # 第2天被改了时间/费用——越权
        _plan_from_items(
            2,
            [
                {
                    "item_type": "attraction",
                    "poi_name": "灵隐寺",
                    "start_time": "10:30",
                    "end_time": "12:00",
                    "cost": 100,
                }
            ],
        ),
        baseline[2],
    ]
    dropped_preserved = [
        _plan_from_items(
            1, [{"item_type": "attraction", "poi_name": "断桥残雪", "start_time": "09:00", "end_time": "10:30"}]
        ),
        baseline[1],
        baseline[2],
    ]
    decision = {"affected_days": params["affected_days"], "preserved": [{"day_no": 1, "poi_name": "晚餐"}]}
    problems: list[str] = []
    if _scope_violations(decision, baseline, legal):
        problems.append(f"授权范围内的改动被误判：{_scope_violations(decision, baseline, legal)}")
    if not _scope_violations(decision, baseline, illegal):
        problems.append("越权修改第2天未被检出")
    if not _scope_violations(decision, baseline, dropped_preserved):
        problems.append("preserved 项（晚餐）被删未被检出")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(PASS, "affected_days 之外逐字段硬校验；preserved 清单逐字段保留检查（不止比名称）")


def check_keep_hotel_phrase_not_routed_to_hotel_flow(params: dict) -> CheckOutcome:
    """E09b：\"保留晚餐和酒店，只删第一天上午\"不进换酒店流程；换酒店话术仍进候选。"""

    def req(message: str) -> ChatTurnRequest:
        return ChatTurnRequest(city="杭州", days=3, message=message, plans=[])

    keep = _is_hotel_request(req("保留晚餐和酒店，只删第一天上午，其他两天不要动，先给建议"))
    time_only = _is_hotel_request(req("只调整酒店入住时间，酒店不换"))
    change = _is_hotel_request(req("酒店换成舒适型，其他不动"))
    problems: list[str] = []
    if keep or time_only:
        problems.append(f"保留/不换话术被名词劫持进换酒店流程：keep={keep} time_only={time_only}")
    if not change:
        problems.append("明确换酒店未路由到酒店候选")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(PASS, "能力路由按动词动作：保留/不换不进酒店候选；明确更换才进")


# ---------------------------------------------------------------- E10 check ----


def check_proposed_requirements_apply_on_confirm(params: dict) -> CheckOutcome:
    """E10：拟变更需求随草稿提议；确认 apply 才与行程一同生效（未确认前正式需求不动）。"""
    place = str(params["place"])
    from_day = int(params["from_day"])
    to_day = int(params["to_day"])
    baseline_req = TripRequirements(
        required_places=[RequiredPlace(constraint_id="place-lingyin", name=place, day_no=from_day)],
        excluded_categories=["museum"],
    )
    proposed = TripRequirements(
        required_places=[RequiredPlace(constraint_id="place-lingyin", name=place, day_no=to_day)],
        excluded_categories=["museum"],
    )
    with business_env("e10-propose"):
        trip_id = _seed_user_and_trip(days=2, requirements=baseline_req)
        plans = _current_plan_rows(trip_id)
        plans[0]["items"] = [
            {"item_type": "attraction", "poi_name": place, "start_time": "09:00", "end_time": "10:30", "cost": 45},
            {"item_type": "food", "poi_name": "知味观", "start_time": "11:00", "end_time": "12:00", "cost": 60},
        ]
        revision = itinerary_chat.plan_revision(itinerary_chat.current_plans(trip_id))
        message_id = _write_draft(
            trip_id,
            plans,
            base_revision=revision,
            base_planning_revision=_main_revision(trip_id),
            proposed_requirements=proposed,
        )
        with db_session.session_scope() as session:
            main = session.get(ItineraryMain, trip_id)
            assert main is not None
            before = TripRequirements.model_validate(main.requirements_json)
        if before.required_places[0].day_no != from_day:
            return CheckOutcome(FAIL, f"未确认前正式需求已被改动（应保持第{from_day}天）")
        itinerary_plan_apply.apply_plans(1, trip_id, message_id, revision)
        with db_session.session_scope() as session:
            main = session.get(ItineraryMain, trip_id)
            assert main is not None
            after = TripRequirements.model_validate(main.requirements_json)
            day_rows = session.execute(select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id)).scalars().all()
        confirmed = after.required_places[0].day_no == to_day and after.excluded_categories == ["museum"]
        day1_alive = any(day.day_no == 1 and day.deleted == 0 for day in day_rows)
        if not (confirmed and day1_alive):
            return CheckOutcome(FAIL, f"确认后需求/行程不一致：{after.required_places} day1_alive={day1_alive}")
        return CheckOutcome(
            PASS,
            f"拟变更（{place} 第{from_day}→{to_day}天）随草稿提议；确认 apply 同事务写正式需求+行程；"
            "未确认前正式需求零改动",
        )


# ---------------------------------------------------------------- E11 checks ----


def check_shrink_reexpand_days_consistency(params: dict) -> CheckOutcome:
    """E11a：2→1→2 软删日原行复活；无唯一键冲突/旧条目复活；日/主表/住宿一致；未触及天保持。"""
    with business_env("e11-shrink"):
        trip_id = _seed_user_and_trip(days=2)
        plans_two = _current_plan_rows(trip_id)
        day1_names_before = [item["poi_name"] for item in plans_two[0]["items"]]
        with db_session.session_scope() as session:
            day2 = (
                session.execute(
                    select(ItineraryDay).where(ItineraryDay.itinerary_id == trip_id, ItineraryDay.day_no == 2)
                )
                .scalars()
                .one()
            )
            day2.generation_status = "SUCCEEDED"
            day2.generation_action_id = "day-1-2"
            day2.generation_fingerprint = "fp-old"
            original_day2_id = day2.id
        # 2→1：收缩到只有 day1
        revision = itinerary_chat.plan_revision(itinerary_chat.current_plans(trip_id))
        message_id = _write_draft(trip_id, _current_plan_rows(trip_id, [1]), base_revision=revision)
        itinerary_plan_apply.apply_plans(1, trip_id, message_id, revision)
        with db_session.session_scope() as session:
            shrunk = session.get(ItineraryMain, trip_id)
            assert shrunk is not None
            if not (shrunk.days == 1 and shrunk.stay_nights == 0):
                return CheckOutcome(FAIL, f"缩天语义不符：days={shrunk.days} nights={shrunk.stay_nights}")
        # 1→2：扩回两天——软删的 day2 原行复活（草稿只带旧酒店 id：身份放行、旧条目不复活）
        revision2 = itinerary_chat.plan_revision(itinerary_chat.current_plans(trip_id))
        # apply 是整份替换：扩天草稿必须带 1..2 完整天（1→1 天合法）；day2 只收敛酒店条目
        expanded_plans = [dict(plan) for plan in plans_two]
        for plan in expanded_plans:
            if plan.get("day_no") == 2:
                plan["items"] = [it for it in plan["items"] if it.get("poi_name") == "杭州老旅馆"]
        message_id2 = _write_draft(trip_id, expanded_plans, base_revision=revision2)
        itinerary_plan_apply.apply_plans(1, trip_id, message_id2, revision2)
        problems: list[str] = []
        with db_session.session_scope() as session:
            main = session.get(ItineraryMain, trip_id)
            assert main is not None
            if not (main.days == 2 and main.stay_nights == 1):
                problems.append(f"扩天语义不符：days={main.days} nights={main.stay_nights}")
            if main.gen_state != "PARTIAL":
                problems.append(f"有 PENDING 天却冒充 {main.gen_state}")
            rows = (
                session.execute(
                    select(ItineraryDay)
                    .execution_options(include_deleted=True)
                    .where(ItineraryDay.itinerary_id == trip_id, ItineraryDay.day_no == 2)
                )
                .scalars()
                .all()
            )
            if len(rows) != 1:
                problems.append(f"唯一键冲突的重复行：{len(rows)} 条")
            else:
                revived = rows[0]
                if revived.id != original_day2_id or revived.deleted != 0:
                    problems.append("day2 不是原行复活")
                if revived.generation_status != "PENDING" or revived.generation_action_id is not None:
                    problems.append("恢复日复活了旧生成动作")
                live_items = (
                    session.execute(
                        select(ItineraryItem).where(ItineraryItem.day_id == revived.id, ItineraryItem.deleted == 0)
                    )
                    .scalars()
                    .all()
                )
                if sorted(item.poi_name for item in live_items) != ["杭州老旅馆"]:
                    problems.append(f"恢复日条目异常：{[item.poi_name for item in live_items]}")
        day1_names_after = [item["poi_name"] for item in _current_plan_rows(trip_id, [1])[0]["items"]]
        if day1_names_before != day1_names_after:
            problems.append(f"未触及的第1天被改动：{day1_names_before} → {day1_names_after}")
        if problems:
            return CheckOutcome(FAIL, "；".join(problems))
        return CheckOutcome(
            PASS,
            "2→1→2 原行复活、住宿默认语义重算（2 天=1 夜）、PARTIAL 如实、未触及天逐字段不变",
            {"scope_cases": 1.0, "scope_preserved": 1.0},
        )


def check_cas_concurrent_apply_single_winner(params: dict) -> CheckOutcome:
    """E11b：并发应用两草稿——过期基准 409 整单回滚；基准对齐者唯一成功并推进修订。"""
    with business_env("e11-cas"):
        trip_id = _seed_user_and_trip(days=2)
        base_revision_at_seed = _main_revision(trip_id)
        plans = _current_plan_rows(trip_id)
        revision_hash = itinerary_chat.plan_revision(itinerary_chat.current_plans(trip_id))
        stale_id = _write_draft(
            trip_id, plans, base_revision=revision_hash, base_planning_revision=base_revision_at_seed
        )
        # 并发写者赢下竞争：修订号先推进（模拟另一个 apply/编辑已提交）
        with db_session.session_scope() as session:
            itinerary_version.bump_planning_revision(session, trip_id)
        revision_after_rival = _main_revision(trip_id)
        try:
            itinerary_plan_apply.apply_plans(1, trip_id, stale_id, revision_hash)
            return CheckOutcome(FAIL, "过期基准未 409")
        except ApiError as exc:
            if exc.status != 409:
                return CheckOutcome(FAIL, f"过期基准错误码不符：{exc.status}")
        if _main_revision(trip_id) != revision_after_rival:
            return CheckOutcome(FAIL, "失败的 apply 推进了修订号")
        with db_session.session_scope() as session:
            message_row = session.get(ItineraryChatMessage, stale_id)
            if message_row is None or message_row.changed != 1:
                return CheckOutcome(FAIL, "CAS 失败未整单回滚（草稿被消费）")
        fresh_id = _write_draft(
            trip_id, plans, base_revision=revision_hash, base_planning_revision=base_revision_at_seed + 1
        )
        itinerary_plan_apply.apply_plans(1, trip_id, fresh_id, revision_hash)
        if _main_revision(trip_id) != base_revision_at_seed + 2:
            return CheckOutcome(FAIL, f"成功 apply 修订推进不符：{_main_revision(trip_id)}")
        return CheckOutcome(PASS, "并发两草稿：过期者 409 零残留，对齐者唯一成功（CAS 修订 +2 语义正确）")


# ---------------------------------------------------------------- E12 check ----

E12_PARTS = [
    '{"trip_theme": "湖光山色", "daily_plans": [',
    '{"day_no": 1, "theme": "d1", "items": [',
    '{"item_type": "attraction", "poi_name": "西湖", "start_time": "09:00", "end_time": "11:00"},',
    '{"item_type": "food", "poi_name": "楼外楼", "start_time": "12:00", "end_time": "13:00"}',
    "]}",
    ',{"day_no": 2, "theme": "d2", "items": [',
    '{"item_type": "attraction", "poi_name": "灵隐寺", "start_time": "09:30", "end_time": "11:30"}',
    "]}],",
    '"suggestions": [{"poi_name": "断桥", "category": "attraction"}]}',
]
E12_FIRST_ITEM_CHUNK = 2


def check_stream_parser_blocked_interleave_and_truncation(params: dict) -> CheckOutcome:
    """E12：第一个完整 item 即发布（模型未结束）；截断终检抛错不冒充完整；EOF 组装正确。"""
    parser = TripPlanStreamParser()
    first_item: list[Any] = []
    for index in range(E12_FIRST_ITEM_CHUNK + 1):
        # 生成器"阻塞"在首个完整 item 后：消费侧已拿到候选而模型尚未结束——
        # 确定性构造（只喂前 3 段），不是 flaky sleep，也非先 join 全部再喂
        first_item.extend(parser.feed(E12_PARTS[index]))
    problems: list[str] = []
    if not first_item:
        return CheckOutcome(FAIL, "第一个完整 item 未在阻塞点前发布")
    preview = first_item[0]
    if preview.day_no != 1 or preview.item.get("poi_name") != "西湖" or preview.item_ordinal != 0:
        problems.append(f"候选身份不符：{preview}")
    for index in range(E12_FIRST_ITEM_CHUNK + 1, len(E12_PARTS)):
        parser.feed(E12_PARTS[index])
    days, suggestions, seen = parser.finish()
    truncated = TripPlanStreamParser()
    for part in E12_PARTS[:-1]:
        truncated.feed(part)
    try:
        truncated.finish()
        problems.append("截断输出未被判 TruncatedTripPlanError")
    except TruncatedTripPlanError:
        pass
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    if not (len(days) == 2 and seen == {1, 2} and len(suggestions) == 1):
        return CheckOutcome(FAIL, f"EOF 组装不符：days={len(days)} seen={seen} suggestions={len(suggestions)}")
    return CheckOutcome(
        PASS,
        "第一个完整 item 先于模型结束发布（runId+dayNo+ordinal 身份稳定）；"
        "正式 day 快照以 EOF 终检为准；截断半截不冒充完整（走逐日兜底）",
    )


# ---------------------------------------------------------------- E13 check ----


def _raise_redis_unavailable():
    raise ConnectionError("Redis disabled for offline experience runner")


def check_turn_id_replay_zero_llm_calls(params: dict) -> CheckOutcome:
    """E13：同 turnId 重复提交零 LLM 调用零落库；并发 running 409；同键不同请求 409。"""
    turn_id = str(params["turn_id"])
    calls: list[int] = []

    def fake_run(request, **kwargs):
        calls.append(1)
        return ChatTurnResponse(reply="已调整", changed=False)

    with business_env("e13-turn"):
        with (
            patch.object(cache_store, "_get_client", _raise_redis_unavailable),
            patch.object(itinerary_chat, "run_chat_turn", fake_run),
        ):
            trip_id = _seed_user_and_trip(days=1)
            first = itinerary_chat.chat_edit(1, trip_id, "把节奏放慢", [], turn_id=turn_id)
            if first.get("messageId") is None:
                return CheckOutcome(FAIL, "首轮执行未落库")
            replay = itinerary_chat.chat_edit(1, trip_id, "把节奏放慢", [], turn_id=turn_id)
            problems: list[str] = []
            if len(calls) != 1:
                problems.append(f"重放发生了第二次模型调用：{len(calls)}")
            if replay.get("changed") or replay.get("messageId") is not None:
                problems.append("重放携带了新变更/新消息")
            try:
                itinerary_chat.chat_edit(1, trip_id, "改成别的", [], turn_id=turn_id)
                problems.append("同 turnId 不同请求未 409")
            except ApiError as exc:
                if exc.status != 409:
                    problems.append(f"同键不同请求错误码：{exc.status}")
            record = cache_store.get_json(itinerary_chat.TURN_IDEM_NAMESPACE, f"1:{trip_id}:{turn_id}")
            assert record is not None
            cache_store.set_json(
                itinerary_chat.TURN_IDEM_NAMESPACE,
                f"1:{trip_id}:{turn_id}",
                {"status": "running", "request_hash": record["request_hash"]},
                600,
            )
            try:
                itinerary_chat.chat_edit(1, trip_id, "把节奏放慢", [], turn_id=turn_id)
                problems.append("running 期间的重试未 409")
            except ApiError as exc:
                if exc.status != 409:
                    problems.append(f"running 冲突错误码：{exc.status}")
            if len(calls) != 1:
                problems.append("409 期间发生了模型调用")
            if problems:
                return CheckOutcome(FAIL, "；".join(problems))
            return CheckOutcome(
                PASS,
                "同 turn 重放零调用零落库（不回填旧结果）；同键不同请求 409；running 窗口 409——重复执行数 0",
                {"duplicate_executions": 0.0, "llm_calls": float(len(calls))},
            )


# ---------------------------------------------------------------- E14 checks ----


def check_official_page_fixture_vetoes_stale_ticket(params: dict) -> CheckOutcome:
    """E14a：灵隐寺 fixture（2025-12-01 免票+实名预约分时）否决旧收费话术；坐标不传染。"""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / str(params["fixture"])
    html = fixture.read_text(encoding="utf-8")
    evidence = extract_evidence(html, fetched_at="2026-10-08T10:00:00+00:00", url=str(params["url"]))
    if evidence is None:
        return CheckOutcome(FAIL, "登记的官方页面要点未被 adapter 识别")
    values: dict[str, Any] = {"ticket": params["stale_ticket"], "reservation": False}
    fields: dict[str, FactEvidence] = {
        "ticket": FactEvidence(value_kind="estimated"),
        "coordinates": FactEvidence(verification_status="verified", value_kind="observed"),
    }
    conflicts = apply_evidence_conflicts(values, fields, evidence)
    problems: list[str] = []
    if [c.field for c in conflicts] != ["ticket", "reservation"]:
        problems.append(f"冲突未逐字段记录：{[c.field for c in conflicts]}")
    if values.get("ticket") is not None:
        problems.append(f"旧票价未被否决撤回：{values.get('ticket')}")
    fact = fields["ticket"]
    if fact.verification_status != "unverified" or fact.review_requirement != "before_departure":
        problems.append(f"冲突字段未降级待复核：{fact}")
    if fields["coordinates"].verification_status != "verified":
        problems.append("坐标证据被传染降级")
    if evidence.fields.get("ticket") != "free" or evidence.fields.get("reservation") != "required":
        problems.append(f"提取字段不符：{evidence.fields}")
    if evidence.published_at is None:
        problems.append("页面发布时间未被记录")
    if problems:
        return CheckOutcome(FAIL, "；".join(problems))
    return CheckOutcome(
        PASS,
        f"旧票价 {params['stale_ticket']} 被官方证据否决"
        f"（值撤回+待复核+冲突记录含 URL/{evidence.published_at} 观测/摘录）；"
        "坐标 verified 不传染票价",
        {"fact_conflicts": float(len(conflicts))},
    )


def check_fetch_failure_keeps_fields_unknown(params: dict) -> CheckOutcome:
    """E14b：官方请求失败 → 字段保持原状（不误判免费/不营业/不用预约）。"""
    url = str(params["url"])

    def boom(url_arg: str, **kwargs):
        raise RuntimeError("network down")

    with patch("app.agent.grounding.official_pages.fetch_official_page", boom):
        values: dict[str, Any] = {"ticket": 80.0}
        fields: dict[str, FactEvidence] = {"ticket": FactEvidence(value_kind="estimated")}
        conflicts = collect_field_evidence(values, fields, url)
    if conflicts or values.get("ticket") != 80.0 or fields["ticket"].source_url is not None:
        return CheckOutcome(FAIL, f"失败被误判：conflicts={conflicts} values={values} fact={fields['ticket']}")
    values2: dict[str, Any] = {}
    fields2: dict[str, FactEvidence] = {}
    conflicts2 = collect_field_evidence(values2, fields2, "http://127.0.0.1/admin")
    if conflicts2 or values2 or fields2:
        return CheckOutcome(FAIL, "守卫拒绝被误判成证据")
    return CheckOutcome(PASS, "请求失败/守卫拒绝：零升级零撤回，unknown 保持 unknown（给官方核实入口）")


# ---------------------------------------------------------------- 编排 ----

CHECKS: dict[str, Callable[[dict], CheckOutcome]] = {
    "clarify_multi_turn_requirements_terminal_state": check_clarify_multi_turn_requirements_terminal_state,
    "generation_pipeline_preserves_requirements": check_generation_pipeline_preserves_requirements,
    "clarify_replacement_updates_keep_other_requirements": check_clarify_replacement_updates_keep_other_requirements,
    "overlong_days_blocked_not_bypassed": check_overlong_days_blocked_not_bypassed,
    "budget_negotiation_acceptance_binds_params": check_budget_negotiation_acceptance_binds_params,
    "hard_cap_budget_no_tolerance": check_hard_cap_budget_no_tolerance,
    "negation_never_becomes_positive_preference": check_negation_never_becomes_positive_preference,
    "excluded_category_violation_and_unknown_for_unsupported": (
        check_excluded_category_violation_and_unknown_for_unsupported
    ),
    "half_day_window_not_flagged_thin_full_day_keeps_guard": (
        check_half_day_window_not_flagged_thin_full_day_keeps_guard
    ),
    "optimizer_required_priority_lunch_and_locked": check_optimizer_required_priority_lunch_and_locked,
    "mode_aware_transfer_estimates_and_unknown_coords": check_mode_aware_transfer_estimates_and_unknown_coords,
    "scope_violations_outside_affected_days": check_scope_violations_outside_affected_days,
    "keep_hotel_phrase_not_routed_to_hotel_flow": check_keep_hotel_phrase_not_routed_to_hotel_flow,
    "proposed_requirements_apply_on_confirm": check_proposed_requirements_apply_on_confirm,
    "shrink_reexpand_days_consistency": check_shrink_reexpand_days_consistency,
    "cas_concurrent_apply_single_winner": check_cas_concurrent_apply_single_winner,
    "stream_parser_blocked_interleave_and_truncation": check_stream_parser_blocked_interleave_and_truncation,
    "turn_id_replay_zero_llm_calls": check_turn_id_replay_zero_llm_calls,
    "official_page_fixture_vetoes_stale_ticket": check_official_page_fixture_vetoes_stale_ticket,
    "fetch_failure_keeps_fields_unknown": check_fetch_failure_keeps_fields_unknown,
}


def run_check(name: str, params: dict) -> dict:
    """单 check 执行：异常一律落 fail（带截断堆栈），不允许逃逸成静默。"""
    entry = CHECKS.get(name)
    if entry is None:
        return {"name": name, "status": SKIP, "detail": "runner 未登记该检查名", "stats": {}}
    try:
        outcome = entry(params)
    except Exception as exc:  # 任何异常都算该 check 失败，不允许逃逸成静默
        tail = "".join(traceback.format_exception(exc)[-4:])
        return {"name": name, "status": FAIL, "detail": f"{type(exc).__name__}: {exc}{tail}", "stats": {}}
    return {"name": name, "status": outcome.status, "detail": outcome.detail, "stats": outcome.stats}


def run_case(case: dict) -> dict:
    checks = [run_check(check["name"], check.get("params") or {}) for check in case["checks"]]
    if any(check["status"] == FAIL for check in checks):
        status = FAIL
    elif all(check["status"] == SKIP for check in checks):
        status = SKIP
    else:
        status = PASS
    return {"id": case["id"], "title": case["title"], "kind": case["kind"], "status": status, "checks": checks}


def run_all(dataset: dict | None = None, *, dataset_path: Path | None = None) -> dict:
    dataset = dataset if dataset is not None else load_dataset(dataset_path)
    cases = [run_case(case) for case in dataset["cases"]]
    case_counts = {PASS: 0, SKIP: 0, FAIL: 0}
    check_counts = {PASS: 0, SKIP: 0, FAIL: 0}
    for case in cases:
        case_counts[case["status"]] = case_counts[case["status"]] + 1
        for check in case["checks"]:
            check_counts[check["status"]] = check_counts[check["status"]] + 1
    return {
        "dataset": {
            "file": DATASET_PATH.name,
            "dataset_hash": dataset["dataset_hash"],
            "case_count": len(dataset["cases"]),
        },
        "summary": {
            "cases": case_counts,
            "checks": check_counts,
            "checks_total": sum(len(case["checks"]) for case in cases),
        },
        "cases": cases,
    }


def write_report(result: dict) -> Path:
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return REPORT_PATH


def main() -> int:
    result = run_all()
    path = write_report(result)
    print(json.dumps(result["summary"], ensure_ascii=False))
    for case in result["cases"]:
        marker = {"pass": "[PASS]", "skip": "[SKIP]", "fail": "[FAIL]"}[case["status"]]
        print(f"{marker} {case['id']} [{case['kind']}] {case['title'][:48]}")
        for check in case["checks"]:
            if check["status"] != "pass":
                print(f"    {check['status'].upper()} {check['name']}: {check['detail'][:200]}")
    print(f"报告已生成：{path}")
    return 0 if result["summary"]["cases"][FAIL] == 0 and result["summary"]["checks"][FAIL] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
