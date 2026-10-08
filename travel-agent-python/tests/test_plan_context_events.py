"""plan-context 研究进度事件接入单测：stub 研究链路走通成功/降级/异常三路径。

发布调用通过 monkeypatch event_publisher.publish_event 记录（高层便捷函数
publish_research_* 内部按调用时名解析 publish_event，故替换后全链路生效）；
研究依赖（LLM/高德/RAG）以 run_research_context 的最小 stub 绕开重资源。
"""

import json

import pytest

from app.agent.generation.orchestration import plan_context
from app.agent.runtime.trace import trace_run
from app.common import event_publisher
from app.common.event_publisher import (
    publish_degraded,
    publish_research_done,
    publish_research_start,
)
from app.schemas.trip import GenerateRequest, PlanContextRequest


@pytest.fixture()
def events(monkeypatch):
    """记录 publish_event 收到的 (itinerary_id, type, data)；模拟真实缺失即跳过。"""
    recorded: list[tuple[int | None, str, dict]] = []

    def _fake_publish(itinerary_id, event_type, data):
        if not itinerary_id:
            return
        recorded.append((itinerary_id, event_type, data))

    monkeypatch.setattr(event_publisher, "publish_event", _fake_publish)
    return recorded


def _successful_context() -> dict:
    """模拟 Supervisor.synthesize 的成功输出形状（含 research_report）。"""
    return {
        "candidates": [{"name": f"景点{i}"} for i in range(10)],
        "foods": [{"name": f"餐厅{i}"} for i in range(6)],
        "hotels": [{"name": "酒店A"}],
        "consumption": {"meal_price": 50},
        "research_report": {
            "mode": "supervisor",
            "agents": {
                "attraction": {"domain": "attraction", "count": 10, "degraded": False, "gaps": []},
                "food": {"domain": "food", "count": 6, "degraded": False, "gaps": []},
                "hotel": {"domain": "hotel", "count": 1, "degraded": False, "gaps": []},
            },
        },
    }


def test_plan_context_success_publishes_start_and_done(events, monkeypatch):
    monkeypatch.setattr(plan_context, "run_research_context", lambda req: _successful_context())

    result = plan_context.run_plan_context(GenerateRequest(city="杭州", preferences=["亲子"]), itinerary_id=88)

    assert [e[1] for e in events] == ["research_start", "research_done"]
    start = events[0]
    assert start[0] == 88
    assert start[2] == {"domains": ["attraction", "food", "hotel"]}
    done = events[1]
    assert done[0] == 88
    # evidenceCount=实际返回的三域候选总数；degraded 如实为 False
    assert done[2] == {
        "evidenceCount": 17,
        "degraded": False,
        "domains": [
            {"domain": "attraction", "count": 10},
            {"domain": "food", "count": 6},
            {"domain": "hotel", "count": 1},
        ],
    }
    assert not any(e[1] == "degraded" for e in events)
    # 返回契约：四键研究上下文 + C3.1 天气键（无日期入参时恒为 None）
    # + L14 航班报价键（无出发地时恒为空列表，缺席原因由工具层留痕）
    # M1b：research_report（各域降级证据）不再在投影时丢弃
    assert set(result) == {
        "candidates",
        "foods",
        "hotels",
        "consumption",
        "weather",
        "flight_quotes",
        "research_report",
    }
    assert result["weather"] is None
    assert result["flight_quotes"] == []


def test_plan_context_degraded_pack_publishes_degraded_event(events, monkeypatch):
    context = _successful_context()
    # 单域研究走了降级证据包（Supervisor 捕获域异常，不阻塞其它域）：
    # 降级包 items 为空，返回的 foods 列表也随之清空
    context["foods"] = []
    context["research_report"]["agents"]["food"] = {
        "domain": "food",
        "count": 0,
        "degraded": True,
        "gaps": ["研究失败：高德超时"],
    }
    monkeypatch.setattr(plan_context, "run_research_context", lambda req: context)

    plan_context.run_plan_context(GenerateRequest(city="杭州"), itinerary_id=88)

    types = [e[1] for e in events]
    assert types == ["research_start", "research_done", "degraded"]
    done = events[1][2]
    assert done["degraded"] is True
    assert done["evidenceCount"] == 11
    degraded = events[2]
    assert degraded[0] == 88
    assert degraded[2]["scope"] == "research"
    assert "高德超时" in degraded[2]["reason"]
    assert degraded[2]["fallback"]


def test_plan_context_exception_publishes_degraded_and_reraises(events, monkeypatch):
    def _boom(req):
        raise ValueError("开放研究失败")

    monkeypatch.setattr(plan_context, "run_research_context", _boom)

    with pytest.raises(ValueError):
        plan_context.run_plan_context(GenerateRequest(city="杭州"), itinerary_id=88)

    assert [e[1] for e in events] == ["research_start", "degraded"]
    degraded = events[1]
    assert degraded[2]["scope"] == "research"
    assert "开放研究失败" in degraded[2]["reason"]


def test_plan_context_without_itinerary_id_publishes_nothing(events, monkeypatch):
    monkeypatch.setattr(plan_context, "run_research_context", lambda req: _successful_context())

    plan_context.run_plan_context(GenerateRequest(city="杭州"))

    assert events == []


def test_research_event_stats_without_report_falls_back_to_list_lengths(events, monkeypatch):
    """旧式 stub 上下文没有 research_report 时按列表长度统计且不算降级。"""
    monkeypatch.setattr(
        plan_context,
        "run_research_context",
        lambda req: {
            "candidates": [{"name": "a"}, {"name": "b"}],
            "foods": [{"name": "f"}],
            "hotels": [],
            "consumption": {},
        },
    )

    plan_context.run_plan_context(GenerateRequest(city="杭州"), itinerary_id=1)

    done = events[1][2]
    assert done["evidenceCount"] == 3
    assert done["degraded"] is False
    assert done["domains"] == [
        {"domain": "attraction", "count": 2},
        {"domain": "food", "count": 1},
        {"domain": "hotel", "count": 0},
    ]


def test_plan_context_request_accepts_camel_case_itinerary_id():
    req = PlanContextRequest.model_validate({"city": "北京", "itineraryId": 42})
    assert req.itinerary_id == 42
    # 旧调用方不传时保持 None，事件链路静默跳过
    assert PlanContextRequest.model_validate({"city": "北京"}).itinerary_id is None


def test_publisher_helpers_route_through_publish_event(events):
    """便捷函数是 publish_event 的薄封装：事件类型与 data 形状在此收敛。"""
    publish_research_start(3, ["attraction"])
    publish_research_done(3, 2, False, [{"domain": "attraction", "count": 2}])
    publish_degraded(3, "research", "r", "f")

    assert [(e[0], e[1]) for e in events] == [(3, "research_start"), (3, "research_done"), (3, "degraded")]


def _fake_redis_recorder(monkeypatch) -> list[str]:
    """注入最小 Redis 桩，返回收集到的发布 payload（JSON 字符串）列表。"""
    published: list[str] = []

    class _FakeRedis:
        def incr(self, key: str) -> int:
            return len(published) + 1

        def expire(self, key: str, ttl: int) -> None:
            return None

        def publish(self, channel: str, payload: str) -> None:
            published.append(payload)

    monkeypatch.setattr(event_publisher, "_get_client", lambda: _FakeRedis())
    return published


def test_plan_context_events_carry_trace_run_id(monkeypatch):
    """M5 三向关联：trace 上下文内发布的 research 事件 data 携带当前 runId。"""
    published = _fake_redis_recorder(monkeypatch)
    monkeypatch.setattr(plan_context, "run_research_context", lambda req: _successful_context())

    with trace_run("run-plan-context") as recorder:
        plan_context.run_plan_context(GenerateRequest(city="杭州"), itinerary_id=88)

    payloads = [json.loads(p) for p in published]
    assert [p["type"] for p in payloads] == ["research_start", "research_done"]
    # runId 在 data 自定义区；信封五键不变，且与当前轨迹 run_id 一致
    for payload in payloads:
        assert set(payload) == {"type", "itineraryId", "seq", "ts", "data"}
        assert payload["data"]["runId"] == recorder.run_id == "run-plan-context"


def test_plan_context_events_without_trace_have_no_run_id(monkeypatch):
    """无 trace 上下文时退化为旧形态：事件 data 不含 runId。"""
    published = _fake_redis_recorder(monkeypatch)
    monkeypatch.setattr(plan_context, "run_research_context", lambda req: _successful_context())

    plan_context.run_plan_context(GenerateRequest(city="杭州"), itinerary_id=88)

    assert published
    for payload in (json.loads(p) for p in published):
        assert "runId" not in payload["data"]
