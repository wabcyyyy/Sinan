"""schema 漂移检查：Flyway 迁移（V1 基线 + 全部 V*.sql）必须覆盖 Python 查询列（Java 实体列一半已归档跳过）。

用法（仓库任意位置）：
    uv run python scripts/check_schema_contract.py

校验口径（纯静态，不需要数据库）：
- 解析 db/migration 下**全部** V*.sql（按版本号升序），列集合取并集：
  CREATE TABLE 的列定义 + ALTER TABLE ... ADD COLUMN 新增的列；
- Python：app/agent/data/city_reference.py 查询的 city_geo / city_consumption 列
  （下方静态期望表，POI 库退役后 agent 层仅存的城市级数据面）必须在并集中存在；
- Java：各 @TableName 实体的字段（camelCase→snake_case）必须在对应表的并集列中；
  **Java 模块归档后这一半自动跳过并在输出里注明**（不会静默当全过）；
- 任何缺失即 exit 1 —— 新环境「启动即建表」后两端查询不会因缺列直接失败。

为什么不再只读 V1：V1 一经发布不得修改（Flyway checksum 校验），后续变更一律新增
V2__*.sql。若只解析 V1，给实体加字段而 ALTER 写进 V2 会被误报为漂移，等于把
「新增迁移」这条路堵死；迁移是 schema 的真相来源，就必须全量应用后再比对。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# 脚本直接用 `python scripts/…` 跑时 CWD 不在 sys.path，先补服务根目录才能复用 app 的解析器
_SERVICE_ROOT = ROOT / "travel-agent-python"
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

try:
    from app.db.schema_source import resolve_migration_dir
except ImportError:
    # 本脚本的定位是"零依赖静态检查"：CI 的 schema job 用裸 python3 调它（没装 uv 依赖），
    # 那种环境 import 不到 app 包。这里的兜底是同一套规则的极简副本，
    # tests/test_retirement_readiness.py 会断言两份实现给出同一个目录（防漂移）。
    def resolve_migration_dir() -> Path:  # type: ignore[misc]
        # R3-5：迁移真相源只有本仓一个落点，不设任何 Java 侧旁路（与 app 解析器同规则）
        return _SERVICE_ROOT / "app" / "db" / "migrations" / "sql"


# 与运行时同源：只有一个落点（R3-5，见 tests/test_retirement_readiness.py）
MIGRATION_DIR = resolve_migration_dir()
ENTITY_DIR = ROOT / "travel-backend-java/src/main/java/com/travel/backend/entity"

# city_reference（agent 层仅存的数据库面）查询列的静态期望：脚本改解析 SQL 字符串
# 太脆，列清单以本表为准——city_reference 增列时这里同步登记，缺列即门禁变红。
CITY_REFERENCE_TABLES: dict[str, set[str]] = {
    "city_geo": {"city_name", "country", "country_code", "lat", "lng", "is_domestic"},
    "city_consumption": {"city", "level", "meal_price", "transport_price", "hotel_price"},
}

_COLUMN_RE = re.compile(
    r"^\s*`?(\w+)`?\s+(?:BIGINT|VARCHAR|DECIMAL|INT|TINYINT|DATETIME|DATE|TIME|TEXT|LONGTEXT|CHAR|DOUBLE|FLOAT)",
    re.I | re.M,
)
_CREATE_TABLE_RE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\)\s*ENGINE", re.S | re.I)
_ALTER_TABLE_RE = re.compile(r"ALTER TABLE\s+`?(\w+)`?([^;]*);", re.S | re.I)
_ADD_COLUMN_RE = re.compile(r"\bADD\s+COLUMN\s+`?(\w+)`?", re.I)
# ADD 后面跟这些关键字都不是新增列，静默忽略即可。
_NON_COLUMN_ADD_RE = re.compile(r"\bADD\s+(COLUMN|INDEX|KEY|UNIQUE|CONSTRAINT|PRIMARY|FULLTEXT|SPATIAL)\b", re.I)


def migration_files() -> list[Path]:
    def version_of(path: Path) -> tuple[int, str]:
        m = re.match(r"V(\d+)_", path.name)
        return (int(m.group(1)) if m else 0, path.name)

    return sorted(MIGRATION_DIR.glob("V*.sql"), key=version_of)


def parse_migrations() -> tuple[dict[str, set[str]], list[str]]:
    """返回 {表名: 列集合} 与「未识别的 ADD 形态」告警列表。"""
    tables: dict[str, set[str]] = {}
    warnings: list[str] = []

    for f in migration_files():
        text = f.read_text(encoding="utf-8")

        for m in _CREATE_TABLE_RE.finditer(text):
            tables.setdefault(m.group(1).lower(), set()).update(c.lower() for c in _COLUMN_RE.findall(m.group(2)))

        for m in _ALTER_TABLE_RE.finditer(text):
            table, body = m.group(1).lower(), m.group(2)
            columns = {c.lower() for c in _ADD_COLUMN_RE.findall(body)}
            tables.setdefault(table, set()).update(columns)
            # 只认 ADD COLUMN / ADD INDEX|KEY|CONSTRAINT...；其余 ADD 形态提示出来，
            # 不让它静默漏过（例如手写 ADD (col INT) 这种不带 COLUMN 关键字的写法）。
            for add in re.finditer(r"\bADD\b(?! COLUMN)", body, re.I):
                tail = body[add.start() : add.start() + 24]
                if not _NON_COLUMN_ADD_RE.match(tail) and "COLUMN" not in tail.upper()[:12]:
                    warnings.append(f"{f.name}: 未识别的 ADD 形态，可能漏解析 → {tail.strip()!r}")

    return tables, warnings


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def check_python(tables: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    for table, columns in CITY_REFERENCE_TABLES.items():
        known = tables.get(table)
        if known is None:
            problems.append(f"迁移（V1+V*）缺少 city_reference 依赖表 {table}")
            continue
        problems += [f"{table} 缺列（city_reference 只读查询需要）: {c}" for c in sorted(columns) if c not in known]
    return problems


def check_java(tables: dict[str, set[str]]) -> list[str]:
    """Java 实体列 vs 迁移列。Java 模块归档后这一半自动失效（返回空），由 main 说明。"""
    if not ENTITY_DIR.is_dir():
        return []
    problems: list[str] = []
    for f in sorted(ENTITY_DIR.glob("*.java")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r'@TableName\("(\w+)"\)', text)
        if not m:
            continue
        table = m.group(1)
        known = tables.get(table)
        if known is None:
            problems.append(f"{f.name}: 迁移（V1+V*）缺少实体对应表 {table}")
            continue
        for fm in re.finditer(r"^\s*private\s+[\w<>,\s\.]+\s+(\w+)\s*;", text, re.M):
            col = snake(fm.group(1))
            if col not in known:
                problems.append(f"{f.name}: {table} 缺列 {col}")
    return problems


def main() -> int:
    files = migration_files()
    if not files:
        print(f"未找到迁移文件：{MIGRATION_DIR}")
        return 1
    tables, warnings = parse_migrations()
    problems = check_python(tables) + check_java(tables)
    if problems:
        print("schema 漂移（迁移 V1+V* 与两端代码不一致）：")
        for p in problems:
            print("  -", p)
        return 1
    for w in warnings:
        print("  !", w)
    java_half = "Python 只读列与 Java 实体列全部存在" if ENTITY_DIR.is_dir() else "Java 实体列对照已跳过（模块已删除）"
    print(
        f"OK: 应用 {len(files)} 个迁移文件（{files[0].name} … {files[-1].name}），覆盖 {len(tables)} 张表；{java_half}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
