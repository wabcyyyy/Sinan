"""业务服务层：行程域的编排、持久化与查询（api 层之下、agent 层之上）。

选址规则见本包各模块与 AGENTS.md：业务写路径、用户与缓存编排放这里；
agent 能力只经 `from app.agent import ...` 门面消费（import-linter 强制）。
"""
