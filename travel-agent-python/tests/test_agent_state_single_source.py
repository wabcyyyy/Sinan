"""State 单一化的机检（PR-2）：全仓只有一处图状态定义、无 dict 兼容访问器。

风格抄 tests/test_agent_domain_ladder.py（AST 扫描 + 常量钉在磁盘上）。背景：M0 把
主图状态换成 Pydantic 后，仓里长期并存**三份** state 定义（UnifiedAgentState /
AgentState TypedDict / ResearchAgentState TypedDict）与 ``get`` / ``__getitem__`` /
``__contains__`` 兼容访问器——重复定义必然漂移（哲学 1），访问器让节点永远学不会
真实类型、错键只能在运行时炸（哲学 4 的迁移动机）。这里把"只此一份、节点属性访问"
钉成机检：多一份定义、旧名复活、访问器回潮，任一都红。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: 全仓允许的 *State 类：图状态唯一定义 + db 层的 addon 表模型（非图状态，仅同名尾缀）。
EXPECTED_STATE_CLASSES = {
    ("agent/research/agent_state.py", "UnifiedAgentState"),
    ("db/models.py", "AddonState"),
}

#: 图状态定义不准带的 dict 兼容访问器（带回 = 节点属性访问的迁移白做）。
FORBIDDEN_ACCESSORS = {"get", "__getitem__", "__setitem__", "__contains__"}

#: 旧定义名：AgentState（trip 字段 TypedDict 副本）/ ResearchAgentState（研究子图）。
LEGACY_STATE_NAMES = {"AgentState", "ResearchAgentState"}


def _py_files() -> list[Path]:
    return sorted(p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _state_classes() -> dict[tuple[str, str], ast.ClassDef]:
    found: dict[tuple[str, str], ast.ClassDef] = {}
    for path in _py_files():
        rel = path.relative_to(APP_DIR).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and node.name.endswith("State"):
                found[(rel, node.name)] = node
    return found


def test_exactly_one_graph_state_definition() -> None:
    """全仓只有一处图状态定义（db 的 AddonState 表模型除外）；多一份/少一份都红。"""
    assert set(_state_classes()) == EXPECTED_STATE_CLASSES


def test_state_is_pydantic_without_dict_compat_accessors() -> None:
    """UnifiedAgentState 必须是 BaseModel 子类、且无 get/__getitem__/__contains__/__setitem__。"""
    node = _state_classes()[("agent/research/agent_state.py", "UnifiedAgentState")]
    bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
    assert "BaseModel" in bases, "图状态必须是 Pydantic 模型（PR-3 的 checkpoint 序列化前提）"
    methods = {item.name for item in node.body if isinstance(item, ast.FunctionDef)}
    revived = sorted(methods & FORBIDDEN_ACCESSORS)
    assert not revived, f"dict 兼容访问器回潮：{revived}（节点一律属性访问，见 agent_state.py）"


def test_legacy_state_names_are_gone() -> None:
    """旧名 AgentState / ResearchAgentState 不许再作为标识符出现（定义/导入/引用）。"""
    hits: list[str] = []
    for path in _py_files():
        rel = path.relative_to(APP_DIR).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.alias):
                names.append(node.asname or node.name.split(".")[0])
            for name in names:
                if name in LEGACY_STATE_NAMES:
                    hits.append(f"{rel}:{getattr(node, 'lineno', '?')} {name}")
    assert not hits, f"旧 state 名残留（应引用 research.agent_state.UnifiedAgentState）：{hits}"
