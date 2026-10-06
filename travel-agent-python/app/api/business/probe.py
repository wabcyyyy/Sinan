"""联通性探针端点（Java `TestController#hello`）。

为什么要搬：`release.yml`、`nightly.yml` 与 `start-all.ps1` 都用
`GET /api/test/hello` 判"8080 上这套栈起来了"。M7 把 Java 退掉后，这个 URL 必须
仍然有人应答，否则三条流水线与本地一键启动全要跟着改——迁一个常量端点比改三处便宜。

刻意**不**搬 `GET /api/test/call-agent`：它的用途是"Java 探 Python 的内部跳"，
单进程后这一跳不存在（等价物是 `/api/agent/health`）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.security import enforce_business_auth
from app.common.envelope import ok

router = APIRouter(
    prefix="/api/test",
    tags=["test"],
    # 公开路径也要挂：默认拒绝的方向靠"每条业务 router 都声明守卫"维持，
    # 白名单放行由 enforce_business_auth 内部判（/api/test/hello 在 permitAll 里）。
    dependencies=[Depends(enforce_business_auth)],
)


@router.get("/hello")
def hello() -> dict:
    return ok("hello from travel-agent-python")
