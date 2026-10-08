"""M7-b 迁移验证：启动即迁移（等价 Java 侧 Flyway 在 boot 跑 V*.sql）。

revision 自 S0-13 起按文件拆分（0001 管 V1、0002 管 V2，见 E23）。这里钉住三条：

1. **既有库（有表、无 alembic_version）打点到基线、且继续 upgrade**：stamp 目标必须是
   `0001` 而不是 head——打到 head 会把基线之后的迁移整体跳过；打点后必须再 upgrade，
   才能把老库缺的 V2+ 补上。本用例把 `upgrade` 换成记录器：既断言它被调用，也证明
   除打点外没有任何 DDL 执行（真跑 V1 的 MySQL 语法在 SQLite 上会当场报错，
   测试通过本身就是"没跑 DDL"的证据）。
2. **重复启动幂等**：已有 alembic_version 的库不再打点，只走 upgrade。
3. **迁移 revision 自身拒绝重建**：若分支写错直接 upgrade，`0001_wrap_flyway_baseline`
   会用一句可执行的报错拦住（宁可启动失败，也不要"以为建了表其实没有"）。
"""

from __future__ import annotations

import importlib
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.db import migrate as db_migrate
from app.db import session as db_session
from app.db.models import Base


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    db_session.init_engine(engine, sessionmaker(bind=engine, expire_on_commit=False))
    yield engine
    db_session.init_engine(None, None)


def _head_revision() -> str:
    head = ScriptDirectory.from_config(db_migrate.alembic_config()).get_current_head()
    assert head is not None, "样例库已升级到基线，alembic_version 必有 head"
    return head


def _record_upgrade(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(db_migrate.command, "upgrade", lambda config, revision: calls.append(revision))
    return calls


def test_existing_database_stamps_baseline_then_upgrades(sqlite_db, monkeypatch) -> None:
    # 模拟"Flyway 已经建过表的既有库"：只有业务表，没有 alembic_version
    Base.metadata.create_all(sqlite_db)
    assert inspect(sqlite_db).has_table("sys_user")
    assert not inspect(sqlite_db).has_table("alembic_version")

    upgrade_calls = _record_upgrade(monkeypatch)
    assert db_migrate.ensure_schema() == "stamped"

    with sqlite_db.connect() as connection:
        recorded = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert recorded == "0001_wrap_flyway_baseline", "打点必须落在基线：打到 head 会跳过 V2+ 的 DDL，库看着最新实际缺列"
    assert upgrade_calls == ["head"], "打点后必须继续 upgrade，把基线之后的迁移补齐"
    assert _head_revision() != "0001_wrap_flyway_baseline", (
        "head 尚等于基线说明 V2 revision 不存在——本断言的防跳过后半段失去意义"
    )


def test_second_boot_skips_stamp_and_only_upgrades(sqlite_db, monkeypatch) -> None:
    Base.metadata.create_all(sqlite_db)
    with sqlite_db.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('0001_wrap_flyway_baseline')")

    stamp_calls: list[str] = []
    monkeypatch.setattr(db_migrate.command, "stamp", lambda config, revision: stamp_calls.append(revision))
    upgrade_calls = _record_upgrade(monkeypatch)

    assert db_migrate.ensure_schema() == "upgraded"
    assert stamp_calls == [], "已有版本登记的库不应重复打点"
    assert upgrade_calls == ["head"]
    with sqlite_db.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM alembic_version").scalar() == 1


def test_revision_refuses_to_rebuild_an_existing_schema(sqlite_db) -> None:
    """分支写错的兜底：既有的表 + 直接 upgrade 必须炸出可执行的指引，不能静默跳过。"""
    Base.metadata.create_all(sqlite_db)
    with pytest.raises(RuntimeError) as exc:
        command.upgrade(db_migrate.alembic_config(), "head")
    assert "stamp" in str(exc.value)
    assert "V1__baseline_schema.sql" in str(exc.value)


# ---------------------------------------------------------------------------
# V4 退役 poi_knowledge（append-only 迁移，INV-3）：head 接线 + V4 执行接线
# ---------------------------------------------------------------------------


def test_head_is_0014_and_linear() -> None:
    """结构化需求快照列（M1a）接在 city_geo 补种之后，历史链保持单一且完整。"""
    script = ScriptDirectory.from_config(db_migrate.alembic_config())
    assert script.get_current_head() == "0014_itinerary_planning_revision"
    # 从 head 沿 down_revision 走回基线，链路上每个 revision 都必须真实存在
    chain: list[str] = []
    for revision in script.walk_revisions(base="base", head="heads"):
        chain.append(revision.revision)
    assert chain == [
        "0014_itinerary_planning_revision",
        "0013_itinerary_requirements_json",
        "0012_city_geo_domestic_expansion",
        "0011_user_llm_gateway",
        "0010_gen_params",
        "0009_flight_quotes",
        "0008_item_feedback",
        "0007_template",
        "0006_collaboration",
        "0005_expense",
        "0004_retire_poi_knowledge",
        "0003_addons",
        "0002_atlas_share_covers",
        "0001_wrap_flyway_baseline",
    ], "head 之后的祖先链断了或分叉了：V2/V3 的执行接线被破坏"


def _stamp_and_create_legacy_tables(engine, revision: str) -> None:
    """把库置为「已到 0003 且 poi_knowledge/hotel_room_type 还在」的形态。

    V1 的 DDL 是 MySQL 方言，SQLite 跑不动，所以不真跑 0001-0003：直接手建
    alembic_version 并打点 0003，再建两张 V4 要删的表（覆盖 0004 负责的变更面）。
    """
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql(f"INSERT INTO alembic_version (version_num) VALUES ('{revision}')")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE poi_knowledge (id INTEGER PRIMARY KEY, name VARCHAR(200) NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE hotel_room_type (id INTEGER PRIMARY KEY, poi_id INTEGER NOT NULL)")


def test_upgrade_v4_drops_retired_tables(sqlite_db, capsys) -> None:
    """SQLite 验证 V4 的 DROP；V5 MySQL DDL 由独立 MySQL 演练验证。"""
    _stamp_and_create_legacy_tables(sqlite_db, "0003_addons")

    command.upgrade(db_migrate.alembic_config(), "0004_retire_poi_knowledge")

    inspector = inspect(sqlite_db)
    assert not inspector.has_table("poi_knowledge"), "V4 未生效：poi_knowledge 还在（0004 没被 head 触达）"
    assert not inspector.has_table("hotel_room_type"), "V4 未生效：hotel_room_type 还在"
    with sqlite_db.connect() as connection:
        recorded = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert recorded == "0004_retire_poi_knowledge", "upgrade 完成后版本必须推进到 0004"

    # V4 只负责 V4：从 0003 升级时打印的 applying 日志里不得出现 V1-V3（SQLite 本身
    # 也跑不动 MySQL 方言的 DDL，测试通过本身就是「没重跑旧迁移」的证据）
    output = capsys.readouterr().out
    applying = [line for line in output.splitlines() if line.startswith("[alembic] applying")]
    assert applying == ["[alembic] applying V4__retire_poi_knowledge.sql"], (
        f"从 0003 升级应只执行 V4 一个文件，实际：{applying}"
    )


def test_upgrade_head_applies_v4_statements_from_sql_truth(monkeypatch) -> None:
    """0004 的执行面钉住 SQL 真相源：语句必须来自 statements_between(4, 4) 逐条 op.execute，
    不得复制/改写 SQL（INV-3）；SQL 缺失时报错必须响亮而不是静默跳过。
    """
    module = importlib.import_module("app.db.migrations.versions.0004_retire_poi_knowledge")
    schema_source = importlib.import_module("app.db.schema_source")

    # 真调 schema_source.statements_between（保持对真相源文件的依赖），但在外面记录区间参数
    seen_ranges: list[tuple[int, int]] = []
    real_between = schema_source.statements_between

    def spy(lo: int, hi: int):
        seen_ranges.append((lo, hi))
        return real_between(lo, hi)

    monkeypatch.setattr(module, "statements_between", spy)

    executed: list[str] = []
    monkeypatch.setattr(
        module.op,
        "execute",
        lambda statement: executed.append(statement),
    )

    module.upgrade()

    assert seen_ranges == [(4, 4)], "0004 只准圈定 V4 区间，不得触碰 V1-V3"
    real_v4 = [s for _, s in real_between(4, 4)]
    assert executed == real_v4, "执行的语句必须逐条等于 V4__retire_poi_knowledge.sql 的内容"
    assert any("DROP TABLE" in s and "poi_knowledge" in s for s in executed)
    assert any("DROP TABLE" in s and "hotel_room_type" in s for s in executed)


def test_v4_revision_fails_loudly_when_sql_missing(monkeypatch) -> None:
    """SQL 目录缺失属于部署错误：0004 必须抛 RuntimeError，不能静默 0 语句通过。"""
    module = importlib.import_module("app.db.migrations.versions.0004_retire_poi_knowledge")
    monkeypatch.setattr(module, "statements_between", lambda lo, hi: [])

    execute = Mock()
    monkeypatch.setattr(module.op, "execute", execute)
    with pytest.raises(RuntimeError, match="V4"):
        module.upgrade()
    execute.assert_not_called()


def test_v4_revision_refuses_downgrade() -> None:
    module = importlib.import_module("app.db.migrations.versions.0004_retire_poi_knowledge")
    with pytest.raises(NotImplementedError, match="downgrade"):
        module.downgrade()


def test_v12_seeds_domestic_tourist_cities_from_sql_truth() -> None:
    """GROUND-1：V12 只补 city_geo 国内行（纯 INSERT、不建表），验收实测未收录的
    洛阳/长沙/南京必须在种子内；坐标纪律（不凭记忆填）一并钉住。"""
    schema_source = importlib.import_module("app.db.schema_source")

    statements = [s for _, s in schema_source.statements_between(12, 12)]
    assert statements, "V12 SQL 真相源缺失"
    joined = "\n".join(statements)
    for city in ("洛阳", "长沙", "南京", "重庆", "哈尔滨", "敦煌"):
        assert f"('{city}', '中国', 'CN', 1)" in joined, f"{city} 必须补种为国内城市"
    assert "CREATE TABLE" not in joined, "V12 是纯 INSERT 迁移，不得建表"
    assert "lat" not in joined and "lng" not in joined, "坐标一律 NULL——兜底坐标必须来自真实来源"
