"""受控工具目录（包）：core = 机制，catalog = 登记（G-2.6 按域拆包）。

公开面（re-export，消费者零改动）：registry / ToolSpec / ToolInvocationError / RunContext。
导入本包即完成全部工具登记（__init__ 末尾导入 catalog）。

工具调用预算（begin_tool_budget / end_tool_budget / tool_budget_snapshot）**不在这里**：
它是运行边界，属 `app.agent.runtime.tool_budget`（2026-09-23 域化改造上移，解掉
observability → tool_registry 的倒挂）。
"""

from app.agent.tools.registry import catalog
from app.agent.tools.registry.core import (
    RunContext,
    ToolInvocationError,
    ToolRegistry,
    ToolSpec,
    registry,
)

__all__ = [
    "RunContext",
    "ToolInvocationError",
    "ToolRegistry",
    "ToolSpec",
    "catalog",
    "registry",
]
