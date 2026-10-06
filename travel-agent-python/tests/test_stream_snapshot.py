"""P2 安全网（G-2.0）：trip / day 两条链路的端到端事件序列快照。

为什么需要它：PR-4 把旁路 trip_stream 收编进统一图（mode=stream 分支，custom
stream 逐天 yield）。这次「等积搬家 + 归一」最容易犯的错不是崩溃，而是**事件序列
悄悄变样**——少一天的 day_patch、多一条被去重拦下的重复项、done 的 daysEmitted
顺序变化。离线单测断言的是单点字段，抓不到这种漂移；本快照逐字节比对完整序列。

覆盖范围（两条链路各一份 golden）：
- trip：`run_generate_trip_stream`（统一图 mode=stream）产出的完整 NDJSON 事件
  列表（day / day_patch / suggestions / done），含**酒店摊铺触发的 day_patch 粒度**；
- day：单日链路的 observable 序列——`run_generate_day` 的结果 DailyPlan
  wire 形状 + 该次运行的 trace 事件序列（节点/工具/决策顺序）。

有意不覆盖：业务侧 SSE 信封（`app/services/generation_events.py`）是薄封装，
其键名口径由 `tests/api` 活栈契约与 `stream_events` schema 兜底；手术刀落点是
上面两条 agent 侧链路。

确定性来源：LLM 输出取自 `tests/agent_eval/mock_llm.py` 的 fixture（trip 走
`open_plans.llm_open_trip` 替身、day 走 `llm_open_day` 替身），检索/研究/补池
全部 mock，联网关闭；trace 中的 run_id/request_id 在本测试固定，duration/span/
uuid 类字段在归一化时剔除。PR-4 后增量解析已不存在（手写状态机退役），旧的
64 字符切片夹具随之退役——逐天产出由生成期挂点（open_plans.on_day）保证。

重生成基线（**必须人工复核 diff**）：
    GOLDEN_REGENERATE=1 uv run pytest tests/test_stream_snapshot.py
"""

from __future__ import annotations

import json
import os
import pathlib
from contextlib import ExitStack
from unittest.mock import patch

from app.agent.generation.content import landing
from app.agent.generation.orchestration import day_stream, open_plans, stream_branch
from app.agent.research import reasoning
from app.agent.runtime.trace import trace_run
from app.agent.tools import impl as tools
from app.common.config import settings
from app.schemas.trip import GenerateDayRequest
from tests.agent_eval import mock_llm

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"
TRIP_GOLDEN = GOLDEN_DIR / "stream_trip_events.json"
DAY_GOLDEN = GOLDEN_DIR / "stream_day_events.json"

_REGENERATE = os.getenv("GOLDEN_REGENERATE") == "1"

CITY = "巴塞罗那"
DAYS = 3
# 流式切片宽度：模拟真实 LLM 的增量输出（64 字符边界会切在 JSON 结构中间，
# 顺带覆盖解析器的跨块拼装）；固定值保证事件序列可复现。
CHUNK = 64


class _FixtureLLMClient:
    """确定性 LLM 桩：把 fixture 行程 JSON 按固定宽度切片流式吐出。"""

    def __init__(self, payload: str, chunk: int = CHUNK) -> None:
        self._chunks = [payload[i : i + chunk] for i in range(0, len(payload), chunk)] or [""]

    def stream_chat_deltas(self, messages, **kwargs):  # 跟随 llm_client 签名
        yield from self._chunks


def _forbid_network_call(*args, **kwargs):
    raise AssertionError("离线快照不得发起实时价/联网查询")


def _fixture_context(city: str) -> dict:
    data = mock_llm.catalog(city)
    return {"candidates": data["attractions"], "foods": data["foods"], "hotels": data["hotels"]}


def _patch_fixture_stack(stack, city: str) -> None:
    """把全链路外部依赖换成 fixture：检索、研究、补池、落点、图片。

    **实时价必须显式关闭**：`live_price_search`/`live_food_price_search` 默认
    开启，链路会真的去打 LLM 网关问"当前挂牌价"（本机实测 401 被内部吞掉降级）。
    不关的话这套快照会依赖外网、且成本值随环境凭据漂移——快照基线就失去意义。
    """
    data = mock_llm.catalog(city)
    stack.enter_context(patch.object(tools, "search_attractions", mock_llm.search_attractions))
    stack.enter_context(patch.object(tools, "search_foods", mock_llm.search_foods))
    stack.enter_context(patch.object(tools, "search_hotels", mock_llm.search_hotels))
    stack.enter_context(patch.object(tools, "get_consumption", mock_llm.get_consumption))
    stack.enter_context(patch.object(tools, "search_local_poi", mock_llm.search_local_poi))
    stack.enter_context(patch.object(tools, "attach_poi_images", mock_llm.attach_poi_images))
    stack.enter_context(patch.object(reasoning, "plan_research", mock_llm.plan_research))
    stack.enter_context(patch.object(reasoning, "evaluate_research", mock_llm.evaluate_research))
    # 联网与实时价：全部关闭，保证序列与成本只由 fixture 决定（离线确定性）
    stack.enter_context(patch.object(settings, "llm_api_key", "snapshot-fixture"))
    stack.enter_context(patch.object(settings, "llm_generation_web_search", False))
    stack.enter_context(patch.object(settings, "web_search_enabled", False))
    stack.enter_context(patch.object(settings, "live_price_search", False))
    stack.enter_context(patch.object(settings, "live_food_price_search", False))
    # 硬护栏：开关关掉之外，再把实时价函数换成"一旦被调用就报错"。
    # 只关开关是"约定离线"，这条是"证明离线"——将来谁在链路里新加一个
    # 绕过开关的实时价调用，这里立刻炸而不是变成环境相关的漂移快照。
    stack.enter_context(patch("app.agent.generation.output.prices.query_live_price", _forbid_network_call))
    stack.enter_context(patch("app.agent.generation.output.prices.query_live_food_price", _forbid_network_call))
    stack.enter_context(patch.object(stream_branch, "fill_suggestion_gaps", lambda rows, city, **kw: rows))
    stack.enter_context(patch.object(landing, "local_ground", lambda item, city: None))
    assert data  # 保持 fixture 数据被显式构造（城市名参与生成，非法城市会在此暴露）


def _trip_plans(city: str, days: int) -> tuple[list[dict], list[dict]]:
    """从 mock_llm 的单日 fixture 展开成 trip 生成入口（llm_open_trip 替身）的输出。

    有意只让第 1 天带酒店（第 2 天起剥掉）：这正是 day_patch 存在的场景——
    LLM 漏排某晚入住时由 spread_hotels 确定性补齐，只对补了酒店的天发 patch。
    若每天都有酒店，链路不会产出 day_patch，快照就丢了该粒度覆盖。
    """
    req = GenerateDayRequest(city=city, days=days, day_no=1, needs_hotel=True, context=_fixture_context(city))
    plans, suggestions = mock_llm.fixture_open_trip(req)
    for plan in plans:
        if int(plan.get("day_no") or 0) > 1:
            plan["items"] = [it for it in plan.get("items") or [] if it.get("item_type") != "hotel"]
    # 注：生产的 llm_open_trip 会把顶层 trip_theme 注入每一天的 plan（第 1 天为权威），
    # done.tripTheme 因此有值；本替身不模拟那一步注入，golden 的 done.tripTheme 为
    # null 是**替身形状**而非链路差异（PR-4 重认账的唯一 diff 即此一行，生产等价）。
    return plans, suggestions


def _trip_stream_events() -> list[dict]:
    """跑一次整段流式生成（统一图 mode=stream 分支），返回完整 NDJSON 事件列表。"""
    plans, suggestions = _trip_plans(CITY, DAYS)
    req = GenerateDayRequest(
        city=CITY,
        persons=2,
        days=DAYS,
        day_no=1,
        needs_hotel=True,
        hotel_tier="豪华型",
        context=_fixture_context(CITY),
    )
    with ExitStack() as stack:
        _patch_fixture_stack(stack, CITY)
        stack.enter_context(patch.object(open_plans, "llm_open_trip", lambda req: (plans, suggestions)))
        return [dict(event) for event in stream_branch.run_generate_trip_stream(req)]


def _normalize_trace(events: list[dict]) -> list[dict]:
    """把 trace 的非确定 id 归一为稳定序号，保留 kind/name/status/metadata 口径。

    span_id/parent_span_id/tool_call_id 是每次运行新生成的 uuid：直接剔除会
    丢掉"哪个事件挂在哪个 span 下"的结构信息，所以按**首现顺序**重写成
    span-1/span-2…（父 span 先于子 span 出现，顺序本身也是被测事实）。
    duration_ms 是耗时，与结构无关，直接剔除。
    """
    counters: dict[str, dict[str, str]] = {"span": {}, "tool": {}}

    def stable(prefix: str, value: object) -> str:
        key = str(value)
        table = counters[prefix]
        if key not in table:
            table[key] = f"{prefix}-{len(table) + 1}"
        return table[key]

    out: list[dict] = []
    for event in events:
        row = dict(event)
        row.pop("duration_ms", None)
        if row.get("span_id"):
            row["span_id"] = stable("span", row["span_id"])
        if row.get("parent_span_id"):
            row["parent_span_id"] = stable("span", row["parent_span_id"])
        if row.get("tool_call_id"):
            row["tool_call_id"] = stable("tool", row["tool_call_id"])
        out.append(json.loads(json.dumps(row, ensure_ascii=False, sort_keys=True)))
    return out


def _day_chain_snapshot() -> dict:
    """跑一次单日链路，返回 {plan: wire 形状, trace: 归一化事件序列}。"""
    req = GenerateDayRequest(
        city=CITY,
        persons=2,
        days=None,  # 逐日修复路径不传 days（保留单日反思重试），对齐业务侧发法
        day_no=1,
        needs_hotel=True,
        hotel_tier="豪华型",
        # 不带日期 → season_factor 按 date.today() 算：国庆/春节窗口会把 golden
        # 的酒店估价打上 ×1.8（2026-09-30 实录）——钉死平季日期保确定性。
        start_date="2026-04-15",
        context=_fixture_context(CITY),
    )
    with ExitStack() as stack:
        _patch_fixture_stack(stack, CITY)
        stack.enter_context(patch.object(day_stream, "llm_open_day", mock_llm.fixture_open_day))
        with trace_run("snapshot-day", request_id="snapshot-day") as recorder:
            plan = day_stream.run_generate_day(req)
    return {
        "plan": plan.model_dump(mode="json", by_alias=True),
        "trace": _normalize_trace(recorder.to_dict()["events"]),
    }


def _compare(actual: object, golden_path: pathlib.Path) -> None:
    rendered = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if _REGENERATE:
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(rendered, encoding="utf-8")
        return
    assert golden_path.exists(), f"缺少基线 {golden_path.name}；先跑 GOLDEN_REGENERATE=1 生成并人工复核"
    expected = golden_path.read_text(encoding="utf-8")
    assert rendered == expected, (
        f"{golden_path.name} 与当前事件序列不一致。若为有意改口径，"
        f"跑 GOLDEN_REGENERATE=1 重生成并人工复核 diff；否则这是行为回归。"
    )


def test_trip_stream_event_sequence_matches_golden():
    """整段流式链路：完整 NDJSON 事件序列逐字节一致（含 day_patch 粒度）。"""
    events = _trip_stream_events()
    types = [event["type"] for event in events]
    # 快照必须覆盖 day_patch：酒店摊铺给漏排的晚次补酒店（本 fixture 第 2/3 天触发）
    assert "day_patch" in types, "fixture 未触发 day_patch，快照失去该粒度覆盖"
    assert types[-1] == "done"
    _compare(events, TRIP_GOLDEN)


def test_day_chain_plan_and_trace_match_golden():
    """单日链路：结果 DailyPlan wire 形状 + trace 事件序列一致。"""
    snapshot = _day_chain_snapshot()
    assert snapshot["plan"]["dayNo"] == 1
    assert snapshot["trace"], "单日链路未产出 trace 事件，快照失去意义"
    _compare(snapshot, DAY_GOLDEN)
