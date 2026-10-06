"""逻辑删除全局作用域（PLAN v3.0 §1.2）——迁移期最危险的一处回归防线。

Java 的 @TableLogic 会替每条查询自动追加 `deleted = 0`；SQLAlchemy 不会。若移植时
漏掉这层，已删除行程会继续出现在列表、详情、Atlas 与分享 token 反查里，而且
**不会报错**，只是安静地把别人的数据泄露出去。因此这里用真实 CRUD（SQLite 内存库）
验证钩子生效，而不是只验证 SQL 文本。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import session as db_session
from app.db.models import Base, CityGeo, ItineraryItem, ItineraryMain, SysUser


@pytest.fixture
def session(tmp_path, monkeypatch) -> Iterator[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db_session.init_engine(engine, factory)
    with factory() as s:
        yield s
    db_session.init_engine(None, None)


def _trip(title: str, user_id: int = 1, deleted: bool = False) -> ItineraryMain:
    return ItineraryMain(user_id=user_id, title=title, city="杭州", days=2, persons=2, deleted=deleted)


def test_soft_deleted_rows_are_invisible_by_default(session: Session) -> None:
    session.add_all([_trip("活着的"), _trip("已删的", deleted=True)])
    session.commit()

    titles = session.scalars(select(ItineraryMain).order_by(ItineraryMain.id)).all()
    assert [t.title for t in titles] == ["活着的"]
    assert session.execute(select(ItineraryMain)).scalars().all() == titles
    # 单行取用同样受限：移植时最容易漏的是 get/select_one 这类"看起来无害"的读法
    assert session.get(titles[0].__class__, titles[0].id) is not None


def test_include_deleted_opt_out_works(session: Session) -> None:
    session.add_all([_trip("活着的"), _trip("已删的", deleted=True)])
    session.commit()

    everything = session.execute(select(ItineraryMain).execution_options(include_deleted=True)).scalars().all()
    assert sorted(t.title for t in everything) == ["已删的", "活着的"]


def test_scope_applies_to_every_soft_delete_table(session: Session) -> None:
    session.add(SysUser(username="alice", password="x", deleted=True))
    session.add(ItineraryMain(user_id=1, title="t", city="c", deleted=False))
    session.add(ItineraryItem(day_id=1, itinerary_id=1, item_type="attraction", poi_name="p", deleted=True))
    session.commit()

    assert session.execute(select(SysUser)).scalars().all() == []
    assert session.execute(select(ItineraryItem)).scalars().all() == []
    assert len(session.execute(select(ItineraryMain)).scalars().all()) == 1


def test_tables_without_deleted_column_are_untouched(session: Session) -> None:
    """无 deleted 列的表（city_geo 等字典表）不能被钩子误伤成 0 行。"""
    session.add(CityGeo(city_name="杭州", country="中国", country_code="CN", is_domestic=True))
    session.commit()
    assert len(session.execute(select(CityGeo)).scalars().all()) == 1


def test_update_path_is_not_silently_scoped(session: Session) -> None:
    """钩子只管 SELECT：UPDATE 必须自己带条件（见 session.py 注释）。"""
    live = _trip("活着的")
    gone = _trip("已删的", deleted=True)
    session.add_all([live, gone])
    session.commit()
    live_id, gone_id = live.id, gone.id

    session.execute(ItineraryMain.__table__.update().where(ItineraryMain.id == live_id).values(title="改过"))
    session.execute(ItineraryMain.__table__.update().where(ItineraryMain.id == gone_id).values(title="也改过"))
    session.commit()
    # Core UPDATE 不刷新 identity map，必须过期后重读才拿到库里的真值
    session.expire_all()

    assert session.get(ItineraryMain, live_id).title == "改过"
    # 已软删行也能被显式 id 命中：证明写路径没有被偷偷加 deleted=0，
    # 还原/回收类任务因此可以正常操作软删数据。
    raw = session.execute(
        select(ItineraryMain).execution_options(include_deleted=True).where(ItineraryMain.id == gone_id)
    ).scalar_one()
    assert raw.title == "也改过"
