"""目的地币种约束与大面额本币兜底（2026-10-12：东京行程 cost 混入日元的回归）。"""

from app.agent.data import fx
from app.agent.generation.rules.budget import budget_clause


def test_destination_currency_matches_keyword_and_domestic_is_none() -> None:
    assert fx.destination_currency("东京") == "JPY"
    assert fx.destination_currency("日本东京都") == "JPY"
    assert fx.destination_currency("首尔") == "KRW"
    assert fx.destination_currency("巴黎") == "EUR"
    assert fx.destination_currency("杭州") is None
    assert fx.destination_currency(None) is None
    assert fx.destination_currency("  ") is None


def test_currency_clause_always_cny_and_names_local_rate_overseas() -> None:
    domestic = fx.currency_clause("成都")
    assert "人民币 CNY/RMB" in domestic and "目的地使用" not in domestic
    tokyo = fx.currency_clause("东京")
    assert "日元（JPY）" in tokyo and "1 JPY ≈ ¥0.048" in tokyo
    # 示例金额两位有效数字、换算结果与汇率自洽：3100 日元 ≈ 149 元
    assert "人均 3100 日元的一餐写 149" in tokyo
    assert "130000 韩元" in fx.currency_clause("首尔"), "小面额汇率也给出像样的本币示例"


def test_budget_clause_no_longer_carries_currency_rule() -> None:
    """币种口径单一定义在 fx.currency_clause：无预算行程也要约束，不能挂在预算句里。"""
    assert "日元" not in budget_clause(3000, 2, 3)
    assert budget_clause(None, 2, 3) == ""


def test_backstop_converts_implausible_yen_and_keeps_plausible_cny() -> None:
    items = [
        {"item_type": "hotel", "cost": 12000, "remark": "近地铁"},
        {"item_type": "food", "cost": 80},
        {"item_type": "food", "cost": 4000},
        {"item_type": "attraction", "cost": 700},
        {"item_type": "attraction", "cost": 0},
        {"item_type": "food", "cost": None},
        {"item_type": "transport", "cost": 9000},
    ]
    fx.backstop_item_costs(items, "东京")
    assert items[0]["cost"] == 576.0 and items[0]["remark"].startswith("近地铁；原估价 12000 为日元")
    assert items[1] == {"item_type": "food", "cost": 80}, "按人民币合理的值不动"
    assert items[2]["cost"] == 192.0
    assert items[3]["cost"] == 34.0
    assert items[4]["cost"] == 0 and "remark" not in items[4]
    assert items[5]["cost"] is None
    assert items[6]["cost"] == 9000, "无合理区间定义的类型不兜底"


def test_backstop_is_idempotent_and_skips_domestic_and_small_unit_currencies() -> None:
    once = [{"item_type": "food", "cost": 4000}]
    fx.backstop_item_costs(once, "东京")
    fx.backstop_item_costs(once, "东京")
    assert once[0]["cost"] == 192.0, "已换算的值落在合理区间，不会二次触发"
    for city in ("杭州", "巴黎"):
        row = [{"item_type": "hotel", "cost": 12000}]
        fx.backstop_item_costs(row, city)
        assert row[0]["cost"] == 12000, f"{city}：国内/小面额币种无从判别，只靠提示句"


def test_backstop_requires_converted_value_in_range() -> None:
    """按本币换算后仍离谱（低于合理下限）→ 不是本币，保持原值交给后续钳制。"""
    row = [{"item_type": "hotel", "cost": 7000}]
    fx.backstop_item_costs(row, "首尔")  # 7000 韩元 ≈ ¥36，低于酒店下限
    assert row[0]["cost"] == 7000
