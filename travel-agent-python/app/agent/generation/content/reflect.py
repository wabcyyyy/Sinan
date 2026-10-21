"""生成结果的自我校验（reflect）层：纯规则检查行程是否合理。

职责：
- validate_plans：检查每日时间冲突、营业时间覆盖、POI 间可达性、景点过多/行程过满，
  返回 (问题列表, 日志列表)；
- build_feedback：把校验问题拼成可回喂给 LLM 的修复反馈文本。

实现要点：
- 全部为无副作用的确定性规则，配合 workflow 的“生成 → 校验 → 修复”循环；
- 单日饱和度由阈值常量（MAX_DAILY_MINUTES / MAX_DAILY_ATTRACTIONS）控制。

依赖：rules.generation_core / rules.transfer_time（时间解析与转场判据的唯一实现，
2026-10-08 自本模块下沉——rules 是生成域最底层，生成后微调 fix_transfer_gaps
与本层校验共用同一判据而不必复制常量）/ route_service（估算源判定与容差）。
"""

from app.agent.data.route_service import (
    ROUTE_BUFFER_RATIO,
    ROUTE_ESTIMATE_TOLERANCE_MIN,
    ROUTE_FIXED_BUFFER_MIN,
    is_estimated,
)
from app.agent.generation.rules.constraint_checks import check_constraints
from app.agent.generation.rules.day_policy import (
    day_policy_for,
)
from app.agent.generation.rules.generation_core import (
    DINNER_SLOT_END_MIN,
    estimate_plans_total,
    has_double_lunch,
    meal_slot_of,
)
from app.agent.generation.rules.opening_hours import opening_checks
from app.agent.generation.rules.transfer_time import (
    estimate_transfer_minutes,
    item_end,
    item_start,
    parse_time,
)
from app.schemas.trip_requirements import TripRequirements

# 预算超支 issue 的稳定标记（单一真源）：整趟终检的调用方据此从合并 issues 里认出
# 预算项（BIZ-1），不许在别处再写字面量。
BUDGET_OVERAGE_MARK = "超出预算"

# 餐次过晚：餐厅大多已打烊，属于不可执行的安排，与"未安排景点"同级阻断交付
LATE_MEAL_MARK = "餐饮安排过晚"
_HARD_DELIVERY_TAGS = ("未安排任何景点", "安排过稀", LATE_MEAL_MARK, "闭馆日冲突", "开放时间不符")


def is_late_meal(start_time: str | None) -> bool:
    """餐饮开始时刻越过晚餐时段（> 21:30）或落在凌晨（< 05:00）。

    实测 2026-10-12：远处分店坐标触发转场顺延，晚餐被推到 22:19-23:49，校验此前不看
    餐次时段，当天仍判成功。只拦"过晚"，午后茶点/早餐等其它时段不在此判。
    """
    start = parse_time(start_time)
    return start is not None and (start > DINNER_SLOT_END_MIN or start < 5 * 60)


def hard_day_issues(
    plan: dict,
    requirements: TripRequirements | None = None,
    *,
    locked_names: set[str] | None = None,
    strict_budget: float | None = None,
    start_date: str | None = None,
) -> list[str]:
    """图的最后一次尝试与业务落库共用的单日交付闸，不放宽已有判据。"""
    day_no = plan["day_no"]
    if not plan.get("items"):
        return [f"第 {day_no} 天没有生成任何条目"]
    issues, _log = validate_plans([plan], requirements=requirements, start_date=start_date)
    structural = [issue for issue in issues if any(tag in issue for tag in _HARD_DELIVERY_TAGS)]
    report = check_constraints(
        plan,
        requirements,
        day_no,
        day_policy_for(requirements, day_no),
        locked_names=locked_names,
        strict_budget=strict_budget,
    )
    blockers = [f"第 {day_no} 天需求违例（{check.kind}）：{check.reason}" for check in report.blocking_checks]
    unknown = [
        f"第 {day_no} 天待确认（{check.kind}）：{check.reason}"
        for check in report.unknown_checks
        if check.reason and check.blocking
    ]
    return [*structural, *blockers, *unknown]


def _route_from_matrix(first: dict, second: dict, route_matrix: dict | None) -> dict | None:
    if not route_matrix:
        return None
    first_keys = (
        str(first.get("poi_id") or first.get("poi_name") or first.get("name") or ""),
        str(first.get("poi_name") or first.get("name") or ""),
    )
    second_keys = (
        str(second.get("poi_id") or second.get("poi_name") or second.get("name") or ""),
        str(second.get("poi_name") or second.get("name") or ""),
    )
    for first_key in first_keys:
        for second_key in second_keys:
            route = route_matrix.get((first_key, second_key))
            if route is not None:
                return route
    return None


def validate_plans(
    daily_plans: list[dict],
    route_matrix: dict | None = None,
    *,
    budget: float | None = None,
    persons: int = 1,
    consumption: dict | None = None,
    budget_overage_ratio: float = 0.08,
    requirements: "TripRequirements | None" = None,
    start_date: str | None = None,
) -> tuple[list[str], list[str]]:
    issues: list[str] = []
    log: list[str] = []
    # M3：预算房间口径——用户明确给出 rooms 则替代 ceil(persons/2) 猜测
    rooms = requirements.lodging.rooms if requirements is not None and requirements.lodging else None
    for plan in daily_plans:
        day_no = plan.get("day_no")
        items = plan.get("items") or []
        if not items:
            log.append(f"第 {day_no} 天无行程项")
            continue
        # M3（spec §8.2）：当日规则按有效需求派生——到达/返程日不要求凑满 240、
        # 慢游降景点上限、窗口钳制时长上限；无需求 = 普通全天（现状基线不变）
        policy = day_policy_for(requirements, int(day_no) if isinstance(day_no, int) else 1)
        if requirements is not None:
            report = check_constraints(plan, requirements, policy.day_no, policy)
            issues.extend(
                f"第 {day_no} 天需求检查（{check.kind}）：{check.reason}"
                for check in report.checks
                if check.blocking and check.status in ("violation", "unknown")
            )

        timed = [it for it in items if it.get("item_type") in ("attraction", "food")]
        timed.sort(key=item_start)
        for i in range(len(timed) - 1):
            prev, nxt = timed[i], timed[i + 1]
            prev_end = item_end(prev)
            next_start = item_start(nxt)
            if prev_end > next_start:
                issues.append(
                    f"第 {day_no} 天时间冲突：{prev.get('poi_name')}({prev.get('start_time')}-{prev.get('end_time')}) "
                    f"与 {nxt.get('poi_name')}({nxt.get('start_time')}) 重叠"
                )
                continue
            route = _route_from_matrix(prev, nxt, route_matrix)
            if route is not None:
                try:
                    required_transfer = max(int(route.get("duration_min") or 0), 0)
                except (AttributeError, TypeError, ValueError):
                    required_transfer = None
                route_source = str(route.get("source") or "route-service")
            else:
                required_transfer = estimate_transfer_minutes(prev, nxt)
                route_source = "coordinate-estimate"
            available_gap = next_start - prev_end
            # 坐标估算已含两层保守缓冲（下限 17 分钟），1-2 分钟级裕量差不再判不足
            # （P2 空天链路前件）；真实路线源是测量口径，维持严格判定。
            estimated = is_estimated(route_source)
            tolerance = ROUTE_ESTIMATE_TOLERANCE_MIN if estimated else 0
            if required_transfer is not None and available_gap + tolerance < required_transfer:
                source_label = "坐标估算" if estimated else "真实路线"
                buffer_note = (
                    ""
                    if not estimated
                    else (f"（含 {ROUTE_FIXED_BUFFER_MIN} 分钟固定缓冲和 {ROUTE_BUFFER_RATIO:.0%} 容错）")
                )
                issues.append(
                    f"第 {day_no} 天路线时间不足：{prev.get('poi_name')} → {nxt.get('poi_name')} "
                    f"仅留 {available_gap} 分钟，按{source_label}需约 {required_transfer} 分钟{buffer_note}"
                )

        checks = opening_checks(plan, start_date)
        issues.extend(f"第 {day_no} 天 {c['poi_name']} {c['reason']}" for c in checks if c["status"] == "violation")
        pending = [c["poi_name"] for c in checks if c["status"] == "unknown"]
        if pending:
            log.append(f"开放安排待确认：{'、'.join(pending)}；缺少完整开放资料或规则存在条件，出发前核实")

        attractions = [it for it in items if it.get("item_type") == "attraction"]
        foods = [it for it in items if it.get("item_type") == "food"]
        active = [it for it in items if it.get("item_type") in ("attraction", "food")]
        # 负时长（end<start 的跨午夜脏数据）按 0 计，避免抵消其它项而掩盖超满。
        total = sum(max(0, item_end(it) - item_start(it)) for it in active)
        # 只有酒店/餐饮没有景点的行程不可交付：必须进反思循环修复，
        # 禁止以 READY_WITH_WARNINGS 交付。到达/返程日可用时间不足（不可游玩）
        # 时"抵达/返程安排"是合法日类型（spec §8.2），不算伤。
        if not attractions and items and policy.playable:
            issues.append(f"第 {day_no} 天未安排任何景点")
        if len(attractions) > policy.max_attractions:
            issues.append(f"第 {day_no} 天景点过多（{len(attractions)} 个，上限 {policy.max_attractions}）")
        if total > policy.max_daily_minutes:
            issues.append(f"第 {day_no} 天行程过满（约 {total} 分钟，上限 {policy.max_daily_minutes}）")
        # 空白过多：白天有效活动过短（城市游常见问题：只有 2 个点、大片空档）。
        # 到离日不设下限（min_active=0，按可用窗口验收）
        if attractions and total < policy.min_active_minutes:
            issues.append(
                f"第 {day_no} 天安排过稀（有效活动约 {total} 分钟，建议 ≥{policy.min_active_minutes} 分钟）；"
                "请根据地理邻近与用户偏好补充 1-2 个可衔接的景点/餐饮/体验，避免午后与傍晚大片空白"
            )
        if not foods and attractions:
            issues.append(f"第 {day_no} 天未安排餐饮")
        # 两顿午餐：午间窗口安排了 ≥2 家餐厅，应改为一午一晚
        if has_double_lunch(foods):
            lunch_names = [
                str(it.get("poi_name") or "") for it in foods if meal_slot_of(it.get("start_time")) == "lunch"
            ]
            issues.append(
                f"第 {day_no} 天出现两顿午餐（{('、'.join(lunch_names) or '多条餐饮')}），"
                "请将其中一餐改到晚餐时段（17:00 后），保证午、晚各至多一餐"
            )
        late_meals = [
            f"{it.get('poi_name')}({it.get('start_time')})" for it in foods if is_late_meal(it.get("start_time"))
        ]
        if late_meals:
            issues.append(
                f"第 {day_no} 天{LATE_MEAL_MARK}：{'、'.join(late_meals)} 开始时间晚于 21:30 或在凌晨，"
                "请把晚餐安排在 17:30-20:30，必要时减少前序景点或换同片区餐厅"
            )
        # 付费餐饮写 0：预算会失效
        for it in foods:
            cost = it.get("cost")
            try:
                cost_val = float(cost) if cost is not None else None
            except (TypeError, ValueError):
                cost_val = None
            if cost_val == 0:
                issues.append(
                    f"第 {day_no} 天餐饮「{it.get('poi_name')}」cost 为 0，请填写合理人均消费（免费/含在门票内除外）"
                )

    # 预算硬约束：估算合计超过用户预算一定比例时，要求换平价点/降酒店档。
    # M3：hard_cap 是严格上限（无容差）；target 保持容差与超额警告。
    if budget is not None and float(budget) > 0 and daily_plans:
        policy_mode = (
            requirements.budget_policy.mode if requirements is not None and requirements.budget_policy else None
        )
        ratio = 0.0 if policy_mode == "hard_cap" else max(float(budget_overage_ratio), 0.0)
        try:
            est = estimate_plans_total(
                daily_plans, persons=persons or 1, days=len(daily_plans), consumption=consumption, rooms=rooms
            )
            total = float(est.get("合计") or 0)
            limit = float(budget)
            over = total - limit
            if over > limit * ratio:
                issues.append(
                    f"估算总价约 ¥{total:.0f}，{BUDGET_OVERAGE_MARK} ¥{limit:.0f} 约 ¥{over:.0f}。"
                    "请压缩花费：优先更换高价酒店/餐饮为预算内选项，减少付费体验，"
                    "选择免费或低价景点，确保总花费不超过预算"
                )
        except Exception:
            pass

    if issues:
        log.append(f"发现 {len(issues)} 个问题：")
        log.extend(issues)
    else:
        log.append("校验通过：已知资料内无排程冲突；未知开放安排不计为已核实")
    return issues, log


def build_feedback(issues: list[str]) -> str:
    return "；".join(issues) if issues else ""
