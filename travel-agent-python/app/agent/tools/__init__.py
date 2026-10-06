"""工具面域：注册表 / 派发 / FC 循环 / 工具实现。

阶梯位置：``grounding`` 之上、``research`` 之下——只准 import 下层域
（core / runtime / data / grounding）与 stdlib / ``app.common``。

- ``impl``：工具实现（原 ``tools`` 模块）——OTM 为主 + 联网搜索补池 + Nominatim 兜底；
- ``registry/``：受控工具目录（core = 机制，catalog = 登记，导入包即完成登记）；
- ``function_calling``：受限 Function Calling Loop。

**命名约定（可 mock 契约）**：实现模块是 ``impl``，调用方一律
``from app.agent.tools import impl as tools`` 后在**调用期**读模块属性
（``tools.search_attractions(...)``）——注册表 handler 与单测的
``patch.object(tools, ...)`` 因此零修改生效。不得在注册期绑定函数对象。

预算计数状态不在本域：``app.agent.runtime.tool_budget``（observability 开、本域占用）。

不设 re-export 面：域内符号按子模块路径导入。
"""
