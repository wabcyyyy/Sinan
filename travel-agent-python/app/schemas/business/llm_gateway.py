"""用户自带 LLM 网关（BYOK）线级契约（G-1.1；端点见 app/api/business/llm_gateway.py）。

脱敏红线（本域的立身之本）：
- LlmGatewayVO **只**携带 apiKeyHint（尾 4 位）；明文 apiKey 只出现在
  Create/Update 请求体与上游请求头构造的内存瞬间，绝不进任何响应/日志/异常；
- 模型字段名沿用本包 camelCase 惯例（前端在发这个形状）。

依赖：pydantic；无内部依赖。
"""

from datetime import datetime

from pydantic import BaseModel, field_validator

_MAX_NAME = 64
_MAX_BASE_URL = 512
_MAX_MODEL = 128
_MAX_API_KEY = 256


def _clean_name(value: str) -> str:
    if not value.strip():
        raise ValueError("配置名不能为空")
    if len(value) > _MAX_NAME:
        raise ValueError("配置名最长 64 字符")
    return value.strip()


def _clean_base_url(value: str) -> str:
    stripped = value.strip()
    if not (stripped.startswith("http://") or stripped.startswith("https://")):
        raise ValueError("网关地址必须以 http:// 或 https:// 开头")
    if len(stripped) > _MAX_BASE_URL:
        raise ValueError("网关地址过长")
    return stripped.rstrip("/")


def _clean_model(value: str) -> str:
    if not value.strip():
        raise ValueError("模型名不能为空")
    if len(value) > _MAX_MODEL:
        raise ValueError("模型名最长 128 字符")
    return value.strip()


class LlmGatewayCreateBody(BaseModel):
    """`POST /api/llm-gateway` 请求体。"""

    name: str
    baseUrl: str
    apiKey: str
    model: str

    @field_validator("name")
    @classmethod
    def _name_rules(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("baseUrl")
    @classmethod
    def _base_url_rules(cls, value: str) -> str:
        return _clean_base_url(value)

    @field_validator("apiKey")
    @classmethod
    def _api_key_rules(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("API 密钥不能为空")
        if len(value) > _MAX_API_KEY:
            raise ValueError("API 密钥过长")
        return value

    @field_validator("model")
    @classmethod
    def _model_rules(cls, value: str) -> str:
        return _clean_model(value)


class LlmGatewayUpdateBody(BaseModel):
    """`PUT /api/llm-gateway/{id}` 请求体：全部可空，apiKey 留空 = 不修改密钥。"""

    name: str | None = None
    baseUrl: str | None = None
    apiKey: str | None = None
    model: str | None = None

    @field_validator("name")
    @classmethod
    def _name_optional_rules(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)

    @field_validator("baseUrl")
    @classmethod
    def _base_url_optional_rules(cls, value: str | None) -> str | None:
        return None if value is None else _clean_base_url(value)

    @field_validator("apiKey")
    @classmethod
    def _api_key_optional_rules(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None  # 留空/空白 = 不修改
        if len(value) > _MAX_API_KEY:
            raise ValueError("API 密钥过长")
        return value

    @field_validator("model")
    @classmethod
    def _model_optional_rules(cls, value: str | None) -> str | None:
        return None if value is None else _clean_model(value)


class LlmGatewayVO(BaseModel):
    """列表/详情回显：只有尾 4 位提示，没有密文更没有明文。"""

    id: int
    name: str
    baseUrl: str
    model: str
    apiKeyHint: str | None
    enabled: bool
    createdAt: datetime | None
    updatedAt: datetime | None


class LlmGatewayTestVO(BaseModel):
    """`POST /api/llm-gateway/{id}/test` 回显：连通性结果（文案脱敏，不含密钥）。"""

    ok: bool
    latencyMs: int
    message: str
