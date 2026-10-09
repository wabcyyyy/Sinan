"""M5a 真实逐项候选预览（spec §10）：流式生成器 + 两套事件协议接通。

覆盖（验收口径：受控模型生成器，第一个完整 item 后阻塞后续 chunks——
浏览器/消费层已收到该 item，生成器尚未结束；不允许先 join 全部 chunks 再喂）：
- llm_open_trip_stream：阻塞交错、单字符切片全链（转义/字符串内花括号/中文）、
  截断 → 有界修复（成功与失败两态）、day_no 后置归属；
- open_plans：on_item_preview / on_item_withdrawn 挂点（默认 None = 完全旧行为）；
- stream_branch：item_previews 开关 → day_item_preview / day_item_preview_withdrawn
  wire 事件（模型构造 → dump），既有 day/done 序列不变；
- 业务面：generation_events 预览帧口径（previewId = runId:dayNo:itemOrdinal），
  _plan_whole_trip 消费 agent 事件并转发，KNOWN_STREAM_TYPES 白名单。

不打真网：LLM 全部注入桩，外部解析/联网由 tests/conftest.py 的 autouse 夹具钉死。
"""

from __future__ import annotations

import json
import threading

import pytest
from pydantic import TypeAdapter

from app.agent.core.json_utils import LlmJsonError
from app.agent.generation.content import day_prompts, landing, trip_stream
from app.agent.generation.content.trip_stream import llm_open_trip_stream
from app.agent.generation.orchestration import open_plans, stream_branch
from app.agent.generation.orchestration.stream_branch import run_generate_trip_stream
from app.common.config import settings
from app.schemas.stream_events import (
    ItemPreviewEvent,
    ItemPreviewWithdrawnEvent,
    StreamEvent,
    to_wire,
)
from app.schemas.trip import GenerateDayRequest, GenerateRequest
from app.services import generation_events, itinerary_generation

# ---------------------------------------------------------------- 测试数据 ----

#: 按「片段」拼的整段 JSON：PARTS[2] 末尾恰好是第一个 item（西湖）闭合——
#: 阻塞场景据此把流停在该 item 之后、后续 chunks 之前。
PARTS = [
    '{"trip_theme": "湖光山色", "daily_plans": [',
    '{"day_no": 1, "theme": "d1", "note": "n1", "items": [',
    '{"item_type": "attraction", "poi_name": "西湖", "start_time": "09:00", "end_time": "11:00"},',
    '{"item_type": "food", "poi_name": "楼外楼", "start_time": "12:00", "end_time": "13:00"}',
    "]}",
    ',{"day_no": 2, "theme": "d2", "note": "n2", "items": [',
    '{"item_type": "attraction", "poi_name": "灵隐寺", "start_time": "09:30", "end_time": "11:30"}',
    "]}],",
    '"suggestions": [{"poi_name": "断桥", "city": "杭州", "category": "attraction"}]}',
]
FIRST_ITEM_CHUNK = 2
TRIP_TEXT = "".join(PARTS)


def _trip_payload() -> dict:
    """与 TRIP_TEXT 同构的 dict 版（供修复重试用例返回完整 JSON 文本）。"""
    return {
        "trip_theme": "湖光山色",
        "daily_plans": [
            {
                "day_no": 1,
                "theme": "d1",
                "note": "n1",
                "items": [
                    {"item_type": "attraction", "poi_name": "西湖", "start_time": "09:00", "end_time": "11:00"},
                    {"item_type": "food", "poi_name": "楼外楼", "start_time": "12:00", "end_time": "13:00"},
                ],
            },
            {
                "day_no": 2,
                "theme": "d2",
                "note": "n2",
                "items": [
                    {"item_type": "attraction", "poi_name": "灵隐寺", "start_time": "09:30", "end_time": "11:30"}
                ],
            },
        ],
        "suggestions": [{"poi_name": "断桥", "city": "杭州", "category": "attraction"}],
    }


def _day_req(days: int = 2) -> GenerateDayRequest:
    return GenerateDayRequest(city="杭州", persons=2, days=days, day_no=1, needs_hotel=False)


def _trip_req() -> GenerateRequest:
    return GenerateRequest(city="杭州", days=2, persons=1)


def _stub_offline(monkeypatch) -> None:
    """接地/联网桩：解析不出坐标也不打网（conftest 已钉死各外部源开关）。"""
    monkeypatch.setattr(landing, "local_ground", lambda item, city: None)
    monkeypatch.setattr(stream_branch, "fill_suggestion_gaps", lambda rows, city, **kw: rows)


# ------------------------------------------------------------- 契约形状 ----


class TestPreviewEventContract:
    def test_preview_wire_shape_and_union_validation(self):
        """模型构造 → dump 的 wire 形状，且两事件已入 StreamEvent 判别联合。"""
        adapter = TypeAdapter(StreamEvent)
        wire = to_wire(
            ItemPreviewEvent(type="day_item_preview", run_id="r1", day_no=1, item_ordinal=0, item={"poi_name": "西湖"})
        )
        assert wire == {
            "type": "day_item_preview",
            "runId": "r1",
            "dayNo": 1,
            "itemOrdinal": 0,
            "item": {"poi_name": "西湖"},
            "status": "drafting",
        }
        assert adapter.validate_python(wire).type == "day_item_preview"

        withdrawn = to_wire(
            ItemPreviewWithdrawnEvent(
                type="day_item_preview_withdrawn", run_id="r1", day_no=1, item_ordinal=0, reason="regenerate"
            )
        )
        assert withdrawn == {
            "type": "day_item_preview_withdrawn",
            "runId": "r1",
            "dayNo": 1,
            "itemOrdinal": 0,
            "reason": "regenerate",
        }
        assert adapter.validate_python(withdrawn).type == "day_item_preview_withdrawn"

    def test_known_stream_types_include_preview(self):
        """业务侧白名单放行两类预览事件（前向兼容消费的附加类型）。"""
        assert "day_item_preview" in itinerary_generation.KNOWN_STREAM_TYPES
        assert "day_item_preview_withdrawn" in itinerary_generation.KNOWN_STREAM_TYPES


# ------------------------------------- llm_open_trip_stream（生成器核心） ----


class _FakeLLMClient:
    """stream_chat_deltas 按 chunks 吐增量；chat 仅修复重试路径使用。"""

    def __init__(self, chunks: list[str], repair_text: str | None = None) -> None:
        self._chunks = chunks
        self._repair_text = repair_text
        self.repair_calls = 0
        self.stream_kwargs: dict = {}

    def stream_chat_deltas(self, messages, **kwargs):
        self.stream_kwargs = kwargs
        yield from self._chunks

    def chat(self, *args, **kwargs):
        self.repair_calls += 1
        if self._repair_text is None:
            raise AssertionError("成功路径不应触发修复重试")
        return self._repair_text


def _consume(generator, previews: list, finals: list) -> threading.Event:
    """在子线程里消费生成器（阻塞场景需要真正的交错）。"""
    done = threading.Event()

    def run() -> None:
        try:
            for message in generator:
                if message[0] == "item_preview":
                    previews.append(message)
                elif message[0] == "final":
                    finals.append(message)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return done


class TestLlmOpenTripStream:
    def test_candidate_visible_while_generator_still_running(self, monkeypatch):
        """验收关键场景：第一个完整 item 后流停住——消费方已收到候选，生成器未结束。"""
        proceed = threading.Event()
        first_item_sent = threading.Event()

        class BlockingClient:
            def stream_chat_deltas(self, messages, **kwargs):
                for index, chunk in enumerate(PARTS):
                    yield chunk
                    if index == FIRST_ITEM_CHUNK:
                        first_item_sent.set()
                        # 停住：后续 chunks 等消费方取走候选后才放行。
                        # 若实现是"先 join 全部 chunks 再喂"，这里必然超时。
                        assert proceed.wait(10), "消费方未在候选发布后及时到达：候选未边流边出"

        monkeypatch.setattr(trip_stream, "get_llm_client", lambda: BlockingClient())

        previews: list[tuple] = []
        finals: list[tuple] = []
        done = _consume(llm_open_trip_stream(_day_req()), previews, finals)

        assert first_item_sent.wait(5), "流未到达第一个完整 item"
        assert previews, "生成器尚未结束，消费方就必须已收到第一个候选（spec §10 验收场景）"
        assert not done.is_set(), "候选到达时生成器仍在运行（不能先 join 全部 chunks 再喂）"
        assert not finals, "final 必须晚于流结束"
        assert previews[0][1:3] == (1, 0), "候选身份 = (dayNo=1, itemOrdinal=0)，与 chunk 切割无关"
        assert previews[0][3]["poi_name"] == "西湖"

        proceed.set()
        done.wait(10)
        assert done.is_set()
        assert len(finals) == 1, "EOF + 顶层校验通过后恰好一个 final"
        cleaned, suggestions = finals[0][1], finals[0][2]
        assert [plan["day_no"] for plan in cleaned] == [1, 2]
        assert cleaned[0]["trip_theme"] == "湖光山色", "顶层 trip_theme 注入第 1 天（与 llm_open_trip 同装配段）"
        assert suggestions[0]["poi_name"] == "断桥"

    def test_single_char_chunks_escapes_and_braces_full_chain(self, monkeypatch):
        """单字符切片全链：转义引号、字符串内花括号、中文逐字符过 stream_chat_deltas。"""
        payload = json.dumps(
            {
                "trip_theme": "T",
                "daily_plans": [
                    {
                        "day_no": 1,
                        "theme": "d",
                        "note": "n",
                        "items": [
                            {
                                "item_type": "attraction",
                                "poi_name": '引号"与花括号{}景点',
                                "why_this": '备注 {含} "引号" 与 : 冒号',
                                "start_time": "09:00",
                                "end_time": "11:00",
                            }
                        ],
                    }
                ],
                "suggestions": [],
            },
            ensure_ascii=False,
        )
        client = _FakeLLMClient([payload[i] for i in range(len(payload))])
        monkeypatch.setattr(trip_stream, "get_llm_client", lambda: client)

        previews: list[tuple] = []
        finals: list[tuple] = []
        done = _consume(llm_open_trip_stream(_day_req()), previews, finals)
        done.wait(10)
        assert done.is_set()
        assert client.repair_calls == 0, "合法 JSON 不应触发修复"
        assert [p[3]["poi_name"] for p in previews] == ['引号"与花括号{}景点']
        assert len(finals) == 1 and finals[0][1][0]["items"][0]["why_this"] == '备注 {含} "引号" 与 : 冒号'

    def test_truncated_stream_fails_after_failed_repair(self, monkeypatch):
        """截断 → parser 判 TruncatedTripPlanError → 修复再失败 → 向上抛（不冒充成功）。"""
        truncated = '{"trip_theme": "T", "daily_plans": [{"day_no": 1, "theme"'
        client = _FakeLLMClient([truncated], repair_text="这不是 JSON")
        monkeypatch.setattr(trip_stream, "get_llm_client", lambda: client)

        previews: list[tuple] = []
        finals: list[tuple] = []
        with pytest.raises(LlmJsonError):
            for message in llm_open_trip_stream(_day_req()):
                if message[0] == "item_preview":
                    previews.append(message)
                elif message[0] == "final":
                    finals.append(message)
        assert not previews and not finals, "半截候选不能当成功，也不该发布无归属候选"
        assert client.repair_calls == 1, "截断后恰好一次有界修复重试"

    def test_truncated_stream_recovers_via_bounded_repair(self, monkeypatch):
        """截断 → 一次修复重试返回完整 JSON → final 正常发布。"""
        full = json.dumps(_trip_payload(), ensure_ascii=False)
        client = _FakeLLMClient([full[: len(full) // 2]], repair_text=full)
        monkeypatch.setattr(trip_stream, "get_llm_client", lambda: client)

        messages = list(llm_open_trip_stream(_day_req()))
        finals = [m for m in messages if m[0] == "final"]
        assert client.repair_calls == 1
        assert len(finals) == 1
        assert [plan["day_no"] for plan in finals[0][1]] == [1, 2]

    def test_stream_kwargs_match_llm_open_trip_contract(self, monkeypatch):
        """流式调用参数与 llm_open_trip 同源：max_tokens 公式 / response_format / 关闭思考。"""

        class SpyClient(_FakeLLMClient):
            def __init__(self, chunks):
                super().__init__(chunks)
                self.messages: list[dict] = []

            def stream_chat_deltas(self, messages, **kwargs):
                self.messages = messages
                return super().stream_chat_deltas(messages, **kwargs)

        client = SpyClient([TRIP_TEXT])
        monkeypatch.setattr(trip_stream, "get_llm_client", lambda: client)
        monkeypatch.setattr(settings, "llm_generation_web_search", False)
        list(llm_open_trip_stream(_day_req(2)))
        assert client.stream_kwargs["max_tokens"] == 2 * 8000, "max_tokens 按天数等比（与 llm_open_trip 同公式）"
        assert client.stream_kwargs["enable_search"] is False
        assert client.stream_kwargs["enable_thinking"] is False
        assert client.stream_kwargs["response_format"] is not None
        assert [m["role"] for m in client.messages] == ["system", "user"]

    def test_day_no_late_binding_attribution(self, monkeypatch):
        """day_no 后置（items 在 day_no 之前）：候选缓冲到归属确定后按正确 dayNo 发布。"""
        payload = (
            '{"daily_plans": ['
            '{"items": ['
            '{"item_type": "attraction", "poi_name": "西湖", "start_time": "09:00", "end_time": "11:00"},'
            '{"item_type": "food", "poi_name": "楼外楼", "start_time": "12:00", "end_time": "13:00"}], '
            '"day_no": 1, "theme": "d1", "note": "n1"},'
            '{"items": [{"item_type": "attraction", "poi_name": "灵隐寺", "start_time": "09:30", "end_time": "11:30"}],'
            ' "day_no": 2, "theme": "d2", "note": "n2"}], "suggestions": []}'
        )
        client = _FakeLLMClient([payload])
        monkeypatch.setattr(trip_stream, "get_llm_client", lambda: client)

        previews: list[tuple] = []
        finals: list[tuple] = []
        done = _consume(llm_open_trip_stream(_day_req()), previews, finals)
        done.wait(10)
        assert done.is_set()
        assert [(p[1], p[2], p[3]["poi_name"]) for p in previews] == [
            (1, 0, "西湖"),
            (1, 1, "楼外楼"),
            (2, 0, "灵隐寺"),
        ], "后置 day_no 到达后统一冲刷，归属与日内序号（0 起）不得错位"
        assert len(finals) == 1

    def test_blocking_llm_open_trip_keeps_shape_after_refactor(self, monkeypatch):
        """llm_open_trip 抽出共享装配段后原签名行为不变：返回值 + 主题注入。"""
        payload = json.dumps(_trip_payload(), ensure_ascii=False)

        class BlockingClient:
            def complete(self, user, **kwargs):
                return payload

            def chat(self, *args, **kwargs):
                raise AssertionError("成功路径不应触发修复重试")

        monkeypatch.setattr(day_prompts, "get_llm_client", lambda: BlockingClient())
        plans, suggestions = day_prompts.llm_open_trip(_day_req())
        assert [plan["day_no"] for plan in plans] == [1, 2]
        assert plans[0]["trip_theme"] == "湖光山色" and plans[1]["trip_theme"] == "湖光山色"
        assert suggestions[0]["poi_name"] == "断桥"


# ------------------------------------ open_plans 挂点（候选/撤回） ----


class TestOpenPlansPreviewHooks:
    def test_stream_leg_forwards_candidates_and_final(self, monkeypatch):
        """on_item_preview 非 None 且多日：整段腿走流式，候选逐个回调，final 走同一后续。"""
        _stub_offline(monkeypatch)

        def fake_stream(trip_req):
            yield ("item_preview", 1, 0, {"item_type": "attraction", "poi_name": "西湖"})
            yield ("item_preview", 1, 1, {"item_type": "food", "poi_name": "楼外楼"})
            yield ("item_preview", 2, 0, {"item_type": "attraction", "poi_name": "灵隐寺"})
            yield ("final", _trip_payload()["daily_plans"], [])

        monkeypatch.setattr(trip_stream, "llm_open_trip_stream", fake_stream)
        monkeypatch.setattr(
            open_plans,
            "llm_open_trip",
            lambda trip_req: (_ for _ in ()).throw(AssertionError("回调在挂时应走流式腿而非阻塞 llm_open_trip")),
        )

        previews: list[tuple] = []
        withdrawn: list[tuple] = []
        increment, errors = open_plans.generate_open_plans(
            _trip_req(),
            "",
            None,
            on_item_preview=lambda d, o, item: previews.append((d, o, item["poi_name"])),
            on_item_withdrawn=lambda d, o, reason: withdrawn.append((d, o)),
        )
        assert errors == [] and increment is not None
        assert not withdrawn
        assert previews == [(1, 0, "西湖"), (1, 1, "楼外楼"), (2, 0, "灵隐寺")]
        plans = increment["daily_plans"]
        assert [p["day_no"] for p in plans] == [1, 2], "final 段与 llm_open_trip 同一后续（落地/组装不分叉）"
        assert plans[0]["items"][0]["poi_name"] == "西湖"

    def test_failure_withdraws_published_candidates_then_falls_back_per_day(self, monkeypatch):
        """整段腿失败：已发布候选逐个显式撤回，缺口天交逐日兜底重生成。"""
        _stub_offline(monkeypatch)

        def boom_stream(trip_req):
            yield ("item_preview", 1, 0, {"item_type": "attraction", "poi_name": "西湖"})
            raise RuntimeError("upstream truncated")

        monkeypatch.setattr(trip_stream, "llm_open_trip_stream", boom_stream)

        def fallback_day(day_req, used):
            return {
                "day_no": day_req.day_no,
                "note": f"兜底{day_req.day_no}",
                "items": [{"item_type": "attraction", "poi_name": f"兜底点{day_req.day_no}"}],
            }

        monkeypatch.setattr(open_plans, "llm_open_day", fallback_day)

        previews: list[tuple] = []
        withdrawn: list[tuple] = []
        increment, errors = open_plans.generate_open_plans(
            _trip_req(),
            "",
            None,
            on_item_preview=lambda d, o, item: previews.append((d, o)),
            on_item_withdrawn=lambda d, o, reason: withdrawn.append((d, o, reason)),
        )
        assert previews == [(1, 0)]
        assert withdrawn == [(1, 0, withdrawn[0][2])] and "整段生成失败" in withdrawn[0][2]
        assert errors, "整段失败必须留下研究错误（不静默）"
        assert increment is not None
        plans = increment["daily_plans"]
        assert [p["day_no"] for p in plans] == [1, 2], "缺口天逐日兜底补齐"
        assert plans[0]["items"][0]["poi_name"] == "兜底点1", "撤回的候选天由重生成结果替换"

    def test_default_path_stays_on_blocking_llm_open_trip(self, monkeypatch):
        """默认（不传回调）= 完全旧行为：走阻塞 llm_open_trip，不触碰流式腿。"""
        _stub_offline(monkeypatch)
        plans_fixture = [
            {
                "day_no": 1,
                "theme": "t1",
                "note": "n1",
                "items": [{"item_type": "attraction", "poi_name": "西湖", "start_time": "09:00", "end_time": "11:00"}],
            },
            {
                "day_no": 2,
                "theme": "t2",
                "note": "n2",
                "items": [
                    {"item_type": "attraction", "poi_name": "灵隐寺", "start_time": "09:30", "end_time": "11:30"}
                ],
            },
        ]
        monkeypatch.setattr(open_plans, "llm_open_trip", lambda trip_req: (plans_fixture, []))
        monkeypatch.setattr(
            open_plans,
            "open_trip_streamed",
            lambda trip_req, on_preview, published: (_ for _ in ()).throw(AssertionError("默认路径不得走流式腿")),
        )
        increment, errors = open_plans.generate_open_plans(_trip_req(), "", None)
        assert errors == [] and increment is not None
        assert [p["day_no"] for p in increment["daily_plans"]] == [1, 2]


# --------------------------------- stream_branch 集成（agent 面 wire） ----


class TestStreamBranchPreviewEvents:
    def _env(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_api_key", "test-key")
        monkeypatch.setattr(settings, "llm_generation_web_search", False)
        _stub_offline(monkeypatch)

    def test_item_previews_on_emits_preview_before_day(self, monkeypatch):
        """开关开：候选先于同 day 正式 day 事件，wire 形状走模型 dump；day/done 序列不变。"""
        self._env(monkeypatch)

        def fake_stream(trip_req):
            yield ("item_preview", 1, 0, {"item_type": "attraction", "poi_name": "西湖"})
            yield ("item_preview", 1, 1, {"item_type": "food", "poi_name": "楼外楼"})
            yield ("item_preview", 2, 0, {"item_type": "attraction", "poi_name": "灵隐寺"})
            yield ("final", _trip_payload()["daily_plans"], [])

        monkeypatch.setattr(trip_stream, "llm_open_trip_stream", fake_stream)
        events = list(run_generate_trip_stream(_day_req(2), item_previews=True))
        types = [e["type"] for e in events]

        previews = [e for e in events if e["type"] == "day_item_preview"]
        assert [(p["dayNo"], p["itemOrdinal"]) for p in previews] == [(1, 0), (1, 1), (2, 0)]
        assert all(p["status"] == "drafting" for p in previews)
        assert set(previews[0]) == {"type", "runId", "dayNo", "itemOrdinal", "item", "status"}
        # item 是开放形状（候选原样透传，snake_case 键、未过 ground），不做 wire 整形
        assert previews[0]["item"] == {"item_type": "attraction", "poi_name": "西湖"}

        first_preview_at = types.index("day_item_preview")
        day1_at = min(i for i, t in enumerate(types) if t == "day")
        assert first_preview_at < day1_at, "候选必须先于正式 day 事件发布"

        # 既有序列不变：done 收尾、daysEmitted 齐、complete
        assert types[-1] == "done"
        done = events[-1]
        assert done["daysEmitted"] == [1, 2] and done["complete"] is True
        day1 = next(e for e in events if e["type"] == "day" and e["plan"]["dayNo"] == 1)
        assert day1["plan"]["items"][0]["poiName"] == "西湖", "day 快照仍是权威内容"

    def test_item_previews_off_keeps_event_stream_unchanged(self, monkeypatch):
        """开关关（默认）：事件序列与既有逐字节一致，无任何预览帧。"""
        self._env(monkeypatch)
        plans_fixture = _trip_payload()["daily_plans"]
        monkeypatch.setattr(open_plans, "llm_open_trip", lambda trip_req: (plans_fixture, []))
        monkeypatch.setattr(
            open_plans,
            "open_trip_streamed",
            lambda trip_req, on_preview, published: (_ for _ in ()).throw(AssertionError("未开启开关不得走流式腿")),
        )
        events = list(run_generate_trip_stream(_day_req(2)))
        types = [e["type"] for e in events]
        assert not [t for t in types if t.startswith("day_item")], "默认路径不得产出预览帧"
        assert types.count("day") == 2 and types[-1] == "done"

    def test_whole_trip_failure_emits_withdrawal_then_fallback_days(self, monkeypatch):
        """流式腿失败：撤回事件先行，随后逐日兜底的 day/done 照常收尾。"""
        self._env(monkeypatch)

        def boom_stream(trip_req):
            yield ("item_preview", 1, 0, {"item_type": "attraction", "poi_name": "西湖"})
            raise RuntimeError("upstream truncated")

        monkeypatch.setattr(trip_stream, "llm_open_trip_stream", boom_stream)

        def fallback_day(day_req, used):
            return {
                "day_no": day_req.day_no,
                "note": f"兜底{day_req.day_no}",
                "items": [{"item_type": "attraction", "poi_name": f"兜底点{day_req.day_no}"}],
            }

        monkeypatch.setattr(open_plans, "llm_open_day", fallback_day)
        events = list(run_generate_trip_stream(_day_req(2), item_previews=True))
        types = [e["type"] for e in events]

        assert types.count("day_item_preview") == 1
        withdrawals = [e for e in events if e["type"] == "day_item_preview_withdrawn"]
        assert len(withdrawals) == 1
        assert withdrawals[0]["dayNo"] == 1 and withdrawals[0]["itemOrdinal"] == 0
        assert withdrawals[0]["reason"], "撤回必须带原因（不静默消失）"
        assert types.index("day_item_preview_withdrawn") < min(i for i, t in enumerate(types) if t == "day"), (
            "撤回先于兜底 day 事件"
        )
        done = events[-1]
        assert done["type"] == "done" and done["daysEmitted"] == [1, 2] and done["complete"] is True


# -------------------------------- 业务面 SSE（generation_events + 编排转发） ----


class TestBusinessSseFrames:
    def test_item_preview_frame_shape(self, monkeypatch):
        """业务帧 data 口径：{runId, previewId, dayNo, itemOrdinal, item}，previewId 三段式。"""
        captured: list[tuple] = []
        monkeypatch.setattr(
            generation_events,
            "publish_event",
            lambda itinerary_id, event_type, data, run_id=None: captured.append(
                (itinerary_id, event_type, data, run_id)
            ),
        )
        generation_events.item_preview(7, "run-1", 2, 3, {"poi_name": "西湖"})
        generation_events.item_preview_withdrawn(7, "run-1", 2, 3, "regenerate")

        assert captured[0][0] == 7 and captured[0][1] == "item_preview"
        assert captured[0][2] == {
            "runId": "run-1",
            "previewId": "run-1:2:3",
            "dayNo": 2,
            "itemOrdinal": 3,
            "item": {"poi_name": "西湖"},
        }
        assert captured[0][3] == "run-1", "runId 同时落 trace 关联"
        assert captured[1][1] == "item_preview_withdrawn"
        assert captured[1][2]["previewId"] == "run-1:2:3" and captured[1][2]["reason"] == "regenerate"

    def test_plan_whole_trip_forwards_preview_events(self, monkeypatch):
        """业务编排消费 agent 事件：day_item_preview / withdrawn 转发 generation_events。"""
        agent_events = [
            {
                "type": "day_item_preview",
                "runId": "run-9",
                "dayNo": 1,
                "itemOrdinal": 0,
                "item": {"poi_name": "西湖"},
                "status": "drafting",
            },
            {"type": "day_item_preview_withdrawn", "runId": "run-9", "dayNo": 1, "itemOrdinal": 0, "reason": "regen"},
        ]

        def fake_stream(request, cancel=None, **kwargs):
            assert kwargs.get("item_previews") is True, "业务整段腿必须开启候选预览（M5a）"
            yield from agent_events

        monkeypatch.setattr(itinerary_generation, "run_generate_trip_stream", fake_stream)
        captured: list[tuple] = []
        monkeypatch.setattr(
            generation_events,
            "item_preview",
            lambda *args: captured.append(("item_preview", args)),
        )
        monkeypatch.setattr(
            generation_events,
            "item_preview_withdrawn",
            lambda *args: captured.append(("item_preview_withdrawn", args)),
        )

        command = itinerary_generation.GenerateCommand(city="杭州", days=2, persons=1, stay_nights=1)
        suggestions_done = itinerary_generation._plan_whole_trip(1, 42, command, {}, "fp")
        assert suggestions_done is False
        assert [kind for kind, _ in captured] == ["item_preview", "item_preview_withdrawn"]
        assert captured[0][1] == (42, "run-9", 1, 0, {"poi_name": "西湖"})
        assert captured[1][1] == (42, "run-9", 1, 0, "regen")
