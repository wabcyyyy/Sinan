"""初版落库的「不可交付」硬伤门禁（2026-09-30 评审遗留：过稀初版不得进完成态）。

评审实录：行程 #7 DAY04 初版仅 2 条目，校验已报「过稀+无餐饮」却标 SUCCEEDED
展示，靠异步重生成才自愈。本模块把整段流式后置终检（`_revalidate_stream_days`）
的同一套 validate_plans 口径前移到落库瞬间：命中硬伤的天标 PENDING 而非
SUCCEEDED——条目照写（是真实产出，用户看得到、可手动重生成），但"完成"
不允许冒充。complete_trip 汇总为 PARTIAL，详情面 qualityStatus=DRAFT 如实呈现。

独立成模块的原因：day_persistence 因此突破 400 行规模棘轮，且质量口径与
事务写入本就是两个关注点。硬伤集 = 无景点/过稀/空天——"结构上没法出发"；
无餐饮不在此列（半日无餐仍可交付，reflect 以警告呈现，评审案例里真正不可
交付的是过稀本身）；时间冲突/预算/过满同为软问题（reflect 已尽力修复，
剩余项走质量告警）。
"""

from __future__ import annotations

from typing import Any

from app.agent import check_constraints, day_policy_for, validate_plans
from app.schemas.trip import DailyPlan
from app.schemas.trip_requirements import TripRequirements

_HARD_DELIVERY_TAGS = ("未安排任何景点", "安排过稀")


def _rule_item(item: Any) -> dict[str, Any]:
    """TripItem → validate_plans 规则键（与 itinerary_generation._item_rule_dict 同口径，源是 wire 对象）。"""
    return {
        "item_type": item.item_type,
        "poi_name": item.poi_name,
        "start_time": item.start_time,
        "end_time": item.end_time,
        "duration_min": item.duration_min,
        "open_time": item.open_time,
        "cost": float(item.cost) if item.cost is not None else None,
        "latitude": float(item.latitude) if item.latitude is not None else None,
        "longitude": float(item.longitude) if item.longitude is not None else None,
    }


def hard_delivery_blockers(
    day_no: int,
    plan: DailyPlan,
    requirements: TripRequirements | None = None,
    locked_names: set[str] | None = None,
    strict_budget: float | None = None,
) -> list[str]:
    """单天初版的不可交付硬伤清单；空=可交付。空天同样算硬伤。

    M3（spec §8.2/8.3）双轨：
    - 结构伤（日型感知）：无景点（可玩日）/过稀（全天）——validate_plans 按
      DayPolicy 判定，到达/返程日不可游玩时"抵达/返程安排"合法；
    - 需求违例（ConstraintReport）：指定日必去/排除点类别/明确窗口/锁定条目/
      strict 预算/时间冲突——判 status/blocking，不再靠中文子串。
    """
    if not plan.items:
        return [f"第 {day_no} 天没有生成任何条目"]
    plan_dict = {"day_no": day_no, "items": [_rule_item(item) for item in plan.items]}
    issues, _log = validate_plans([plan_dict], requirements=requirements)
    structural = [issue for issue in issues if any(tag in issue for tag in _HARD_DELIVERY_TAGS)]
    policy = day_policy_for(requirements, day_no)
    report = check_constraints(
        plan_dict, requirements, day_no, policy, locked_names=locked_names, strict_budget=strict_budget
    )
    constraint_blockers = [
        f"第 {day_no} 天需求违例（{check.kind}）：{check.reason}" for check in report.blocking_checks
    ]
    unknown = [
        f"第 {day_no} 天待确认（{check.kind}）：{check.reason}" for check in report.unknown_checks if check.reason
    ]
    # unknown 不阻断（spec：关键 unknown 提示待确认，非阻断事实带警告交付），
    # 但要进 error 备注让上游/用户看得到
    return [*structural, *constraint_blockers, *unknown]
