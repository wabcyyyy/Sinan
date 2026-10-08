"""需求 patch：clarify 抽取器输出的有类型变更与其确定性应用（M1a，spec §5.2）。

职责：
- RequirementPatch：模型输出的单条需求变更，op = set/unset/add/remove，
  目标只允许预定义枚举（PatchTarget），不支持任意 JSONPath 或字典写入；
- apply_intake_patches：把 patch 确定性应用到累计状态 IntakeState。
  语义：未提及 = 不更新；明确删除/取消 = 清空；空数组 = 清空；"改成四天"
  替换三天（set 的 upsert 语义，required_place 按 name 替换日约束不留冲突副本）；
- 被拒绝的 patch 不丢弃：原话描述追加进 requirements.unresolved_requests
  （有界，超出截最旧），保证"未支持或冲突的要求不被丢弃后宣称全部满足"。

应用器按字段归属分流：IntakeState 基础参数直接写 state；TripRequirements 侧
标量/嵌套结构走点路径（_set_nested，unset 时父对象整体置 None 不残留半截口径）。

依赖：pydantic + app.schemas.common + app.schemas.trip_requirements（单向，无环）。
"""

from typing import Any, Literal

from pydantic import Field

from app.schemas.common import WireModel
from app.schemas.trip_requirements import (
    MAX_TRIP_DAYS,
    SUPPORTED_EXCLUDED_CATEGORIES,
    BudgetPolicy,
    IntakeState,
    LodgingRequirements,
    RequiredPlace,
    TripRequirements,
)

#: patch 操作目标预定义枚举；day_windows 暂不开放 patch（到离时间协商在 M3 接入）。
PatchTarget = Literal[
    "city",
    "days",
    "persons",
    "budget",
    "start_date",
    "stay_nights",
    "origin_city",
    "hotel_tier",
    "preferences",
    "required_place",
    "excluded_place",
    "excluded_category",
    "pace",
    "transport_preference",
    "max_walk_minutes_per_leg",
    "budget_policy_mode",
    "budget_policy_include_intercity",
    "lodging_rooms",
    "lodging_locked_hotel",
    "lodging_stay_nights_explicit",
]

#: patch 值类型：JSON 标量或字符串列表（preferences 的 set）；结构目标用
#: name/day_no 扁平字段携带，value 不放嵌套对象，应用语义保持确定性。
PatchValue = str | int | float | bool | list[str] | None


class RequirementPatch(WireModel):
    """clarify 抽取器输出的单条需求变更。

    - set：upsert 语义（"改成四天"替换三天；required_place 按 name 替换日约束，
      不保留冲突副本）；
    - unset：清空（"预算不限制了"；明确删除/取消）；
    - add/remove：集合目标（preferences/required_place/excluded_place/excluded_category）
      的元素级操作，remove 按名称定位；
    - 未提及 = 不输出 patch = 不更新。
    """

    op: Literal["set", "unset", "add", "remove"]
    target: PatchTarget
    value: PatchValue = None
    #: 集合目标的元素定位名（required_place/excluded_place/excluded_category/preferences）
    name: str | None = Field(default=None, min_length=1, max_length=64)
    #: required_place 的日约束（set/add 携带；null=全程必去）
    day_no: int | None = Field(default=None, ge=1, le=MAX_TRIP_DAYS)


_INT_BOUNDS = {
    "days": (1, None),
    "persons": (1, 20),
    "stay_nights": (0, None),
    "lodging_rooms": (1, 20),
    "max_walk_minutes_per_leg": (1, 480),
}
_STATE_INT_FIELDS = {"days", "persons", "stay_nights"}
_STATE_STR_LIMITS = {"city": 64, "origin_city": 64, "hotel_tier": 16, "start_date": 10}
_STATE_SCALAR_FIELDS = {
    # IntakeState 基础参数（非 int 槽位）
    "city": "city",
    "budget": "budget",
    "start_date": "start_date",
    "origin_city": "origin_city",
    "hotel_tier": "hotel_tier",
}
_REQ_SCALAR_FIELDS = {
    # TripRequirements 侧字段（含嵌套结构；点路径由 _set_nested 处理）
    "lodging_rooms": "lodging.rooms",
    "pace": "pace",
    "transport_preference": "transport_preference",
    "max_walk_minutes_per_leg": "max_walk_minutes_per_leg",
    "budget_policy_mode": "budget_policy.mode",
    "budget_policy_include_intercity": "budget_policy.include_intercity_transport",
    "lodging_locked_hotel": "lodging.locked_hotel_identity",
    "lodging_stay_nights_explicit": "lodging.stay_nights_explicit",
}
_COLLECTION_TARGETS = {
    "preferences",
    "required_place",
    "excluded_place",
    "excluded_category",
}
_STR_TARGETS = (
    "city",
    "origin_city",
    "hotel_tier",
    "pace",
    "transport_preference",
    "start_date",
    "lodging_locked_hotel",
)
_INT_TARGETS = ("days", "persons", "stay_nights", "max_walk_minutes_per_leg", "lodging_rooms")


def _reject(patches: list[str], patch: RequirementPatch, reason: str) -> None:
    """被拒绝的 patch 不丢弃：原话进 unresolved_requests，保留协商依据。"""
    patches.append(f"{patch.op} {patch.target}{(' ' + patch.name) if patch.name else ''}：{reason}")


def _set_nested(req: TripRequirements, path: str, value: Any) -> None:
    """按点路径写入嵌套可选结构；unset 时父对象整体置 None，避免残留半截口径。"""
    if path.startswith("budget_policy."):
        if value is None:
            req.budget_policy = None
            return
        req.budget_policy = req.budget_policy or BudgetPolicy()
        setattr(req.budget_policy, path.split(".", 1)[1], value)
    elif path.startswith("lodging."):
        if value is None:
            req.lodging = None
            return
        req.lodging = req.lodging or LodgingRequirements()
        setattr(req.lodging, path.split(".", 1)[1], value)
    else:
        setattr(req, path, value)


def _coerce_scalar(target: str, value: PatchValue) -> tuple[bool, Any]:
    """patch 值类型守卫：类型不符返回 (False, None)，由调用方拒绝（不猜测转换）。"""
    if target in _STR_TARGETS:
        return (isinstance(value, str), value)
    if target in _INT_TARGETS:
        return (isinstance(value, int) and not isinstance(value, bool), value)
    if target == "budget":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return (True, float(value))
        return (False, None)
    if target in ("budget_policy_include_intercity", "lodging_stay_nights_explicit"):
        return (isinstance(value, bool), value)
    if target == "budget_policy_mode":
        return (value in ("target", "hard_cap"), value)
    return (False, None)


def _bucket(state: IntakeState, req: TripRequirements, target: str) -> list[str]:
    """集合目标的同名取值视图（required_place 返回名字列表供存在性判定）。"""
    if target == "preferences":
        return state.preferences
    if target == "excluded_place":
        return req.excluded_places
    if target == "excluded_category":
        return req.excluded_categories
    return [place.name for place in req.required_places]


def _apply_element_op(state: IntakeState, req: TripRequirements, patch: RequirementPatch, rejected: list[str]) -> None:
    """add/remove：集合目标的元素级操作；remove 按名称定位，词表外类别在 add 时拒绝。"""
    target = patch.target
    name = (patch.name or (patch.value if isinstance(patch.value, str) else "")).strip()
    if not name:
        _reject(rejected, patch, "缺少要操作的名称")
        return
    existing = _bucket(state, req, target)
    if patch.op == "remove":
        if target == "preferences":
            state.preferences = [p for p in state.preferences if p != name]
        elif target == "excluded_place":
            req.excluded_places = [p for p in req.excluded_places if p != name]
        elif target == "excluded_category":
            req.excluded_categories = [c for c in req.excluded_categories if c != name]
        else:
            req.required_places = [p for p in req.required_places if p.name != name]
        return
    if name in existing:
        return
    if len(existing) >= 20:
        _reject(rejected, patch, "列表已满")
        return
    if target == "preferences":
        state.preferences.append(name)
    elif target == "excluded_place":
        req.excluded_places.append(name)
    elif target == "excluded_category":
        if name not in SUPPORTED_EXCLUDED_CATEGORIES:
            supported = "、".join(SUPPORTED_EXCLUDED_CATEGORIES)
            _reject(rejected, patch, f"暂不支持排除类别，当前支持：{supported}")
            return
        req.excluded_categories.append(name)
    else:
        req.required_places.append(
            RequiredPlace(constraint_id=f"place-{len(req.required_places) + 1}", name=name, day_no=patch.day_no)
        )


def _apply_collection_set_unset(
    state: IntakeState, req: TripRequirements, patch: RequirementPatch, rejected: list[str]
) -> None:
    """集合目标的 set/unset：preferences set 接受整体列表替换；unset 清空集合；
    required_place set 按 name upsert（替换日约束，不保留冲突副本）。"""
    target = patch.target
    if patch.op == "unset":
        if target == "preferences":
            state.preferences = []
        elif target == "excluded_place":
            req.excluded_places = []
        elif target == "excluded_category":
            req.excluded_categories = []
        else:
            req.required_places = []
        return
    if target == "preferences" and isinstance(patch.value, list):
        state.preferences = [str(item) for item in patch.value][:10]
        return
    if target == "required_place":
        name = (patch.name or "").strip()
        if not name:
            _reject(rejected, patch, "缺少地点名称")
            return
        existing = next((p for p in req.required_places if p.name == name), None)
        if existing is not None:
            existing.day_no = patch.day_no  # 替换日约束，不保留冲突副本
            return
        if len(req.required_places) >= 20:
            _reject(rejected, patch, "必去列表已满")
            return
        req.required_places.append(
            RequiredPlace(constraint_id=f"place-{len(req.required_places) + 1}", name=name, day_no=patch.day_no)
        )
        return
    _reject(rejected, patch, "不支持的操作")


def _apply_scalar_op(state: IntakeState, req: TripRequirements, patch: RequirementPatch, rejected: list[str]) -> None:
    """标量目标的 set/unset：先归属分流再写；界限与类型守卫不猜测转换。"""
    target = patch.target
    if patch.op == "unset":
        # 明确删除/取消 = 清空：state 基础参数置 None；需求侧嵌套结构整体置 None
        if target in _STATE_INT_FIELDS or target in _STATE_SCALAR_FIELDS:
            setattr(state, target, None)
        elif target in _REQ_SCALAR_FIELDS:
            _set_nested(req, _REQ_SCALAR_FIELDS[target], None)
        else:
            _reject(rejected, patch, "未知目标")
        return
    ok, coerced = _coerce_scalar(target, patch.value)
    if not ok:
        _reject(rejected, patch, "值类型不符合该字段要求")
        return
    bounds = _INT_BOUNDS.get(target)
    if bounds is not None:
        lo, hi = bounds
        if not isinstance(coerced, int) or coerced < lo or (hi is not None and coerced > hi):
            _reject(rejected, patch, f"值超出允许范围 [{lo}, {hi if hi is not None else '∞'}]")
            return
    if target in _STATE_INT_FIELDS:
        setattr(state, target, coerced)
        return
    if target == "lodging_rooms":
        _set_nested(req, "lodging.rooms", coerced)
        return
    if target in _STATE_SCALAR_FIELDS:
        value = coerced
        if isinstance(value, str):
            value = value.strip()[: _STATE_STR_LIMITS.get(target, 64)]
            if not value:
                _reject(rejected, patch, "值不能为空")
                return
        setattr(state, target, value)
        return
    if target in _REQ_SCALAR_FIELDS:
        if target == "lodging_locked_hotel" and isinstance(coerced, str):
            coerced = coerced.strip()[:128]
            if not coerced:
                _reject(rejected, patch, "值不能为空")
                return
        _set_nested(req, _REQ_SCALAR_FIELDS[target], coerced)
        return
    _reject(rejected, patch, "未知目标")


def apply_intake_patches(state: IntakeState, patches: list[RequirementPatch]) -> list[str]:
    """把抽取 patch 确定性应用到累计状态，返回被拒绝的描述列表。

    拒绝的 patch 追加进 state.requirements.unresolved_requests（有界，超出即截断
    最旧记录），保证"未支持或冲突的要求不被丢弃后宣称全部满足"。
    """
    rejected: list[str] = []
    req = state.requirements
    for patch in patches:
        if patch.op in ("add", "remove"):
            _apply_element_op(state, req, patch, rejected)
        elif patch.target in _COLLECTION_TARGETS:
            _apply_collection_set_unset(state, req, patch, rejected)
        else:
            _apply_scalar_op(state, req, patch, rejected)
    # 拒绝记录有界入 unresolved：超出上限截最旧，最新协商依据优先保留
    for item in rejected:
        req.unresolved_requests.append(item)
    if len(req.unresolved_requests) > 20:
        req.unresolved_requests = req.unresolved_requests[-20:]
    return rejected
