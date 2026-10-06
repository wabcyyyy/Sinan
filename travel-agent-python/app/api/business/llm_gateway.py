"""用户自带 LLM 网关（BYOK）管理端点：`/api/llm-gateway`。

路由层只做一行转发（门禁纪律：写薄，逻辑在 llm_gateway_service）；全部端点挂
`enforce_business_auth`（不进 PUBLIC_PATHS，无匿名豁免）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AuthUser
from app.api.security import enforce_business_auth
from app.common.envelope import ok
from app.schemas.business.llm_gateway import LlmGatewayCreateBody, LlmGatewayUpdateBody
from app.services import llm_gateway_service

router = APIRouter(
    prefix="/api/llm-gateway",
    tags=["llm-gateway"],
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("")
def list_gateways(user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(llm_gateway_service.list_configs(user.id))


@router.post("")
def create_gateway(body: LlmGatewayCreateBody, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(llm_gateway_service.create_config(user.id, body))


@router.put("/{gateway_id}")
def update_gateway(
    gateway_id: int, body: LlmGatewayUpdateBody, user: AuthUser = Depends(enforce_business_auth)
) -> dict:
    return ok(llm_gateway_service.update_config(user.id, gateway_id, body))


@router.delete("/{gateway_id}")
def delete_gateway(gateway_id: int, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    llm_gateway_service.delete_config(user.id, gateway_id)
    return ok(None)


@router.post("/{gateway_id}/enable")
def enable_gateway(gateway_id: int, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(llm_gateway_service.enable_config(user.id, gateway_id))


@router.post("/{gateway_id}/disable")
def disable_gateway(gateway_id: int, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(llm_gateway_service.disable_config(user.id, gateway_id))


@router.post("/{gateway_id}/test")
def test_gateway(gateway_id: int, user: AuthUser = Depends(enforce_business_auth)) -> dict:
    return ok(llm_gateway_service.test_connection(user.id, gateway_id))
