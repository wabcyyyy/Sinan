"""LLM JSON 输出解析的唯一实现（G-1.3 ①）。

职责：
- parse_llm_json：LLM 输出 → dict。失败抛 LlmJsonError（ValueError 子类，
  存量 `except ValueError` 兜底链不变）；用于失败必须显式处理的调用方
  （如生成主链路的「严格解析 → 修复重试 → 兜底」三层流）。
- parse_llm_json_or_none：同解析，失败返回 None；用于「解析失败即降级」
  的调用方（intent 提炼、research 规划）。

容错策略（PR-5 收口）：**严格解析**——空串快速失败、json.loads 整段解码、
结果必须是 dict。原「markdown 栅栏剥离」与「首个 `{` 到末个 `}` 截取」两段
容错已删除：全部 LLM 出口都带 response_format（json_schema strict 或
json_object），网关保证纯 JSON 输出；截断/畸形输出不再靠截取悄悄打捞，
改走「修复重试 → 兜底」（editing/chat_draft/decide.py 已验证模式的推广），
坏输出必须显式失败而不是被救活成半截真相。

依赖：json；无内部依赖（保持叶子模块，任何层都可安全 import）。
"""

import json


class LlmJsonError(ValueError):
    """LLM 输出不是可用的 JSON 对象（空输出/解码失败/非 dict）。"""


def parse_llm_json(raw: object) -> dict:
    text = str(raw or "").strip()
    if not text:
        raise LlmJsonError("LLM 输出为空，无法解析 JSON")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LlmJsonError("LLM 输出 JSON 解码失败") from exc
    if not isinstance(data, dict):
        raise LlmJsonError("LLM 输出不是 JSON 对象")
    return data


def parse_llm_json_or_none(raw: object) -> dict | None:
    try:
        return parse_llm_json(raw)
    except LlmJsonError:
        return None
