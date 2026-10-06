"""可解释的软质量评审。

硬约束仍由 reflect/optimizer 负责；这里不否决计划，只评估主题连贯性、节奏、
多样性和偏好命中，供响应、Trace 和离线评测使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CriticResult:
    score: float
    issues: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    dimensions: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "issues": list(self.issues),
            "strengths": list(self.strengths),
            "dimensions": dict(self.dimensions),
        }


def critique_plans(plans: list[dict], preferences: list[str] | None = None) -> CriticResult:
    preferences = [str(item).strip().lower() for item in preferences or [] if str(item).strip()]
    if not plans:
        return CriticResult(0.0, ["没有可评审的日程"], [], {"completeness": 0.0})
    active_days = [
        plan
        for plan in plans
        if any(item.get("item_type") in ("attraction", "food") for item in plan.get("items") or [])
    ]
    day_scores: list[float] = []
    issues: list[str] = []
    strengths: list[str] = []
    types: set[str] = set()
    preference_hits = 0
    preference_total = 0
    for plan in plans:
        items = [item for item in plan.get("items") or [] if item.get("item_type") in ("attraction", "food")]
        types.update(str(item.get("item_type")) for item in items)
        if not items:
            issues.append(f"第 {plan.get('day_no')} 天缺少主要活动")
            day_scores.append(0.0)
            continue
        count = len([item for item in items if item.get("item_type") == "attraction"])
        score = 1.0
        if count >= 4:
            score -= 0.2
            issues.append(f"第 {plan.get('day_no')} 天景点密度偏高")
        if count == 1 and len(items) == 1:
            score -= 0.05
        day_text = " ".join(
            " ".join(str(item.get(key) or "") for key in ("poi_name", "tag", "remark")) for item in items
        ).lower()
        for preference in preferences:
            preference_total += 1
            if preference in day_text:
                preference_hits += 1
        day_scores.append(max(score, 0.0))
    diversity = 1.0 if len(types) >= 2 else 0.7
    if len(types) < 2:
        issues.append("行程类型较单一，可增加餐饮或不同主题活动")
    else:
        strengths.append("景点与餐饮类型有基本搭配")
    completeness = len(active_days) / len(plans)
    preference_score = preference_hits / preference_total if preference_total else 1.0
    dimensions = {
        "pace": sum(day_scores) / len(day_scores),
        "diversity": diversity,
        "preference_alignment": preference_score,
        "completeness": completeness,
    }
    score = sum(dimensions.values()) / len(dimensions)
    if score >= 0.8:
        strengths.append("整体节奏和偏好匹配度良好")
    return CriticResult(score, list(dict.fromkeys(issues)), strengths, dimensions)
