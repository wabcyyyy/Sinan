"""API 数据契约基础类型。

职责：
- 定义所有请求/响应模型的基类与统一返回信封。

实现要点：
- WireModel 继承 pydantic BaseModel，配置 to_camel 别名生成 +
  populate_by_name，使 JSON 对外为 camelCase、对内可用 snake_case；
- ApiResponse[T] 为泛型信封（code/message/data），ok/fail 提供构造助手；
- 所有业务 schema 均继承自此，保证前后端字段命名一致。

依赖：
- pydantic；无内部依赖。
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

T = TypeVar("T")


class WireModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ApiResponse(WireModel, Generic[T]):
    code: int
    message: str
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None, message: str = "success") -> "ApiResponse[T]":
        return cls(code=200, message=message, data=data)

    @classmethod
    def fail(cls, message: str, code: int = 500) -> "ApiResponse[None]":
        return cls(code=code, message=message, data=None)
