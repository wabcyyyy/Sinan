"""叙事字段清洗（G-2.2 自 day_stream 拆出）。

职责：把 LLM 输出的叙事字段（theme/why_this/practical_notes/photo_spots/
backup_plan/day_options/note）做轻量清洗兜底——超限截断、类型非法降级为空、
剥离用户指令残留分句，绝不让单条脏叙事炸掉整日行程（骨架照常交付）。

同时将 LLM 输出边界**收口为唯一一处**（D6=C / P5）：模型自报的身份、坐标与
溯源标签在这里统一剥掉，因为三条生成链（open_day / open_trip / 流式逐日）都在
parse_llm_json 之后经过本函数，而知识/约束链的 items 由服务端自建、不经此处。

实现要点：
- 限值口径与 app/prompts/open_generation 契约、app/schemas/trip.py 的截断
  逐项对齐；
- 同日同名去重（dedupe_same_day_items）是模型重复拆条的确定性兜底。

依赖：generation_core（模型申报字段剥离）、poi_identity（norm_poi_key）、trace.record_event；re。
"""

import re

from app.agent.core.poi_identity import norm_poi_key
from app.agent.generation.rules.generation_core import has_model_claims, strip_model_claims
from app.agent.runtime.trace import record_event

# 叙事字段规模上限：与 open_generation 契约、app/schemas/trip.py 的截断口径
# 一一对应。LLM 偶尔无视条数/长度约束，在 parse_llm_json 之后做轻量清洗兜底——
# 超限截断、类型非法降级为空，绝不让单条脏叙事炸掉整日行程（骨架照常交付）。
NARRATIVE_THEME_MAX = 40
_NARRATIVE_WHY_MAX = 120
_PRACTICAL_NOTES_MAX = 4
_PHOTO_SPOTS_MAX = 4
_BACKUP_PLAN_MAX = 3
_DAY_OPTIONS_MAX = 2


# 指令残留句式：模型偶尔把用户原始请求原样抄进 why_this/note/theme 等
# 成品文案，对用户可见即信任事故。句子级剔除以第一人称请求/指令开头的
# 分句；保守匹配，正常叙事（如「适合想避开人潮的旅客」）不受影响。
_INSTRUCTION_RESIDUE_RE = re.compile(
    r"^(?:我(?:想去|想去看|想去逛|想吃|想玩|想体验|要去看|要去|希望去|打算去|计划去|准备去)"
    r"|请?帮我|帮我(?:规划|安排|推荐|生成|制定|找|看看)"
    r"|(?:规划|安排|生成|制定|推荐)一?(?:下|个|份))"
)


def _strip_instruction_residue(text):
    """剔除叙事文案里以用户口吻请求开头的残留分句（句级拆分，保守匹配）。"""
    if not isinstance(text, str) or not text:
        return text
    kept = [seg for seg in re.split(r"(?<=[。！？；;\n])", text) if not _INSTRUCTION_RESIDUE_RE.match(seg.strip())]
    return "".join(kept)


def _norm_poi_key(name) -> str:
    """同日去重键：委托 poi_identity.norm_poi_key（剥括号注记/标点/大小写归一）。"""
    return norm_poi_key(name)


def _dedupe_same_day_items(items: list):
    """同一天内同名点位去重：保留首条，丢弃后续重复。

    模型偶尔把同一景区按不同玩法拆成多条（如「浅草寺」×3：文化/历史、
    地标/拍照、购物/文化），用户看到「同一个地方去三次」。契约已禁止
    （open_generation 硬性要求），这里做确定性兜底；脏项（非 dict/空名）
    原样透传，交给 sanitize_itinerary_items 统一过滤。
    """
    seen: set[str] = set()
    out: list = []
    for item in items:
        if isinstance(item, dict):
            key = _norm_poi_key(item.get("poi_name") or item.get("poiName"))
            if key and key in seen:
                record_event(
                    "decision", "same_day_duplicate_dropped", metadata={"poi_name": str(item.get("poi_name") or "")}
                )
                continue
            if key:
                seen.add(key)
        out.append(item)
    return out


def sanitize_narrative(plan: dict) -> dict:
    """对开放模式 LLM 输出（parse_llm_json 结果）做叙事字段轻量清洗。

    规则（方案 §4.1.2）：
    - theme/trip_theme 超长截 40 字；item.why_this 超长截 120 字
      （非 attraction 的 why_this 同样保留，只截不删）；
    - practical_notes 超 4 条裁 4、photo_spots 超 4 裁 4、backup_plan 超 3 裁 3、
      day_options 超 2 裁 2；
    - 缺省即空、非法类型降级为空，不抛错；
    - 兼容 camelCase 键（模型不守契约时仍能清洗），统一写回 snake_case，
      供装配层读取。
    """
    if not isinstance(plan, dict):
        # 非法结构不在这里纠偏：保持原样交给既有结构校验抛错路径
        return plan
    cleaned = dict(plan)

    theme = cleaned.get("theme")
    cleaned["theme"] = _strip_instruction_residue(theme)[:NARRATIVE_THEME_MAX] if isinstance(theme, str) else None

    note = cleaned.get("note")
    if isinstance(note, str):
        cleaned["note"] = _strip_instruction_residue(note)

    trip_theme = cleaned.get("trip_theme")
    if trip_theme is None:
        trip_theme = cleaned.get("tripTheme")
    cleaned.pop("tripTheme", None)
    if trip_theme is None:
        cleaned["trip_theme"] = None
    elif isinstance(trip_theme, str):
        cleaned["trip_theme"] = _strip_instruction_residue(trip_theme)[:NARRATIVE_THEME_MAX]
    else:
        # 非字符串降级为空串：叙事字段不允许让 TripItem/DailyPlan 校验失败
        cleaned["trip_theme"] = ""

    notes = cleaned.get("practical_notes")
    if notes is None:
        notes = cleaned.get("practicalNotes")
    cleaned.pop("practicalNotes", None)
    if isinstance(notes, list):
        # 字符串原样保留，数值标量转字符串（模型偶尔输出数字提示），其余丢弃
        cleaned["practical_notes"] = [
            note if isinstance(note, str) else str(note)
            for note in notes[:_PRACTICAL_NOTES_MAX]
            if isinstance(note, (str, int, float, bool))
        ]
    else:
        cleaned["practical_notes"] = []

    spots = cleaned.get("photo_spots")
    if spots is None:
        spots = cleaned.get("photoSpots")
    cleaned.pop("photoSpots", None)
    normalized_spots: list = []
    if isinstance(spots, list):
        for spot in spots[:_PHOTO_SPOTS_MAX]:
            if isinstance(spot, str):
                # 模型把出片点写成纯字符串时降级为 {name}，保住条目
                normalized_spots.append({"name": spot})
            elif isinstance(spot, dict):
                normalized_spots.append(spot)
    cleaned["photo_spots"] = normalized_spots

    backups = cleaned.get("backup_plan")
    if backups is None:
        backups = cleaned.get("backupPlan")
    cleaned.pop("backupPlan", None)
    cleaned["backup_plan"] = (
        [row for row in (backups or [])[:_BACKUP_PLAN_MAX] if isinstance(row, dict)]
        if isinstance(backups, list)
        else []
    )

    options = cleaned.get("day_options")
    if options is None:
        options = cleaned.get("dayOptions")
    cleaned.pop("dayOptions", None)
    cleaned["day_options"] = (
        [row for row in (options or [])[:_DAY_OPTIONS_MAX] if isinstance(row, dict)]
        if isinstance(options, list)
        else []
    )

    items = cleaned.get("items")
    if isinstance(items, list):
        fixed_items: list = []
        claimed: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                fixed_items.append(item)  # 脏项交给 sanitize_itinerary_items 过滤
                continue
            row = dict(item)
            # 身份/位置/溯源标签由服务端解析器写，模型自报的一律剥掉（D6=C + G4）。
            # 不剥的话，一个编出来的名字配上编出来的经纬度就能绕过整条接地链：
            # 落地判据是"已有坐标就不再解析"，坐标权即存在性背书。
            if has_model_claims(row):
                claimed.append(str(row.get("poi_name") or row.get("poiName") or ""))
                strip_model_claims(row)
            why = row.get("why_this")
            if why is None:
                why = row.get("whyThis")
            row.pop("whyThis", None)
            if why is None:
                row["why_this"] = None
            elif isinstance(why, str):
                # 先剔除指令残留分句，再截长度；非 attraction 同样保留，不删字段
                row["why_this"] = _strip_instruction_residue(why)[:_NARRATIVE_WHY_MAX]
            else:
                row["why_this"] = ""
            fixed_items.append(row)
        if claimed:
            record_event("decision", "model_claims_stripped", metadata={"names": claimed, "count": len(claimed)})
        # 同日同名点位去重（保留首条）：模型偶尔把同一景区按不同 tag 拆成多条
        cleaned["items"] = _dedupe_same_day_items(fixed_items)
    return cleaned
