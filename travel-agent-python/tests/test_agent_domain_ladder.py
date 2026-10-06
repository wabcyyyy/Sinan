"""域阶梯的机检：补 import-linter 管不到的那一刀（2026-09-23 域化改造）。

域地图（``app/agent/README.md``）规定：上层域可 import 下层域，下层域不得 import 上层域。
这件事由 ``pyproject.toml`` 的 "Agent domain ladder" 契约机检，但那个契约只覆盖
**已列入契约的域**——平铺模块不在任何层里，于是「域内模块 import 平铺模块（未来的
上层）」不会被它拦下。本测试补这一刀，并把阶梯常量钉在磁盘上（域目录消失/改名即红）。

豁免表按 INV-1 留期限：每条写明消除条件，条件达成后必须删除（否则本测试沦为形式）。
"""

from __future__ import annotations

import ast
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "app" / "agent"

#: 域阶梯，自下而上。迁移每落地一个域就在此追加（与 pyproject 阶梯契约同一 commit）。
DOMAIN_LADDER = ["core", "runtime", "data", "grounding", "tools", "research", "generation", "editing"]

#: 域内模块 import 平铺模块的豁免：(导入方域, 被导入模块) → 消除条件。
FLAT_IMPORT_EXEMPTIONS: dict[tuple[str, str], str] = {}


def _domain_of(dotted: str) -> str | None:
    """``app.agent.<x>`` 所属域；``<x>`` 不是域目录（平铺模块/门面）时返回 None。"""
    parts = dotted.split(".")
    return parts[2] if len(parts) >= 3 and parts[2] in DOMAIN_LADDER else None


def _domain_files(domain: str) -> list[Path]:
    return sorted(p for p in (AGENT_DIR / domain).rglob("*.py") if "__pycache__" not in p.parts)


def _agent_imports(path: Path) -> set[str]:
    """本模块直接 import 的 agent 点分名（含 ``from app.agent import X`` 的 X）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.agent"):
            found.add(node.module)
            if node.module == "app.agent":
                found.update(f"app.agent.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith("app.agent"))
    return found


def test_ladder_constant_points_at_real_domain_packages() -> None:
    """阶梯常量里不许留死名字：域目录不存在（改名/漏建）即红。"""
    missing = [d for d in DOMAIN_LADDER if not (AGENT_DIR / d / "__init__.py").exists()]
    assert not missing, f"DOMAIN_LADDER 里的这些域在磁盘上没有包（改名了？漏建 __init__.py？）：{missing}"


def test_domain_modules_only_import_same_or_lower_domains() -> None:
    violations: list[str] = []
    for domain in DOMAIN_LADDER:
        rank = DOMAIN_LADDER.index(domain)
        for path in _domain_files(domain):
            rel = path.relative_to(AGENT_DIR.parent.parent).as_posix()
            for target in sorted(_agent_imports(path)):
                target_domain = _domain_of(target)
                if target_domain is not None:
                    if DOMAIN_LADDER.index(target_domain) > rank:
                        violations.append(f"{rel} -> {target}（{domain} 域 import 了上层 {target_domain} 域）")
                    continue
                # 平铺模块 = 尚未域化的上层；门面本身（app.agent）永远不许被域内模块 import
                if (domain, target) in FLAT_IMPORT_EXEMPTIONS:
                    continue
                violations.append(f"{rel} -> {target}（平铺模块未登记豁免；{domain} 域只准依赖下层域）")

    assert not violations, (
        "域阶梯被打破（上层可 import 下层，下层不得 import 上层；平铺模块视为上层）：\n  - " + "\n  - ".join(violations)
    )


def test_flat_import_exemptions_are_still_needed() -> None:
    """反向也要钉：倒挂修好后豁免必须删掉，否则它会在下次改动里掩盖真的反向依赖。"""
    used: set[tuple[str, str]] = set()
    for domain in DOMAIN_LADDER:
        for path in _domain_files(domain):
            for target in _agent_imports(path):
                if _domain_of(target) is None and (domain, target) in FLAT_IMPORT_EXEMPTIONS:
                    used.add((domain, target))

    stale = sorted(set(FLAT_IMPORT_EXEMPTIONS) - used)
    assert not stale, f"这些豁免已无对应 import，删掉（消除条件已达成？）：{stale}"
