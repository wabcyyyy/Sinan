"""结构化需求 → 生成 prompt 的确定性指令块（M1b，spec §6.3）。

职责：
- has_structured_requirements：请求是否携带**有语义**的结构化需求（全默认 = 无）；
- requirements_directives_clause：TripRequirements → 硬约束指令块，整段与逐日
  两条生成入口共用同一渲染；与 requirements_clause（自然语言原话）并存且语义
  以本块为准。

逐日投影纪律：day_no 给定时日窗口只渲染该天、必去区分「全程必去」与「本日
必去」；整趟需求不做任何改写（同一 struct 全链路共享，逐日只是投影视图）。
措辞纪律（spec §5.1）：pace/交通偏好照实渲染，不捏造身体条件；
max_walk_minutes_per_leg 是明确标注为估算的策略默认，不称用户硬上限。

依赖：app.schemas.trip_requirements；无 agent 层依赖（prompt 基座同纪律）。
"""

from app.schemas.trip import GenerateDayRequest
from app.schemas.trip_requirements import EXCLUDED_CATEGORY_LABELS, TripRequirements, canonical_requirements_payload


def has_structured_requirements(req: GenerateDayRequest) -> bool:
    """请求是否携带**有语义**的结构化需求（全默认结构 = 无额外约束，视为没有）。"""
    return bool(canonical_requirements_payload(req.requirements_struct))


def _must_visit_lines(req_struct: TripRequirements, day_no: int | None) -> list[str]:
    """必去约束：全程/指定日的整趟渲染或 day_no 投影。"""
    lines: list[str] = []
    for fixed in req_struct.fixed_arrangements:
        if day_no is None or fixed.day_no == day_no:
            lines.append(
                f"固定安排：第{fixed.day_no}天 {fixed.start_time}–{fixed.end_time}"
                f"「{fixed.name}」（{fixed.item_type}），"
                "原名、类型、日期和时段均不得修改。"
            )
    if req_struct.required_places:
        lines.append("必去地点的 poi_name 使用以下原名，不缩写、不增加括号后缀；路线段与入口建议写在 remark 中。")
    trip_required = [p.name for p in req_struct.required_places if p.day_no is None]
    day_required = [p for p in req_struct.required_places if p.day_no is not None]
    if trip_required:
        lines.append(f"全程必去（每一天的规划都要为其留出位置）：{'、'.join(trip_required)}")
    if not day_required:
        return lines
    if day_no is None:
        detail = "；".join(f"第{p.day_no}天必须安排「{p.name}」" for p in day_required)
        lines.append(f"指定日必去（硬要求）：{detail}")
        return lines
    today = [p.name for p in day_required if p.day_no == day_no]
    other = [f"第{p.day_no}天的「{p.name}」" for p in day_required if p.day_no != day_no]
    if today:
        lines.append(f"本日（第{day_no}天）必去（硬要求）：{'、'.join(today)}")
    if other:
        lines.append(f"以下必去项安排在其它天，不要排进本日：{'、'.join(other)}")
    return lines


def _window_lines(req_struct: TripRequirements, day_no: int | None) -> list[str]:
    """日窗口：day_no 给定时只渲染该天（本日口径）。"""
    windows = req_struct.day_windows
    if day_no is not None:
        windows = [w for w in windows if w.day_no == day_no]
    lines: list[str] = []
    for w in windows:
        where = f"第{w.day_no}天" if day_no is None else "本日"
        if w.not_before and w.finish_by:
            lines.append(f"{where}行程窗口：{w.not_before} 之后开始、{w.finish_by} 之前结束")
        elif w.not_before:
            lines.append(f"{where}行程窗口：{w.not_before} 之后才开始（不要排早）")
        elif w.finish_by:
            lines.append(f"{where}行程窗口：须在 {w.finish_by} 之前结束")
    return lines


def _preference_lines(req_struct: TripRequirements) -> list[str]:
    """排除/节奏/交通/预算口径/住宿的照实渲染。"""
    lines: list[str] = []
    if req_struct.excluded_places:
        lines.append(f"用户明确不去以下地点（禁止排入行程）：{'、'.join(req_struct.excluded_places)}")
    if req_struct.excluded_categories:
        labels = "、".join(EXCLUDED_CATEGORY_LABELS.get(c, c) for c in req_struct.excluded_categories)
        lines.append(f"用户明确不去以下类别（禁止排入行程）：{labels}")
    if req_struct.pace == "relaxed":
        lines.append("节奏偏好：慢游（每日安排留白，宁可少而精）")
    if req_struct.max_attractions_per_day is not None:
        lines.append(f"每天景点数硬上限：最多 {req_struct.max_attractions_per_day} 个，不得通过改写条目类型绕过")
    if req_struct.dietary_requirement:
        diet = "纯素（不含蛋奶）" if req_struct.dietary_requirement == "vegan" else "素食（可含蛋奶）"
        lines.append(f"饮食要求：每一餐均需适合{diet}；店名、坐标、模型知识不能证明菜单适用")
        lines.append(
            "参考资料中标注「外部饮食标签已证实」的餐厅应优先安排为正餐；"
            "没有这类条目时选择你确知提供"
            + ("纯素" if req_struct.dietary_requirement == "vegan" else "素食")
            + "的真实餐厅，不确定就如实输出，不得虚构分店或宣称已核实"
        )
        if req_struct.dining_evidence_policy == "review_candidates":
            lines.append("用户接受待核实餐饮候选：证据不足的餐厅不可声称已满足饮食要求，不可虚构分店")
        else:
            lines.append("只安排有外部饮食依据的餐厅；无依据时需更换，不可凭名称宣称素食可用")
    transport = {
        "walking": "交通偏好：市内尽量步行可达的选点与路线",
        "driving": "交通偏好：用户接受打车/驾车为主的市内交通",
        "mixed": "交通偏好：步行与车行结合，按距离灵活选择",
    }
    if req_struct.transport_preference in transport:
        lines.append(transport[req_struct.transport_preference])
    if req_struct.max_walk_minutes_per_leg:
        minutes = req_struct.max_walk_minutes_per_leg
        lines.append(f"步行策略默认（估算值，非用户硬上限）：尽量控制单段步行在 {minutes} 分钟内")
    policy = req_struct.budget_policy
    if policy is not None:
        if policy.mode == "hard_cap":
            lines.append("预算口径：预算为硬上限，费用估算总额不得超过")
        elif policy.mode == "target":
            lines.append("预算口径：预算为目标值，允许小幅浮动但不要明显超出")
        if policy.include_intercity_transport is False:
            lines.append("预算口径：费用估算不含往返大交通（机票/火车）")
    lodging = req_struct.lodging
    if lodging is not None:
        if lodging.locked_hotel_identity:
            lines.append(f"住宿锁定：酒店已确定为「{lodging.locked_hotel_identity}」，不要推荐更换")
        if lodging.rooms:
            lines.append(f"住宿房间数：{lodging.rooms} 间")
    return lines


def requirements_directives_clause(requirements_struct: TripRequirements | None, *, day_no: int | None = None) -> str:
    """结构化需求 → 硬约束指令块；全默认/None 返回空串。

    与 requirements_clause（自然语言原话）并存且语义以此为准；逐日带整趟
    需求及 day_no 的投影，不修改原需求。
    """
    if requirements_struct is None:
        return ""
    lines = [
        *(
            f"固定安排：第 {f.day_no} 天 {f.start_time}–{f.end_time}，类型 {f.item_type}，地点原名「{f.name}」。"
            "必须保留原名和时段，其他安排不得重叠；这是用户提供的安排，不是平台确认的预订。"
            + (f"用户填写单价 {f.cost} 元。" if f.cost is not None else "")
            for f in requirements_struct.fixed_arrangements
            if day_no is None or f.day_no == day_no
        ),
        *_must_visit_lines(requirements_struct, day_no),
        *_window_lines(requirements_struct, day_no),
        *_preference_lines(requirements_struct),
    ]
    if not lines:
        return ""
    body = "\n".join(f"- {line}" for line in lines)
    return f"\n结构化需求硬约束（来自用户确认过的需求，逐条落实；与本块冲突的生成偏好一律让位）：\n{body}\n"
