"""直发 LLM 的业务端点输入上限（审计 §3.3.5 / P1-2 输入上限部分）。

复核附注修正后的真实无界进 prompt 的只有两处：clarify.message 与 city-guide.input
（nl-edit 的 instruction 下游 EditOpRequest 已有 2000 上限，不动）。本批给两处补
max_length：clarify 对齐 ChatTurnRequest.message=2000；city-guide 放宽一档对齐
requirements/intent 的 4000。

两层各自的口径（错误码不属本批，复核附注已登记改码需拍板）：
- agent 面（/api/agent/v1/*）：线级校验天然 422；
- 业务面（/api/itinerary/clarify、/city-guide）：模型构造落在 guard_agent_call 内，
  超长 ValidationError 映射成 Java 网关同款 502 文案——与 nl-edit 超长同语义，
  不再是 300k 字符原样进 prompt。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.schemas.agent_ops import CityGuideRequest
from app.schemas.trip import ClarifyRequest


def _client() -> TestClient:
    # 不进 lifespan（`with`）：422 由路由层线级校验给出，无需启动期装配
    from main import app

    return TestClient(app)


# ---------- 模型级：上限含边界 ----------


def test_clarify_message_cap_is_2000_inclusive() -> None:
    assert ClarifyRequest(message="去" * 2000).message == "去" * 2000
    with pytest.raises(ValidationError):
        ClarifyRequest(message="去" * 2001)


def test_city_guide_input_cap_is_4000_inclusive() -> None:
    assert CityGuideRequest.model_validate({"input": "玩" * 4000}).user_input == "玩" * 4000
    with pytest.raises(ValidationError):
        CityGuideRequest.model_validate({"input": "玩" * 4001})


# ---------- agent 面线级：超长 → 422 ----------


def test_agent_clarify_rejects_oversize_message_with_422() -> None:
    r = _client().post("/api/agent/v1/clarify", json={"message": "去" * 2001})
    assert r.status_code == 422


def test_agent_city_guide_rejects_oversize_input_with_422() -> None:
    r = _client().post("/api/agent/v1/city-guide", json={"input": "玩" * 4001})
    assert r.status_code == 422


# ---------- 业务面：上限生效（错误码保持 Java 契约的 502） ----------


@pytest.fixture
def pinned_city_service(monkeypatch):
    """city-guide 业务腿的依赖全部打桩：supported_cities 不进库、run_city_guide 不外呼。"""
    from app.services import itinerary_city

    monkeypatch.setattr(itinerary_city, "supported_cities", lambda: ["杭州"])
    captured: dict = {}

    def fake_run_city_guide(req: dict) -> dict:
        captured.update(req)
        return {"kind": "city", "city": "杭州", "message": "好选择", "suggestions": []}

    monkeypatch.setattr(itinerary_city, "run_city_guide", fake_run_city_guide)
    yield captured


def test_business_city_guide_oversize_input_is_bounded_not_forwarded(pinned_city_service) -> None:
    """超长 input 不再原样进 prompt：构造期即被 max_length 拦下，落 Java 同款 502 文案。"""
    from app.common.envelope import ApiError
    from app.services import itinerary_city

    with pytest.raises(ApiError) as exc_info:
        itinerary_city.city_guide(1, "玩" * 4001, [])
    assert exc_info.value.status == 502
    assert exc_info.value.message == "城市引导服务暂不可用"
    assert not pinned_city_service, "超长输入不得到达 run_city_guide（也就不会进 prompt）"


def test_business_city_guide_normal_input_still_works(pinned_city_service) -> None:
    from app.services import itinerary_city

    result = itinerary_city.city_guide(1, "想去江南水乡", [])
    assert result["city"] == "杭州"
    assert pinned_city_service["input"] == "想去江南水乡"


def test_business_clarify_oversize_message_is_bounded_not_forwarded(monkeypatch) -> None:
    """clarify 的模型构造本就在 guard 内：超长 message → 502 意图解析文案，不进 prompt。"""
    from app.common.envelope import ApiError
    from app.services import itinerary_city

    called: list[ClarifyRequest] = []
    monkeypatch.setattr(itinerary_city, "run_clarify", lambda req: called.append(req))
    with pytest.raises(ApiError) as exc_info:
        itinerary_city.clarify(1, "去" * 2001, {})
    assert exc_info.value.status == 502
    assert exc_info.value.message == "意图解析服务暂不可用"
    assert not called, "超长输入不得到达 run_clarify（也就不会进 prompt）"
