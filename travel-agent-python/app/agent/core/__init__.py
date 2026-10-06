"""零依赖原语域：agent 域阶梯的最底层。

本域只准 import stdlib / ``app.common`` / ``app.agent.core`` 自身——被所有其他域
依赖，自己不依赖任何域（判据：tests/test_agent_core_is_dependency_free.py 机检）。

- ``json_utils``：LLM JSON 输出解析的唯一实现（合并原四处重复的最强容错）；
- ``geo``：球面距离与最近邻路线排序（纯函数，无外部依赖）；
- ``poi_identity``：点位同一性判定（名称归一 / 球面距离 / 跨天去重）——
  名称归一与距离门槛只准有这一份，否则去重与存在性判定会各说一套；
- ``intent``：用户旅行意图的轻量提炼（生成链路与研究链路共用）。

不设 re-export 面：域内符号按子模块路径导入（``from app.agent.core.geo import ...``），
避免再造一层转发壳（判例见 ``app/agent/editing/chat_draft/__init__.py`` 的 docstring）。
"""
