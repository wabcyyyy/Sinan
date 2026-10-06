"""输出落地阶段的价格与预算。

酒店定价链：联网实时价 → 知识库基准价 × 季节系数（season_factor）估算。
实时价查询按酒店名缓存去重，并受 `settings.max_live_queries` /
`settings.max_live_food_queries` 预算约束——预算耗尽即停止外呼，回落估算价。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import ceil
from typing import Any

from app.agent.data.pricing import query_live_food_price, query_live_price
from app.agent.generation.rules.budget import clamp_meal_cost
from app.common.addons import addons
from app.common.config import settings
from app.common.model_registry import configured
from app.common.season import season_factor, season_label
from app.schemas.trip import GenerateRequest


def _append_remark(item: dict[str, Any], note: str) -> None:
    item["remark"] = f"{item['remark']}；{note}" if item.get("remark") else note


@dataclass
class PriceStage:
    """一次生成 run 的定价上下文：季节系数 + 实时价缓存与预算。"""

    req: GenerateRequest
    trip_date: date | None
    factor: float
    label: str
    live_cache: dict[str, dict | None] = field(default_factory=dict)
    food_live_cache: dict[str, dict | None] = field(default_factory=dict)
    live_budget: int = 0
    food_live_budget: int = 0

    @classmethod
    def create(cls, req: GenerateRequest) -> PriceStage:
        trip_date: date | None = None
        if req.start_date:
            try:
                trip_date = date.fromisoformat(req.start_date)
            except ValueError:
                trip_date = None
        # 不在生成请求中串行检索图片。该操作会对每个 POI 访问外部网络并显著
        # 放大首屏耗时；前端在卡片加载时通过 /api/amap/poi-photo 懒加载图片。
        return cls(
            req=req,
            trip_date=trip_date,
            factor=season_factor(trip_date),
            label=season_label(trip_date),
            live_budget=(
                settings.max_live_queries if settings.live_price_search and addons.is_enabled("live_price") else 0
            ),
            food_live_budget=(
                settings.max_live_food_queries
                if settings.live_food_price_search and addons.is_enabled("live_price")
                else 0
            ),
        )

    def price_hotel(self, item: dict[str, Any]) -> None:
        if item.get("item_type") != "hotel":
            return
        name = item.get("poi_name") or ""
        if name not in self.live_cache:
            if self.live_budget > 0 and configured("main"):
                self.live_budget -= 1
                self.live_cache[name] = query_live_price(self.req.city, name, self.req.start_date)
            else:
                self.live_cache[name] = None
        live = self.live_cache.get(name)
        raw_cost = item.get("cost")
        try:
            base = float(raw_cost) if raw_cost is not None else None
        except (TypeError, ValueError):
            base = None
        if base is not None and base == 0:
            base = None
        if live:
            # 联网拿到的是"当前挂牌价"，未来日期的季节差异再叠系数
            # （即使模型未写 cost / 写成 0，也必须应用实时价）
            item["cost"] = round(live["price"] * self.factor, 2)
            remark = f"联网实时价￥{live['price']:g}：{live['note']}"
            if self.factor != 1.0:
                remark += f"；按{self.label}系数×{self.factor}调整"
            _append_remark(item, remark)
        elif base is not None and self.factor != 1.0:
            item["cost"] = round(base * self.factor, 2)
            _append_remark(item, f"{self.label}估算：系数×{self.factor}（知识库基准价￥{base:g}）")
        elif base is None and item.get("cost") is None:
            # 无实时价且无基准价：保持缺省，由预算引擎/知识库回落
            return

    def price_food(self, item: dict[str, Any], meal_price: float | None) -> None:
        food_name = str(item.get("poi_name") or "")
        if settings.live_food_price_search and configured("main"):
            if food_name not in self.food_live_cache:
                if self.food_live_budget > 0:
                    self.food_live_budget -= 1
                    self.food_live_cache[food_name] = query_live_food_price(self.req.city, food_name)
                else:
                    self.food_live_cache[food_name] = None
            live_food = self.food_live_cache.get(food_name)
            if live_food and live_food.get("price"):
                item["cost"] = float(live_food["price"])
                _append_remark(item, f"联网实时价￥{live_food['price']:g}：{live_food.get('note') or ''}".rstrip("："))
        new_cost, clamp_note = clamp_meal_cost(
            item.get("cost"),
            meal_price,
            hard_ratio=settings.meal_price_hard_cap_ratio,
            soft_ratio=settings.meal_price_soft_cap_ratio,
        )
        if clamp_note:
            item["cost"] = new_cost
            _append_remark(item, clamp_note)

    def recompute_budget(
        self,
        budget_source: dict | None,
        daily_plans: list,
        hotel_total: float,
        attraction_total: float,
        consumption: dict,
    ) -> dict:
        """预算只把 LLM/候选池预算当作初始估计，最终按本次实际选中的 POI 重算，
        避免"候选平均票价"与用户看到的具体景点不一致。
        """
        req = self.req
        budget_estimate = dict(budget_source or {})
        budget_estimate["门票"] = round(attraction_total * req.persons, 2)
        meal_price = float(consumption.get("meal_price", 60.0))
        transport_price = float(consumption.get("transport_price", 35.0))
        # 餐饮按当日选中餐饮条目的人均价**加总**计（BIZ-5：与硬约束判定/落库明细
        # 同口径——此前「每天最贵一餐 × 2」会把三餐日高估、一餐日漏计，同段行程
        # 实测 840 vs 420 差一倍）；当日无价餐饮的日期回落到城市人均餐价 × 2 餐。
        meal_total = 0.0
        unpriced_days = 0
        for plan in daily_plans:
            day_food = sum(float(i.cost or 0) for i in plan.items if i.item_type == "food" and i.cost is not None)
            if day_food > 0:
                meal_total += day_food
            else:
                unpriced_days += 1
        budget_estimate["餐饮"] = round(meal_total * req.persons + meal_price * 2 * unpriced_days * req.persons, 2)
        budget_estimate["交通"] = round(transport_price * len(daily_plans) * req.persons, 2)
        rooms = ceil(max(req.persons, 1) / 2)
        if hotel_total > 0:
            budget_estimate["酒店"] = round(hotel_total * rooms, 2)
        return budget_estimate

    def price_note(self) -> str | None:
        sources = [v["note"] for v in self.live_cache.values() if v]
        if sources:
            return "酒店价格来源：" + "；".join(dict.fromkeys(sources))
        if self.factor != 1.0:
            return f"酒店为{self.label}估算（系数×{self.factor}），未获取到联网实时价"
        return None
