"""Validate extraction against its own typed contract and explicit source scope, never gold."""

import re
from decimal import Decimal, InvalidOperation

from app.agent.core.json_utils import LlmJsonError
from app.schemas.requirement_patches import RequirementPatch, allowed_patch_operations
from app.schemas.trip_requirements import MAX_TRIP_DAYS


def reconcile_person_count(state, message):
    """A direct source count and a conflicting extraction require a user choice, never a Gold guess."""
    if state.person_count_options:
        selected = re.fullmatch(r"\s*(?:确认|实际|一共|总共|共|就按)?\s*(\d{1,2})\s*(?:人|位)\s*[。！!]?", message)
        if selected and 1 <= int(selected[1]) <= 20:
            state.persons = int(selected[1])
            state.person_count_options = []
        else:
            state.persons = None
        return
    direct = re.search(r"(?:旅行人数|出行人数)\s*[:：=]?\s*(\d{1,2})(?!\d)", message)
    companion = re.search(r"我和\s*(\d{1,2})(?!\d)\s*(?:个|位|名)?\s*(?:同学|朋友|同伴|伙伴|家人)", message)
    counts = {state.persons} if state.persons is not None else set()
    if direct and 1 <= int(direct[1]) <= 20:
        counts.add(int(direct[1]))
    if companion and 1 <= int(companion[1]) < 20:
        counts.add(int(companion[1]) + 1)
    if len(counts) > 1:
        state.person_count_options = sorted(counts)
        state.persons = None


def extraction_issues(data, message, state):
    issues, patches = [], []
    for raw in data.get("patches") or []:
        try:
            patch = RequirementPatch.model_validate(raw)
            if patch.op not in allowed_patch_operations(patch.target):
                raise ValueError(f"{patch.target}不能使用{patch.op}，请按目标类型选择set/unset或列表操作")
            if patch.target.startswith("day_") and patch.day_no is None:
                raise ValueError("日窗口必须有明确day_no；每天的窗口须逐日列出")
            if patch.target == "place_relation" and patch.op in {"add", "set"} and patch.relation is None:
                raise ValueError("关系对象必须填relation，value须为null")
            if patch.source_text and patch.source_text not in message:
                raise ValueError(
                    f"{patch.target}/{patch.name or patch.value}: source_text={patch.source_text!r}"
                    "不是最新原话的连续原文。并列项不能补写前项的‘必须去’等前缀；"
                    "请逐字选择原话中的地点名片段或完整连续句作为source_text，不改要求。"
                )
            patches.append(patch)
        except (ValueError, TypeError) as exc:
            issues.append(str(exc)[:220])
    days = data.get("days") or state.days
    if type(days) is int and 1 <= days <= MAX_TRIP_DAYS:
        for clause in re.findall(r"(?:每天|每日|每一天|各日)[^。；;\n]{0,100}", message):
            times = re.findall(r"(?:\d{1,2}:\d{2}|\d{1,2}点|[一二三四五六七八九十两零]+点)", clause)
            if len(times) < 2:
                continue
            if not any(word in clause for word in ("开始", "从", "不早于", "游览")):
                continue
            if not any(word in clause for word in ("结束", "到", "至", "不晚于")):
                continue
            for target in ("day_not_before", "day_finish_by"):
                covered = {p.day_no for p in patches if p.target == target and p.op == "set"}
                missing = set(range(1, days + 1)) - covered
                if missing:
                    issues.append(f"原话统一逐日窗口尚缺{target}的日期{sorted(missing)}；从原话抽取，不省略")
    if state.requirements.constraint_planning and any(word in message for word in ("门票", "活动")):
        amounts = set(re.findall(r"(?:预算|硬上限|上限|不得超过|不超过|总额)[^\d。；;\n]{0,8}(\d+(?:\.\d+)?)", message))
        if len(amounts) == 1:
            value = data.get("budget")
            # Only validate an explicit source amount; never assign it to state here.
            try:
                matches = value is not None and Decimal(str(value)) == Decimal(next(iter(amounts)))
            except InvalidOperation:
                matches = False
            if not matches:
                issues.append("原话有明确活动/门票预算金额（包括0），budget应保留该金额，不能因不是食宿总预算而留空")
        policy = state.requirements.budget_policy
        mode = policy.mode if policy else None
        intercity = policy.include_intercity_transport if policy else None
        for patch in patches:
            if patch.op == "set" and patch.target == "budget_policy_mode":
                mode = patch.value
            if patch.op == "set" and patch.target == "budget_policy_include_intercity":
                intercity = patch.value
        if any(word in message for word in ("硬上限", "不得超过", "不超过")) and mode != "hard_cap":
            issues.append("原话活动费用硬上限必须保留budget_policy_mode=hard_cap，不能降为目标预算")
        if "不含城际" in message and intercity is not False:
            issues.append("原话明确不含城际，须保留budget_policy_include_intercity=false")
    for patch in patches:
        if patch.target != "unresolved_request":
            continue
        source = patch.source_text or str(patch.value or "")
        if source.count("必须先") >= 2 and not any(word in source for word in ("如果", "条件", "跨日")):
            issues.append("同日明确的多条硬先后即使互相矛盾也可表达，须分别输出before关系，交给求解器判冲突")
    return issues


def validate_extraction(data, message, state):
    issues = extraction_issues(data, message, state)
    if issues:
        raise LlmJsonError("；".join(issues)[:1800])
