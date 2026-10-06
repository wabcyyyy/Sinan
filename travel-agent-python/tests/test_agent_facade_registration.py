"""门面契约的登记面机检（R0-4，把根 AGENTS.md 的散文变成门禁）。

根 AGENTS.md 写着「新增 agent 子模块必须登记进契约的 forbidden_modules」，但这句话
本身没有任何机制：`app/agent/` 下 50 个模块里漏 2 个（weather / json_utils）时
`lint-imports` 依旧 KEPT —— 因为契约只拦它列出的模块。漏登记的模块于是成了
「api/services 可以深路径 import 而不会红」的暗洞。

本测试枚举 `app/agent/**` 的全部模块与子包，与 pyproject 里门面契约的
forbidden_modules 取差集；非空即红。import-linter 的包语义（登记父包即覆盖子模块）
在这里同等复现，所以只要某个祖先被登记过就算覆盖。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"
PKG_ROOT = AGENT_DIR.parent.parent  # travel-agent-python/
PYPROJECT = PKG_ROOT / "pyproject.toml"
FACADE_CONTRACT = "Agent facade: api/services import app.agent only, never agent internals"


def _module_paths() -> set[str]:
    """app/agent 下所有模块与子包的点分名（不含门面本身 app.agent）。"""
    names: set[str] = set()
    for path in AGENT_DIR.rglob("*"):
        if not path.is_file() or path.suffix != ".py" or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PKG_ROOT).with_suffix("")
        if path.name == "__init__.py" and path.parent == AGENT_DIR:
            continue  # app/agent/__init__.py = 门面本体，不登记
        parts = [part for part in rel.parts if part != "__init__"]
        names.add(".".join(parts))
    return names


def _forbidden_modules() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    contracts = data.get("tool", {}).get("importlinter", {}).get("contracts", [])
    for contract in contracts:
        if contract.get("name") == FACADE_CONTRACT:
            return set(contract.get("forbidden_modules", []))
    raise AssertionError(f"pyproject 里找不到门面契约 {FACADE_CONTRACT!r}（改名了？本测试要一起改）")


def test_every_agent_module_is_covered_by_the_facade_contract() -> None:
    forbidden = _forbidden_modules()

    def covered(dotted: str) -> bool:
        return any(dotted == name or dotted.startswith(f"{name}.") for name in forbidden)

    unregistered = sorted(name for name in _module_paths() if not covered(name))
    assert not unregistered, (
        "这些 agent 模块不在门面契约的 forbidden_modules 里，深路径 import 不会红："
        f"{unregistered}。补进 pyproject 的 [tool.importlinter] 门面契约。"
    )


def test_contract_still_lists_no_dead_names() -> None:
    """反向也要钉：模块改名/删除后，契约里留下的死名会让「登记面已覆盖」不可信。"""
    live = _module_paths()

    def still_exists(name: str) -> bool:
        return name in live or any(mod.startswith(f"{name}.") for mod in live)

    stale = sorted(name for name in _forbidden_modules() if not still_exists(name))
    assert not stale, f"forbidden_modules 里这些名字已无对应模块，删掉：{stale}"
