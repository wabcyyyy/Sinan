"""预算档位与约束句、餐价钳制（G-2.2 自 generators 拆出）。

职责：
- budget_tier：预算 → (档位名, 规划指引, 人均每天预算)；
- budget_clause：预算约束句（无预算返回空串——调用方按空串跳过注入）；
- clamp_meal_cost：餐饮单价相对城市人均餐价的硬顶/软顶钳制。

依赖：无（纯函数，最底层）。
"""

TIER_KEYWORDS = {
    "经济型": ("经济",),
    "舒适型": ("舒适", "中端"),
    "高档型": ("高端", "高档"),
    "豪华型": ("高端", "五星", "国宾", "地标"),
    "奢华型": ("国宾", "地标", "五星", "百年"),
}


def budget_tier(budget: float | None, persons: int, days: int) -> tuple[str, str, float]:
    """按人均每天预算划分消费档次。

    返回 (档次名, 预算指引, 人均每天金额)；预算缺失时返回空串，不进入 Prompt。
    """
    if not budget or budget <= 0 or persons <= 0 or days <= 0:
        return "", "", 0.0
    ppd = float(budget) / persons / days
    if ppd >= 1500:
        return (
            "奢华档",
            (
                "预算非常充裕：优先选择候选中档次最高、价格最高的酒店（五星/地标级），"
                "餐饮安排高客单的名店，可纳入高价值付费体验项目，不要为了省钱降低标准。"
            ),
            ppd,
        )
    if ppd >= 800:
        return "高档", "预算充裕：优先选择高档酒店与品质餐饮，可适当安排付费体验项目。", ppd
    if ppd >= 400:
        return "舒适", "预算适中：兼顾品质与性价比，选择舒适档酒店与口碑餐饮。", ppd
    if ppd >= 150:
        return "经济", "预算有限：优先选择性价比高的点位与经济型住宿。", ppd
    return "节俭", "预算紧张：尽量选择免费或低价景点、平价餐饮与经济住宿，估算总花费不要超过预算。", ppd


def budget_clause(budget: float | None, persons: int, days: int) -> str:
    """生成给 LLM 的预算约束句；无预算时返回空串。"""
    label, guidance, ppd = budget_tier(budget, persons, days)
    if not label or budget is None:
        return ""
    return (
        f"预算要求（硬性）：总预算 ¥{float(budget):g}，{persons} 人 {days} 天，人均每天约 ¥{ppd:.0f}，"
        f"按「{label}」标准规划——{guidance}"
        "酒店与餐饮的选择必须与该预算档次匹配；"
        "全程估算总价不得超过总预算，禁止为凑必去点排出明显超支的豪华组合；"
        "预算紧张时优先免费/低价景点与平价餐饮。"
    )


def clamp_meal_cost(
    cost: float | None, meal_price: float | None, *, hard_ratio: float = 8.0, soft_ratio: float = 4.0
) -> tuple[float | None, str | None]:
    """餐饮单价相对城市人均餐价钳制，抑制「一兰 1200」这类离谱估值。

    - cost 非正：原样返回（由上层回落）
    - cost > meal_price * hard_ratio：压到 meal_price * soft_ratio
    - cost > meal_price * soft_ratio：压到 meal_price * soft_ratio * 0.75
    返回 (新 cost, 备注) 或 (原 cost, None)
    """
    try:
        c = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        return cost, None
    try:
        m = float(meal_price) if meal_price is not None else None
    except (TypeError, ValueError):
        m = None
    if c is None or c <= 0 or m is None or m <= 0:
        return c, None
    hard = m * max(float(hard_ratio), 1.0)
    soft = m * max(float(soft_ratio), 1.0)
    if c > hard:
        new_c = round(soft, 2)
        return new_c, f"餐饮价超出城市均价约{int(hard_ratio)}倍，已按人均约¥{m:g}钳制为¥{new_c:g}"
    if c > soft:
        new_c = round(soft * 0.75, 2)
        return new_c, f"餐饮价偏高，已按城市人均¥{m:g}参考价调整为¥{new_c:g}"
    return c, None
