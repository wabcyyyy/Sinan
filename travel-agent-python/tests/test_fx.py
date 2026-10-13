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
    assert "cost_currency 写 CNY" in domestic and "目的地使用" not in domestic
    tokyo = fx.currency_clause("东京")
    assert "日元（JPY）" in tokyo and "1 JPY ≈ ¥0.048" in tokyo
    # 示例金额两位有效数字、两种写法与汇率自洽：3100 日元 ≈ 149 元
    assert "cost=3100、cost_currency=JPY" in tokyo and "cost=149、cost_currency=CNY" in tokyo
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
    fx.normalize_item_costs(items, "东京")
    assert items[0]["cost"] == 576.0 and items[0]["remark"].startswith("近地铁；原估价 12000 为日元")
    assert items[1] == {"item_type": "food", "cost": 80, "cost_currency": None}, "按人民币合理的值不动"
    assert items[2]["cost"] == 192.0
    assert items[3]["cost"] == 34.0
    assert items[4]["cost"] == 0 and "remark" not in items[4]
    assert items[5]["cost"] is None
    assert items[6]["cost"] == 9000, "无合理区间定义的类型不兜底"


def test_backstop_is_idempotent_and_skips_domestic_and_small_unit_currencies() -> None:
    once = [{"item_type": "food", "cost": 4000}]
    fx.normalize_item_costs(once, "东京")
    fx.normalize_item_costs(once, "东京")
    assert once[0]["cost"] == 192.0, "已换算的值落在合理区间，不会二次触发"
    for city in ("杭州", "巴黎"):
        row = [{"item_type": "hotel", "cost": 12000}]
        fx.normalize_item_costs(row, city)
        assert row[0]["cost"] == 12000, f"{city}：国内/小面额币种无从判别，只靠提示句"


def test_backstop_requires_converted_value_in_range() -> None:
    """按本币换算后仍离谱（低于合理下限）→ 不是本币，保持原值交给后续钳制。"""
    row = [{"item_type": "hotel", "cost": 7000}]
    fx.normalize_item_costs(row, "首尔")  # 7000 韩元 ≈ ¥36，低于酒店下限
    assert row[0]["cost"] == 7000


def test_backstop_respects_explicit_cny_declaration() -> None:
    """条目写明人民币时不按金额大小猜币种（current-state-review 2026-10-12 复现：1500 → 72）。"""
    declared = {"item_type": "food", "cost": 1500, "remark": "人民币估算，含清酒套餐"}
    assert fx.backstop_item_cost(declared, "东京") is None
    assert declared["cost"] == 1500
    lower = {"item_type": "food", "cost": 1500, "why_this": "约 1500 rmb 的怀石料理"}
    assert fx.backstop_item_cost(lower, "东京") is None
    # ¥ 符号不算人民币证据（日元也用 ¥），仍按原判据换算
    yen_symbol = {"item_type": "food", "cost": 4000, "remark": "¥4000 左右"}
    assert fx.backstop_item_cost(yen_symbol, "东京") is not None
    assert yen_symbol["cost"] == 192


def test_declared_currency_converts_deterministically_regardless_of_magnitude() -> None:
    """有 cost_currency 声明就按汇率换算，不看金额大小（取代按金额猜币种）。"""
    items = [
        {"item_type": "food", "cost": 900, "cost_currency": "JPY"},  # 按旧判据 900 不离谱，不会换算
        {"item_type": "food", "cost": 1500, "cost_currency": "CNY"},  # 旧判据会把它当日元压成 72
        {"item_type": "hotel", "cost": 120, "cost_currency": "usd"},  # 小面额币种以前只能靠提示句
        {"item_type": "food", "cost": 3000, "cost_currency": "日元"},
        {"item_type": "food", "cost": 4000, "cost_currency": "人民币"},
    ]
    fx.normalize_item_costs(items, "东京")
    assert items[0]["cost"] == 43.0 and "原估价 900 日元" in items[0]["remark"]
    assert items[1]["cost"] == 1500 and "remark" not in items[1]
    assert items[2]["cost"] == 864.0, "声明的币种与目的地无关也照换"
    assert items[3]["cost"] == 144.0
    assert items[4]["cost"] == 4000
    assert all(item["cost_currency"] == "CNY" for item in items), "出口语义：cost 已是人民币"


def test_declared_currency_normalization_is_idempotent() -> None:
    row = [{"item_type": "food", "cost": 900, "cost_currency": "JPY"}]
    fx.normalize_item_costs(row, "东京")
    fx.normalize_item_costs(row, "东京")
    assert row[0]["cost"] == 43.0, "第二次看到 CNY 不再换算"
    assert row[0]["remark"].count("原估价") == 1


def test_unknown_or_missing_currency_falls_back_to_magnitude_backstop() -> None:
    unknown = [{"item_type": "food", "cost": 4000, "cost_currency": "XYZ"}]
    fx.normalize_item_costs(unknown, "东京")
    assert unknown[0]["cost"] == 192.0 and unknown[0]["cost_currency"] is None, "认不出的币种当未声明"
    no_cost = [{"item_type": "attraction", "cost": None, "cost_currency": "JPY"}]
    fx.normalize_item_costs(no_cost, "东京")
    assert no_cost[0]["cost"] is None and no_cost[0]["cost_currency"] is None
