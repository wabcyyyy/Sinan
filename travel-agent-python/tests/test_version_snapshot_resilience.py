"""版本快照失败不得把已提交的业务写说成失败（R2-3 的后半句）。

`itinerary_version.create_snapshot` 在 `version_no` 撞唯一索引 3 次后抛
`ApiError(500)`。写在**前面**的快照抛 500 是诚实的（那笔写还没发生）；写在
**后面**的快照抛 500，行已经落库、用户却看到"保存失败"，于是再点一次 ——
多一条重复行程项。这是数据问题，不是提示问题。

拆分之前没有任何测试碰过快照失败，所以这条行为既没被保护、也没人知道它存在。
"""

from __future__ import annotations

from contextlib import nullcontext

import pytest
from sqlalchemy.exc import IntegrityError

from app.common.envelope import ApiError
from app.services import itinerary_version


@pytest.fixture(autouse=True)
def no_real_session(monkeypatch):
    monkeypatch.setattr(itinerary_version, "session_scope", lambda: nullcontext(object()))


def test_create_snapshot_still_raises_for_pre_write_sites(monkeypatch) -> None:
    """3 次冲突后照旧抛错：写之前的快照失败时，500 是对的（那笔写没发生）。"""

    def _boom(*_args, **_kwargs):
        raise IntegrityError("INSERT INTO itinerary_version", {}, OSError("dup key"))

    monkeypatch.setattr(itinerary_version, "_write_snapshot", _boom)
    with pytest.raises(ApiError) as raised:
        itinerary_version.create_snapshot(1, 101, "add_item", "新增行程项前快照")
    assert raised.value.status == 500


def test_post_write_snapshot_failure_is_not_fatal(monkeypatch) -> None:
    """写之后的快照失败只 WARN：行已经落库，报 500 会诱导用户重试而写出重复项。

    这里刻意 spy `logger.warning` 而不是用 `caplog`：caplog 依赖传播与全局日志状态，
    跑全量时会被先前用例影响（本用例单独跑绿、门禁里红过一次），而"有没有留下痕迹"
    本来就该由这一条日志调用本身来断言。
    """
    attempts: list[tuple] = []
    warnings: list[str] = []

    def _conflict(*args):
        attempts.append(args)
        raise ApiError(500, "版本快照写入冲突，请稍后重试")

    monkeypatch.setattr(itinerary_version, "create_snapshot", _conflict)
    monkeypatch.setattr(
        itinerary_version.logger, "warning", lambda msg, *a: warnings.append(msg % a if a else str(msg))
    )
    itinerary_version.record_snapshot_or_log(1, 101, "add_item", "新增行程项完成")  # 不得抛
    assert attempts == [(1, 101, "add_item", "新增行程项完成")], "没有真的去落快照"
    assert any("add_item" in w for w in warnings), "静默吞掉：版本历史缺一条却没有任何痕迹，故障查不出来"
