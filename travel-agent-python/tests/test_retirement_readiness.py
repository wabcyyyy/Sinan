"""Java 退役就绪度：删掉 Java 模块之后，Python 侧与门禁脚本仍必须自洽。

为什么值得单独钉：退休动作最容易以两种方式静默出事——
1. **运行时找不到文件**：SQL 真相源与印刷字体今天都住在 Java 模块里，直接删目录，
   服务要到"启动迁移"或"导出 PDF"时才炸；
2. **门禁自己先红**：`check_schema_contract.py` 以"Java 目录必须存在"为前提，归档当天
   CI 会因为脚本读不到目录而失败。

所以两处路径都改成"Java 侧优先 + 本仓副本兜底"的解析器，归档因此是**一次纯文件搬迁**；
脚本在"Java 侧不存在"时给出明确语义（不是静默跳过）：schema 契约输出里注明跳过了实体列那一半。

（2026-09-18 自检整改 R0-5）`scripts/check_endpoint_coverage.py` 已删除：Java 目录消失后
它只剩"打印 OK 并 return 0"一条路径，两个统计函数永不被调用——一个恒真的门禁比没有门禁更糟，
因为它让人以为"端点残留已被机检"。端点面的真判据改由契约漂移 + 离线 TestClient 用例承担。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.common import config
from app.db import schema_source

BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPTS = BASE_DIR / "scripts"


def _load_script(name: str, file_name: str | None = None):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{file_name or name}.py")
    assert spec is not None and spec.loader is not None, "被加载脚本与仓同行，spec 必然可解析"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_a_stray_java_tree_never_switches_the_migration_truth_source(tmp_path) -> None:
    """R3-5：迁移 SQL 只有一个落点。

    旧解析器"先看 Java、再退本仓"，意味着任何人 restore 一份 `travel-backend-java/`
    （稀疏克隆、误留的构建产物、考古脚本）都会让启动迁移静默改读另一份 SQL——
    本仓的 V*.sql 与 alembic 版本表立刻各说一套。这里钉住它不再有任何旁路。
    """
    java_dir = tmp_path / "travel-backend-java" / "src" / "main" / "resources" / "db" / "migration"
    java_dir.mkdir(parents=True)
    (java_dir / "V99__stray.sql").write_text("SELECT 1;", encoding="utf-8")

    assert schema_source.resolve_migration_dir() == schema_source.IN_REPO_MIGRATION_DIR
    assert not hasattr(schema_source, "JAVA_MIGRATION_DIR"), "Java 侧旁路必须彻底删除，不留常量"
    # 解析出来的文件全部来自本仓，且版本号严格升序
    names = [p.name for p in schema_source.migration_files()]
    assert names and all("migrations" in str(p) for p in schema_source.migration_files())
    assert names == sorted(names, key=lambda n: int(n.split("_")[0].lstrip("V")))


def test_font_default_resolves_to_the_in_repo_copy(monkeypatch, tmp_path) -> None:
    service_root = tmp_path / "repo" / "travel-agent-python"
    monkeypatch.setattr(config, "BASE_DIR", service_root)

    # Java 模块已删除：字体全仓只有本仓一份；历史位置即使残留也不再回读
    java_font = tmp_path / "repo" / "travel-backend-java" / "src" / "main" / "resources" / "fonts" / "simhei.ttf"
    java_font.parent.mkdir(parents=True)
    java_font.write_bytes(b"font")
    assert config._default_font_path() == str(service_root / "app" / "resources" / "fonts" / "simhei.ttf")


def test_schema_contract_notes_the_skipped_java_half(monkeypatch, tmp_path, capsys) -> None:
    module = _load_script("check_schema_contract")
    monkeypatch.setattr(module, "ENTITY_DIR", tmp_path / "archived")

    assert module.main() == 0
    output = capsys.readouterr().out
    assert "OK:" in output
    # 跳过必须写在输出里：不能让人以为"两端都对齐"
    assert "跳过" in output and "已删除" in output


def test_script_fallback_matches_the_app_resolver(monkeypatch) -> None:
    """CI 的 schema job 用裸 python3 跑这个脚本（没装依赖 → import 不到 app 包），
    脚本里那份极简兜底必须与 app 侧解析器给出同一个目录，否则归档当天两边打架。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "app" or name.startswith("app."):
            raise ImportError("simulated dependency-free CI job")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    module = _load_script("check_schema_contract_nodeps", "check_schema_contract")
    assert schema_source.resolve_migration_dir() == module.MIGRATION_DIR
