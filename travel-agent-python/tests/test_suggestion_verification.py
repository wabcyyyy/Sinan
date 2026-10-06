"""备选池批量后验证（PLAN-A1 G6-B）的行为钉。

`verify_suggestion_rows` 是 24-40 个"AI 备选"名字唯一一次被外部数据源检验的地方：
主行程项在生成时已过落地/反思两道判定，备选池此前从未被问过"真的存在吗"。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.grounding import existence
from app.agent.grounding.existence import NOT_FOUND, VERIFIED, ResolveResult
from app.agent.grounding.suggestion_grounding import verify_suggestion_rows

VERDICTS: dict[str, ResolveResult] = {
    "已知存在": ResolveResult(
        state=VERIFIED,
        provider="opentripmap",
        name="已知存在",
        external_id="xid-9",
        latitude=30.25,
        longitude=120.16,
        address="杭州市西湖区",
    ),
    "在别的城市": ResolveResult(
        state="unknown", provider="nominatim", name="在别的城市", out_of_area=True, reason="resolved_out_of_area"
    ),
    "收费源说没有": ResolveResult(
        state=NOT_FOUND, provider="google_places", authoritative_negative=True, reason="provider_empty"
    ),
    "免费源说没有": ResolveResult(state=NOT_FOUND, provider="nominatim", reason="provider_empty"),
    "查不动": ResolveResult.unknown("provider_unavailable"),
}


@pytest.fixture(autouse=True)
def _stubbed_resolve(monkeypatch):
    def _resolve(name: str, city: str) -> Any:
        return VERDICTS.get(name, ResolveResult.unknown("not_in_table"))

    monkeypatch.setattr(existence, "resolve_poi", _resolve)
    yield


def _row(name: str, **overrides) -> dict:
    row = {"name": name, "category": "attraction", "latitude": None, "longitude": None}
    row.update(overrides)
    return row


def test_fills_coords_drops_conflicts_and_keeps_unresolved():
    rows = [_row(name) for name in VERDICTS]
    kept, stats = verify_suggestion_rows(rows, "杭州")

    assert [row["name"] for row in kept] == ["已知存在", "免费源说没有", "查不动"]
    assert kept[0]["latitude"] == 30.25 and kept[0]["address"] == "杭州市西湖区"
    assert kept[0]["poiId"] == "xid-9"  # 库里形状是 camelCase
    assert stats == {"filled": 1, "dropped": 2, "unresolved": 2, "skipped": 0}


def test_rows_already_carrying_coords_are_not_asked_again():
    """候选池来的备选本就带真实坐标：再问一遍只是白吃解析预算。"""
    rows = [_row("楼外楼（孤山路总店）", latitude=30.25, longitude=120.13)]
    kept, stats = verify_suggestion_rows(rows, "杭州")
    assert kept == rows and stats["skipped"] == 1


def test_independent_budget_stops_after_the_cap():
    rows = [_row("已知存在") for _ in range(6)]
    kept, stats = verify_suggestion_rows(rows, "杭州", limit=2)
    assert stats["filled"] == 2
    assert stats["skipped"] == 4  # 预算用尽的行原样保留，绝不删
    assert len(kept) == 6


def test_snake_case_rows_keep_their_key_spelling():
    """生成态（snake_case）与库内态（camelCase）两种形状都必须能回填。"""
    row = {"name": "已知存在", "poi_id": None}
    kept, _stats = verify_suggestion_rows([row], "杭州")
    assert kept[0]["poi_id"] == "xid-9" and "poiId" not in kept[0]


def test_default_cap_comes_from_config():
    """默认上限是配置项（后台富化的独立预算），不是函数里的魔数。"""
    from app.common.config import settings

    rows = [_row("查不动") for _ in range(settings.suggestion_resolve_limit + 3)]
    _kept, stats = verify_suggestion_rows(rows, "杭州")
    assert stats["unresolved"] == settings.suggestion_resolve_limit
    assert stats["skipped"] == 3


def test_zero_zero_coords_are_treated_as_missing():
    """0/0 是缺失哨兵：这样的备选行仍要送去验证，不能被当成"已核实"。"""
    rows = [{"name": "已知存在", "latitude": 0.0, "longitude": 0.0}]
    kept, stats = verify_suggestion_rows(rows, "杭州")
    assert stats["filled"] == 1 and kept[0]["latitude"] == 30.25
