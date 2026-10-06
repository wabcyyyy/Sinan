"""运行期基座域：轨迹 / 限额 / 用量 / 指标 / 会话状态。

阶梯位置：``core`` 之上、``data`` 之下——只准 import ``app.agent.core`` 与
stdlib / ``app.common``，不得 import 任何上层域（data / grounding / tools /
research / generation / editing）。

- ``trace`` / ``trace_store``：运行轨迹采集与本地持久化；
- ``run_limits``：一次运行的硬性边界（deadline、模型/检索/研究/存在性/Token 预算）；
- ``tool_budget``：工具调用预算的计数状态（observability 开、tool_registry 占用）；
- ``usage_store``：LLM 用量历史（SQLite 落库，供后台仪表盘查询）；
- ``observability``：指标与安全轨迹收口（域内最上层）；
- ``memory/``：请求内 WorkingMemory 与会话对话滑窗（不做长期向量记忆）。

不设 re-export 面：域内符号按子模块路径导入。
"""
