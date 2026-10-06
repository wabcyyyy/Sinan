"""Prompt 模板集中管理。

职责：
- 收纳各生成链路的巨型 Prompt 构造函数，使 app/agent 编排代码与 Prompt 文本分离；
- M0 仅做原样搬迁（逐字符一致，不改写、不抽取公共片段），输出与搬迁前完全一致。

版本约定：
- 每个模板函数旁有对应版本常量（如 OPEN_DAY_PROMPT_VERSION）；
- 任何语义变更（改措辞、改规则、合并片段）都必须递增版本号并在本模块 docstring
  记录 changelog；纯格式化参数变化不递增。
- v1.0.migrated = 与搬迁前 day_stream 内联版本逐字符一致。

变更记录：
- v1.0.migrated（M0）：自 app/agent/generation/orchestration/day_stream.py 原样迁入 open_day / open_trip 两段
  system prompt，未做任何文本改动。
"""
