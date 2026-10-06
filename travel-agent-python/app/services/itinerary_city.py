"""城市能力（移植自 Java `ItineraryCityService`）：支持城市清单、城市引导、附近推荐、槽位澄清。

迁移后这四个能力**同进程直调 agent 层**，不再走 Java→HTTP→Python 那一跳。省掉一次序列化，
但两条 Java 侧契约必须原样搬过来，因为它们才是前端看到的东西：

1. **失败口径**（照 `AgentServiceImpl.postForNode` 逐字映射）：
   - 请求体不合法（今天是 FastAPI 422 → Java `RestClientException`）→ `502 <域名>服务暂不可用`；
   - agent 以业务原因拒绝（今天是 `ApiResponse.fail` → Java 见 `code!=200`）→ `502 请求失败：<原因>`；
   - agent 内部崩溃 → 同第一条。
   换进程不换文案，前端 toast 与既有 e2e 断言都不会漂移。
2. **出参形状与缺省值**：`{kind,city,message,suggestions}` / `{items:[…]}` /
   `{slots,missing,question,ready,options,blocked}`。

另有一处**不能"顺手修好"**的地方：附近推荐在 agent 层任何异常时返回空列表而不是报错
（`app/api/agent.py` 的 `except Exception: ApiResponse.ok(PoiNearbyResponse())`）。附近推荐是
锦上添花的能力，坐标解析失败不该让用户看到 502，这里保持静默降级。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import select

from app.agent import find_nearby_pois, run_city_guide, run_clarify
from app.common.envelope import ApiError
from app.db.models import CityGeo
from app.db.session import session_scope
from app.schemas.agent_ops import CityGuideRequest, PoiNearbyItem, PoiNearbyRequest
from app.schemas.trip import ClarifyRequest
from app.services import llm_gateway_service

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_GUIDE_MESSAGE = "想去哪里玩？说说你的想法～"


def supported_cities() -> list[str]:
    """受支持的目的地城市 = city_geo 字典全量。

    POI 库退役后，「能不能生成」不再受本地语料限制，字典即城市清单；
    与 Atlas 的「城市属于哪国」天然同源，不再是两份清单。
    """
    with session_scope() as session:
        stmt = select(CityGeo.city_name).order_by(CityGeo.city_name)
        return [str(city) for city in session.execute(stmt).scalars().all() if city]


def guard_agent_call(unavailable_message: str, invoke: Callable[[], T]) -> T:
    """把 agent 层异常映射成 Java 网关当年会返回的同一句 502 文案。"""
    try:
        return invoke()
    except ValidationError as exc:
        raise ApiError(502, unavailable_message) from exc
    except ValueError as exc:
        raise ApiError(502, f"请求失败：{exc}") from exc
    except ApiError:
        raise
    except Exception as exc:
        logger.warning("agent call failed: %s", exc, exc_info=True)
        raise ApiError(502, unavailable_message) from exc


def clarify(user_id: int, message: str, slots: dict[str, Any]) -> dict[str, Any]:
    """槽位澄清：抽取行程参数并返回缺失字段与追问（纯解析，不落库）。

    user_id（BYOK 路由）：clarify 烧 LLM，路由上下文随调用进入（调用方为请求线程）。
    """
    with llm_gateway_service.route_scope(user_id):
        response = guard_agent_call(
            "意图解析服务暂不可用", lambda: run_clarify(ClarifyRequest(message=message, slots=slots or {}))
        )
    return {
        "slots": response.slots,
        "missing": response.missing,
        "question": response.question,
        "ready": bool(response.ready),
        "options": list(response.options or []),
        "blocked": bool(response.blocked),
        "degraded": bool(response.degraded),
    }


def city_guide(user_id: int, input_text: str, history: list[dict[str, Any]] | None) -> dict[str, Any]:
    """目的地不确定时的城市推荐对话。`supported` 由服务端现算，不接受客户端伪造。"""
    request = CityGuideRequest(
        user_input=input_text or "", supported=supported_cities(), history=_guide_history(history)
    )
    # by_alias=True：user_input 落成 wire 键 "input"，与 run_city_guide 读取的键一致（同迁移前）
    with llm_gateway_service.route_scope(user_id):
        response = guard_agent_call("城市引导服务暂不可用", lambda: run_city_guide(request.model_dump(by_alias=True)))
    suggestions = [
        {"name": item["name"], "reason": item.get("reason") or ""}
        for item in (response.get("suggestions") or [])
        if item.get("name")
    ]
    return {
        "kind": response.get("kind") or "unclear",
        "city": response.get("city"),
        "message": response.get("message") or DEFAULT_GUIDE_MESSAGE,
        "suggestions": suggestions,
    }


def poi_nearby(payload: dict[str, Any]) -> dict[str, Any]:
    """同城权威知识库的真实近邻（轻量 GraphRAG）。

    入参先过一遍 `PoiNearbyRequest`：迁移前它是 HTTP 层的校验者（不合法 → Java 侧
    502「附近推荐服务暂不可用」），换进程后校验口径不能悄悄变松。`radiusM`/`radius_m`
    两种键历史调用方都在用，WireModel 的 camel 别名 + populate_by_name 两者都收。
    参数换算逐项保持：0 值 falsy 归默认/None；坐标原样透传（0.0 仍传 0.0，由底层判无效坐标）。
    """
    try:
        request = PoiNearbyRequest.model_validate(payload or {})
    except ValidationError as exc:
        raise ApiError(502, "附近推荐服务暂不可用") from exc
    try:
        rows = find_nearby_pois(
            request.city,
            name=request.name or None,
            latitude=request.latitude,
            longitude=request.longitude,
            limit=request.limit or 5,
            radius_m=request.radius_m or None,
            category=request.category or None,
        )
        return {"items": [_nearby_row(row) for row in rows]}
    except Exception as exc:
        logger.warning("poi nearby failed, returned empty list: %s", exc)
        return {"items": []}


def _nearby_row(row: dict[str, Any]) -> dict[str, Any]:
    """前端契约行：name/category/rating/address/distanceM（camelCase）。"""
    item = PoiNearbyItem.model_validate(row)
    return {
        "name": item.name or "",
        "category": item.category or "attraction",
        "rating": item.rating,
        "address": item.address,
        "distanceM": item.distance_m,
    }


def _guide_history(history: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [{"role": _as_str(row.get("role")), "content": _as_str(row.get("content"))} for row in (history or [])]


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)
