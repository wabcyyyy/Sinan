"""前端错误探针的上报契约——登记进 contracts.py 的 business_api 组。

线级口径：
- 全字段设上限：端点匿名可达（登录页自身崩溃也要能上报），防的就是大 payload 灌日志；
- 身份不进契约：有会话时由鉴权依赖顺带解析进日志，无会话照收——
  探针覆盖面优先，归属是运维信息不是数据面事实；
- 只做结构化日志、不落库：错误报告是运维信号不是用户数据，入库要吃一条
  append-only 迁移却换不来任何消费面（单 VPS 上 docker logs 即可检索）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ClientErrorReport(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    stack: str | None = Field(default=None, max_length=8000)
    source: str | None = Field(default=None, max_length=40)
    path: str | None = Field(default=None, max_length=500)
    userAgent: str | None = Field(default=None, max_length=500)
    ts: str | None = Field(default=None, max_length=64)
