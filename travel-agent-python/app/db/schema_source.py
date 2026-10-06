"""Flyway 迁移 SQL 的读取入口——**本仓 `app/db/migrations/sql/` 是唯一真相源**。

历史：双跑期（PLAN v3.0 M0–M7）这里先找 `travel-backend-java/`、再退本仓副本，
以便 Java 的 Flyway 与 Python 的 Alembic 读同一份文件。Java 已退役、SQL 已整体
搬进本仓，那条"先看 Java"的分支因此恒为假——但**不是无害的假**：只要有人从历史里
restore 一份 `travel-backend-java/`（哪怕是稀疏克隆或误留的构建产物），迁移真相源
就会静默切走，本仓的 V*.sql 与 alembic 版本表立刻各说一套（R3-5 删除该分支）。

追加迁移只能新增 `V<n>__*.sql` + 对应 `versions/000n_*.py`，已入库的文件不许改（INV-3）。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.common.config import BASE_DIR

# 唯一落点。`IN_REPO_MIGRATION_DIR` 这个别名保留是给解析器与测试表达"本仓这份"之意的
IN_REPO_MIGRATION_DIR = BASE_DIR / "app" / "db" / "migrations" / "sql"

_VERSION_RE = re.compile(r"^V(\d+)_")


def resolve_migration_dir() -> Path:
    """迁移 SQL 目录（历史上会因 Java 侧存在而切换，现在恒为本仓目录）。"""
    return IN_REPO_MIGRATION_DIR


# 既有引用面（env.py 与全部 versions/000n_*.py 的错误文案）用的就是这个名字
SQL_MIGRATION_DIR = resolve_migration_dir()


def migration_files() -> list[Path]:
    """按 Flyway 版本号升序返回 V*.sql。"""

    def key(path: Path) -> tuple[int, str]:
        m = _VERSION_RE.match(path.name)
        return (int(m.group(1)) if m else 0, path.name)

    directory = resolve_migration_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("V*.sql"), key=key)


def read_all() -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in migration_files()]


def _statements_in_range(lo: int | None, hi: int | None) -> list[tuple[str, str]]:
    """版本号在 [lo, hi]（含端点，None 表示不限）的迁移语句，按版本序展开。"""
    out: list[tuple[str, str]] = []
    for path in migration_files():
        matched = _VERSION_RE.match(path.name)
        number = int(matched.group(1)) if matched else 0
        if lo is not None and number < lo:
            continue
        if hi is not None and number > hi:
            continue
        out.extend((path.name, statement) for statement in split_statements(path.read_text(encoding="utf-8")))
    return out


def statements_upto(max_version: int) -> list[tuple[str, str]]:
    """版本号 ≤ max_version 的迁移语句——revision 只应用自己负责的那一段。

    为什么 revision 要能按版本圈定文件：`0001` 原先把「全部 V*.sql」一把执行，
    加了 V2 之后就会与「0002 再执行一次 V2」重复（空库直接炸）。详见各 revision
    的 docstring。
    """
    return _statements_in_range(None, max_version)


def statements_between(min_version: int, max_version: int) -> list[tuple[str, str]]:
    """版本号落在 [min_version, max_version] 的迁移语句。"""
    return _statements_in_range(min_version, max_version)


def split_statements(sql: str) -> list[str]:
    """按分号切句，但**分号只在字符串字面量之外才是语句结束符**。

    两个必要性：
    1. pymysql 默认不开 CLIENT_MULTI_STATEMENTS，整文件一次 execute 会报错；
    2. DDL 的 COMMENT 文本里可能出现分号（如 '状态 1-正常; 0-禁用'），
       朴素 split(";") 会把一条 CREATE TABLE 切成两半，产生难以定位的语法错误。
    """
    statements: list[str] = []
    buf: list[str] = []
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if in_string:
            buf.append(ch)
            if ch == "'":
                # SQL 里单引号转义是 ''：遇到成对引号则仍在字符串内
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    buf.append(sql[i + 1])
                    i += 2
                    continue
                in_string = False
            i += 1
            continue
        if ch == "'":
            in_string = True
            buf.append(ch)
        elif ch == ";":
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []
        elif ch == "-" and "".join(buf).strip() == "" and sql[i : i + 2] == "--":
            # 行注释：跳到行尾（仅在缓冲区为空白时才当注释起始，避免误吃列内文本）
            newline = sql.find("\n", i)
            i = len(sql) if newline == -1 else newline
            buf = []
            continue
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements
