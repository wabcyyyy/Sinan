"""Conservative static import selection; callers must review dynamic consumers."""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path
from typing import Any


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def affected(
    root: Path, cfg: dict[str, Any], files: list[str], changed: list[str]
) -> tuple[set[str], dict[str, set[str]]]:
    """Follow reverse imports, relative imports and literal patch target strings."""
    modules, graph = {}, {}
    for name in files:
        if matches(name, cfg["python_sources"]) and not matches(name, cfg["exclude"]) and (root / name).is_file():
            relative = name.removeprefix(cfg["python_prefix"])
            module = relative[:-3].replace("/", ".").removesuffix(".__init__")
            modules[module] = name
    for module, name in modules.items():
        tree = ast.parse((root / name).read_text(encoding="utf-8-sig"), filename=name)
        dependencies = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                dependencies.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    package = module if name.endswith("/__init__.py") else module.rpartition(".")[0]
                    parts = package.split(".")
                    base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
                dependencies.add(base)
                dependencies.update(base + "." + alias.name for alias in node.names)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and " " not in node.value:
                dependencies.add(node.value)
        linked = set()
        for dep in dependencies:
            while dep:
                if dep in modules:
                    linked.add(modules[dep])
                    break
                dep = dep.rpartition(".")[0]
        graph[name] = linked
    return consumer_tests(graph, changed, cfg["tests"]), graph


def consumer_tests(graph: dict[str, set[str]], changed: list[str], patterns: list[str]) -> set[str]:
    closure = set(changed)
    while True:
        more = {name for name, deps in graph.items() if deps & closure}
        if more <= closure:
            break
        closure |= more
    return {name for name in closure if matches(name, patterns)}
