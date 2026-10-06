"""ORM 模型与 Flyway SQL 的列级对齐（迁移期新增的第二道契约）。

Java 实体正在退役，Python 模型必须继续受同一套约束：模型有列而迁移 SQL 没有，
运行期就会以「Unknown column」的形式在真实请求里炸——所以在 CI 阶段先撞死。
"""

from __future__ import annotations

import re

from app.db.models import Base
from app.db.schema_source import migration_files, read_all, split_statements

_COLUMN_RE = re.compile(
    r"^\s*`?(\w+)`?\s+(?:BIGINT|VARCHAR|DECIMAL|INT|TINYINT|DATETIME|DATE|TIME|TEXT|LONGTEXT|CHAR|DOUBLE|FLOAT)",
    re.I | re.M,
)
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\)\s*ENGINE", re.S | re.I)
_ADD_COLUMN_RE = re.compile(r"\bADD\s+COLUMN\s+`?(\w+)`?", re.I)


_DROP_TABLE_RE = re.compile(r"DROP TABLE (?:IF EXISTS )+`?(\w+)`?", re.I)


def ddl_columns() -> dict[str, set[str]]:
    """迁移全量并集；DROP TABLE 会移除整表（V4 退役 poi_knowledge 后必须跟上，
    否则已退役的表会被误判为「尚无 ORM 模型」。）"""
    tables: dict[str, set[str]] = {}
    for _name, sql in read_all():
        for m in _CREATE_TABLE_RE.finditer(sql):
            tables.setdefault(m.group(1).lower(), set()).update(c.lower() for c in _COLUMN_RE.findall(m.group(2)))
        for m in re.finditer(r"ALTER TABLE\s+`?(\w+)`?([^;]*)", sql, re.S | re.I):
            tables.setdefault(m.group(1).lower(), set()).update(c.lower() for c in _ADD_COLUMN_RE.findall(m.group(2)))
        for m in _DROP_TABLE_RE.finditer(sql):
            tables.pop(m.group(1).lower(), None)
    return tables


def test_migration_sql_is_discoverable():
    files = [p.name for p in migration_files()]
    assert "V1__baseline_schema.sql" in files, "schema 真相源目录未被正确定位（双跑期指向 Java 资源目录）"


def test_every_orm_table_exists_in_sql():
    known = ddl_columns()
    missing = [t.name for t in Base.metadata.sorted_tables if t.name not in known]
    assert not missing, f"ORM 定义了 SQL 里不存在的表: {missing}"


def test_every_orm_column_exists_in_sql():
    known = ddl_columns()
    problems = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.name not in known.get(table.name, set())
    ]
    assert not problems, f"ORM 列在迁移 SQL 中不存在: {problems}"


def test_orm_covers_every_sql_table():
    """反向也要齐：SQL 里有表而模型没有，说明某个 Java Mapper 还没被移植。"""
    modeled = {t.name for t in Base.metadata.sorted_tables}
    unmodeled = sorted(t for t in ddl_columns() if t not in modeled)
    assert not unmodeled, f"以下表尚无 ORM 模型（迁移未完成）: {unmodeled}"


def test_split_statements_keeps_semicolon_inside_string_literal():
    sql = "CREATE TABLE t (\n  a INT COMMENT '状态 1-正常; 0-禁用'\n) ENGINE=InnoDB;\nCREATE TABLE u (a INT);"
    parts = split_statements(sql)
    assert len(parts) == 2, parts
    assert parts[0].startswith("CREATE TABLE t") and parts[0].endswith("ENGINE=InnoDB")
    assert parts[1] == "CREATE TABLE u (a INT)"


def test_split_statements_drops_leading_line_comments():
    parts = split_statements("-- V1 基线\n-- 第二行注释\nCREATE TABLE t (a INT);")
    assert parts == ["CREATE TABLE t (a INT)"], parts


def test_split_statements_handles_escaped_quotes():
    parts = split_statements("CREATE TABLE t (a VARCHAR(8) DEFAULT 'it''s;here');")
    assert len(parts) == 1 and "it''s;here" in parts[0], parts
