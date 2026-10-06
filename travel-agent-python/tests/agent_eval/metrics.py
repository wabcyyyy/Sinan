"""Agent 评测指标：把“能生成”拆成可回答面试追问的质量指标。"""

from __future__ import annotations

from collections import Counter
from typing import Any

from app.agent.core.intent import build_intent_keywords
from app.agent.data.map_link import map_directions_url
from app.agent.generation.content.reflect import _item_end, _item_start, estimate_transfer_minutes
from app.agent.grounding.grounding_evidence import lookup_ticket
from app.agent.research.evidence import RESEARCH_DOMAINS

# 研究泳道序号 = RESEARCH_DOMAINS 序（hotel/attraction/food）；上溯不到
# research.<domain> 的事件（节点/主线程工具）都归主泳道，排最后。
_LANE_RANK_OF_NAME = {f"research.{domain}": rank for rank, domain in enumerate(RESEARCH_DOMAINS)}
_MAIN_LANE_RANK = len(RESEARCH_DOMAINS)


def _lane_rank_of_name(name: str) -> int | None:
    """事件名自身的泳道：`research.<domain>`（agent span）或其下的 `research.<domain>.*`。"""
    for lane_name, rank in _LANE_RANK_OF_NAME.items():
        if name == lane_name or name.startswith(lane_name + "."):
            return rank
    return None


def _lane_rank(event: dict, by_span: dict[str, dict]) -> int:
    """事件所属研究泳道的序号：先看自身名字，再沿 parent_span_id 上溯到 agent span。"""
    rank = _lane_rank_of_name(str(event.get("name") or ""))
    if rank is not None:
        return rank
    parent_id = event.get("parent_span_id")
    for _ in range(8):
        if not parent_id:
            break
        parent = by_span.get(str(parent_id))
        if parent is None:
            break
        rank = _lane_rank_of_name(str(parent.get("name") or ""))
        if rank is not None:
            return rank
        parent_id = parent.get("parent_span_id")
    return _MAIN_LANE_RANK


def ordered_event_names(events: list[dict], universe: list[dict] | None = None) -> list[str]:
    """泳道稳定排序的事件名序列（单测直接断言其确定性与保序语义）。

    为什么不能 `sorted(名字)`（PR-0 修的丢序问题）：研究三域并行执行，
    **到达顺序**由线程调度决定（同一 fixture 实测三种排列），按名排序虽稳定了字节
    却把调用顺序整个丢掉——图路径把 generate 排到 format 前面都看不出来。现在
    按研究泳道稳定排序：泳道内保到达序（顺序信息），泳道间按域序排（确定性），
    主泳道（节点/主线程工具）天然单线程、到达序即确定序。

    `universe` = 全量事件（泳道上溯要查 agent span 事件，而调用方往往只喂
    node/tool 子集）；默认按 events 自身建索引，单测喂小样本即可。
    """
    by_span = {
        str(event.get("span_id")): event for event in (events if universe is None else universe) if event.get("span_id")
    }
    return [event["name"] for event in sorted(events, key=lambda event: _lane_rank(event, by_span))]


def _items(response) -> list[dict]:
    return [
        item.model_dump() if hasattr(item, "model_dump") else item
        for plan in response.daily_plans
        for item in plan.items
    ]


def has_coord(value) -> bool:
    """坐标有效：非 None 且非 0（0/0 是缺失哨兵，与生成链路 has_coord 口径一致）。"""
    try:
        return value is not None and abs(float(value)) > 1e-6
    except (TypeError, ValueError):
        return False


def evaluate_depth(response, case: dict) -> dict[str, Any]:
    """C3.2 深度指标：坐标有效率 / 深链可解析率 / 类目合理率。

    - coord_valid_rate：attraction/food/hotel 项 lat/lng 均有效（has_coord，
      非 None 非 0——0/0 是缺失哨兵）的比例；
    - deeplink_resolvable_rate：按"天"计——当天有效坐标停靠点 ≥2 时必须能
      生成全天路线深链（places.map_directions_url 非 None），不足 2 点的天
      按"无路线承诺"自动通过。用后端 places 语义评测；前端 geo.ts 另有
      90/180 范围校验与 waypoints 上限，口径差异由前端 geo.test.ts 单测覆盖；
    - category_reasonable_rate：item_type 落四值白名单且 poi_name 非空。
      TripItem schema 已约束 pattern，此指标是生成产物的回归绊线。
    """
    items = _items(response)
    poi_items = [i for i in items if i.get("item_type") in ("attraction", "food", "hotel")]

    coord_ok = sum(1 for i in poi_items if has_coord(i.get("latitude")) and has_coord(i.get("longitude")))
    category_ok = sum(
        1
        for i in items
        if i.get("item_type") in ("attraction", "food", "hotel", "transport") and str(i.get("poi_name") or "").strip()
    )

    days_total = 0
    days_route_ok = 0
    for plan in response.daily_plans:
        days_total += 1
        stops = [
            item.model_dump() if hasattr(item, "model_dump") else dict(item)
            for item in plan.items
            if item.item_type in ("attraction", "food")
        ]
        valid_stops = sum(1 for s in stops if has_coord(s.get("latitude")) and has_coord(s.get("longitude")))
        # ≥2 个有效停靠点的天承诺了路线深链（必须可解析）；不足 2 点的天
        # 按"无路线承诺"自动通过——无坐标行程不因此计红。
        days_route_ok += valid_stops < 2 or map_directions_url(stops) is not None

    return {
        "coord_valid_rate": round(coord_ok / max(len(poi_items), 1), 4),
        "deeplink_resolvable_rate": round(days_route_ok / max(days_total, 1), 4),
        "category_reasonable_rate": round(category_ok / max(len(items), 1), 4),
    }


def evaluate_narrative(response, case: dict) -> dict:
    """M5 叙事化指标：度量契约 v1.1.narrative 叙事字段的质量与意图贴合度。

    与 evaluate_response（权威/冲突/预算口径）互补，只读叙事层字段，不改
    既有函数签名。各指标定义：
    - theme_sentence_rate：每日 theme 是叙事句（非空且非「A→B→C」纯路径串，
      即不含 "→"）的比例；
    - why_coverage：attraction 项 why_this 非空的比例；
    - practical_notes_rate：每日 practical_notes 非空的比例；
    - theme_hit_rate：意图关键词（build_intent_keywords(case.intent)）在
      （trip_theme + 各日 theme + 全部 why_this + remark）拼接文本中的
      命中关键词占比；case 无 intent 时为 None；
    - poi_relevance：attraction 项中 why_this/remark 命中任一意图关键词的
      比例（「主题相关点占比」）；case 无 intent 时为 None；
    - coord_available_rate：attraction 项坐标非空（lat/lon 均非 None 非 0）
      的比例；
    - pending_review_count：verification_status=="unverified" 的 item 数
      （「待复核占比」的分子）。
    """
    keywords = build_intent_keywords(case.get("intent"))
    plans = list(response.daily_plans)
    items = _items(response)
    attractions = [i for i in items if i.get("item_type") == "attraction"]

    theme_sentence_days = sum(1 for plan in plans if plan.theme and "→" not in plan.theme)
    practical_days = sum(1 for plan in plans if plan.practical_notes)
    why_filled = sum(1 for i in attractions if (i.get("why_this") or "").strip())
    coord_ok = sum(1 for i in attractions if has_coord(i.get("latitude")) and has_coord(i.get("longitude")))
    pending_review = sum(1 for i in items if i.get("verification_status") == "unverified")

    theme_hit_rate = None
    poi_relevance = None
    if keywords:
        # 关键词命中语料：行程级主题 + 每日主题 + 全部条目的 why_this/remark
        corpus = [response.trip_theme or ""]
        corpus += [plan.theme or "" for plan in plans]
        corpus += [(i.get("why_this") or "") + (i.get("remark") or "") for i in items]
        text = "".join(corpus)
        theme_hit_rate = sum(1 for k in keywords if k in text) / len(keywords)
        poi_relevance = sum(
            1 for i in attractions if any(k in (i.get("why_this") or "") + (i.get("remark") or "") for k in keywords)
        ) / max(len(attractions), 1)

    return {
        "theme_sentence_rate": round(theme_sentence_days / max(len(plans), 1), 4),
        "why_coverage": round(why_filled / max(len(attractions), 1), 4),
        "practical_notes_rate": round(practical_days / max(len(plans), 1), 4),
        "theme_hit_rate": round(theme_hit_rate, 4) if theme_hit_rate is not None else None,
        "poi_relevance": round(poi_relevance, 4) if poi_relevance is not None else None,
        "coord_available_rate": round(coord_ok / max(len(attractions), 1), 4),
        "pending_review_count": pending_review,
    }


def _anchored(item: dict, city: str) -> bool:
    """这一项能不能对上本服务签发的证据票（"有来源"的唯一凭据）。"""
    name = str(item.get("poi_name") or "").strip()
    if not name:
        return False
    return lookup_ticket(name, city, str(item.get("poi_id") or "")) is not None


def evaluate_response(response, case: dict, catalog: dict, trace: dict) -> dict:
    items = _items(response)
    all_known = {p["name"]: p for p in catalog["attractions"] + catalog["foods"] + catalog["hotels"]}
    poi_items = [i for i in items if i.get("item_type") in ("attraction", "food", "hotel")]
    city = str(case.get("city") or "")
    # D11A：权威判定与 fixture 目录解耦——过去"名字在目录里"就算权威，等于
    # 用出题的表给自己打分。现在认的是证据票：目录行必须显式签票才算查到过。
    authoritative = [i for i in poi_items if _anchored(i, city)]
    grounded = [
        i for i in poi_items if _anchored(i, city) and has_coord(i.get("latitude")) and has_coord(i.get("longitude"))
    ]
    refuted = len((response.schedule_report or {}).get("refuted_pois_dropped") or [])
    field_matches = []
    for item in authoritative:
        # 有票但不在本次评测目录里的点位（真实 LLM 自选点几乎都在这档）没有独立的
        # 参照字段可比：跳过，而不是塞进分母或当成"对上了"。
        source = all_known.get(item["poi_name"])
        if source is None:
            continue
        if item.get("item_type") in ("attraction", "food"):
            field_matches.append(item.get("cost") == source.get("ticket_price"))
    # 一个可比样本都没有 → None（报告里显示 "-"）。返回 1.0 会把"无从核对"报成满分。
    field_reference_rate = sum(field_matches) / len(field_matches) if field_matches else None

    conflicts = 0
    pairs = 0
    route_violations = 0
    route_pairs = 0
    duplicates = Counter()
    attractions = []
    for plan in response.daily_plans:
        timed = [i for i in plan.items if i.item_type in ("attraction", "food")]
        timed = sorted((i.model_dump() for i in timed), key=_item_start)
        pairs += max(len(timed) - 1, 0)
        for i in range(len(timed) - 1):
            previous, following = timed[i], timed[i + 1]
            previous_end = _item_end(previous)
            following_start = _item_start(following)
            conflicts += previous_end > following_start
            required = estimate_transfer_minutes(previous, following)
            if required is not None:
                route_pairs += 1
                route_violations += following_start >= previous_end and following_start - previous_end < required
        attractions.extend(i.poi_name for i in plan.items if i.item_type == "attraction")
    duplicates.update(attractions)
    duplicate_count = sum(n - 1 for n in duplicates.values() if n > 1)

    cons = catalog["consumption"]
    rooms = (case["persons"] + 1) // 2
    selected_ticket = sum(float(i.get("cost") or 0) for i in items if i.get("item_type") == "attraction")
    # 住宿期望与生产口径一致：N 天 = N-1 晚，最后一天不计房价。
    selected_hotel = sum(
        float(i.cost or 0)
        for plan in response.daily_plans
        if plan.day_no < case["days"]
        for i in plan.items
        if i.item_type == "hotel"
    )
    # 餐饮期望与生产口径一致（BIZ-5 收拢后）：有价日期按当日选中餐饮条目人均价
    # 加总，无价日期回落到城市人均餐价×2 餐；此前这里复刻旧「最贵一餐×2」口径，
    # 与生产的加总口径分裂成第三套，指标会惩罚真实的计价改进。
    priced_meal = 0.0
    priced_days = 0
    for plan in response.daily_plans:
        day_food = sum(float(i.cost or 0) for i in plan.items if i.item_type == "food" and i.cost is not None)
        if day_food > 0:
            priced_meal += day_food
            priced_days += 1
    unpriced_days = max(case["days"] - priced_days, 0)
    expected = {
        "门票": round(selected_ticket * case["persons"], 2),
        "餐饮": round(
            priced_meal * case["persons"] + float(cons.get("meal_price", 60)) * 2 * unpriced_days * case["persons"], 2
        ),
        "交通": round(float(cons.get("transport_price", 35)) * case["days"] * case["persons"], 2),
        "酒店": round(selected_hotel * rooms, 2),
    }
    expected_total = sum(expected.values())
    actual_total = sum(float(v) for v in response.budget_estimate.values())

    trace_events = trace.get("events", [])
    tool_events = [event for event in trace_events if event["kind"] == "tool"]
    node_events = [event for event in trace_events if event["kind"] == "node"]
    # 多 Agent 研究编排（P3）：从 schedule_report 汇总各域证据包规模与推理轮次。
    research = (response.schedule_report or {}).get("research") or {}
    research_agents = research.get("agents") or {}
    research_rounds = sum((a or {}).get("rounds", 0) for a in research_agents.values())
    research_pack = sum((a or {}).get("count", 0) for a in research_agents.values())
    return {
        "case": case,
        "status": response.status,
        "status_reason": response.status_reason,
        "days": len(response.daily_plans),
        "total_items": len(items),
        "poi_authority_rate": round(len(authoritative) / max(len(poi_items), 1), 4),
        # 与 coord_valid_rate 成对看：只算坐标会给"模型自填坐标"记成功，
        # 只算票会漏掉"查到过但这一版没落上坐标"——两个一起才看得清接地质量。
        "poi_grounded_rate": round(len(grounded) / max(len(poi_items), 1), 4),
        "poi_refuted_count": refuted,
        "field_reference_rate": round(field_reference_rate, 4) if field_reference_rate is not None else None,
        "time_conflict_rate": round(conflicts / pairs, 4) if pairs else 0.0,
        "route_violation_rate": round(route_violations / route_pairs, 4) if route_pairs else 0.0,
        "attraction_duplicate_rate": round(duplicate_count / max(len(attractions), 1), 4),
        "budget_deviation_rate": round(abs(actual_total - expected_total) / expected_total, 4)
        if expected_total
        else 0.0,
        "fallback_success": len(response.daily_plans) == case["days"]
        and all(plan.items for plan in response.daily_plans),
        "trace": {
            "node_count": len(node_events),
            "tool_count": len(tool_events),
            # 节点/工具序列经 ordered_event_names 泳道稳定排序：保留调用顺序
            # （顺序信息是回归绊线的一部分），又对并行研究三域的到达乱序免疫，
            # 报告字节可复现（CI eval-determinism job 两遍 SHA256 把关）。
            # 旧口径按名排序把顺序整个丢掉，图路径乱序也测不出来（PR-0 修）。
            "nodes": ordered_event_names(node_events, trace_events),
            "tools": ordered_event_names(tool_events, trace_events),
            "routes": [event["name"] for event in trace_events if event["kind"] == "route"],
        },
        "research_rounds": research_rounds,
        "research_pack": research_pack,
    }
