"""Windows/GBK 语言环境下的文件编码防线。

`configparser`（alembic.ini）、`powershell -File`（*.ps1）都按 **系统区域编码** 读取文件，
不是 UTF-8：非 ASCII 注释在中文 Windows 上会直接 UnicodeDecodeError / 语法错乱，
而在 Linux CI 上却一切正常——即"只在开发机上坏"的那类缺陷。已实际踩到一次
（alembic.ini 写中文注释导致 `alembic history` 崩溃），故钉成测试。
"""

from __future__ import annotations

from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVICE_ROOT.parent


_SKIP_PARTS = ("site-packages", ".venv", "__pycache__", "\\.uv-cache")


def _candidates() -> list[Path]:
    paths = [*SERVICE_ROOT.rglob("*.ini"), *REPO_ROOT.glob("scripts/**/*.ps1")]
    unique: dict[str, Path] = {}
    for path in paths:
        if not any(part in str(path) for part in _SKIP_PARTS):
            unique[str(path.resolve())] = path
    return list(unique.values())


def test_guard_actually_scans_something():
    """防退化：候选集为空时本测试会静默通过，等于没守卫（已踩过一次）。"""
    scanned = {p.name for p in _candidates()}
    assert "alembic.ini" in scanned, f"未扫到 alembic.ini，候选={sorted(scanned)}"
    assert any(name.endswith(".ps1") for name in scanned), f"未扫到任何 .ps1，候选={sorted(scanned)}"


def test_locale_parsed_files_are_pure_ascii():
    offenders: list[str] = []
    for path in _candidates():
        try:
            path.read_bytes().decode("ascii")
        except UnicodeDecodeError:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        f"以下文件含非 ASCII 字节，会被按区域编码(GBK)解析而在中文 Windows 上崩溃: {offenders}；请把注释改为英文"
    )
