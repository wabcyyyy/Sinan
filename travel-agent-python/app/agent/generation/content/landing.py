"""两条生成链路共用的落地步骤（G-2.5 ① 治因）。

为什么单独成模块：图节点链路（open_plans）与整段流式链路此前各自实现
「过滤脏项 + 参考资料落地 + 本地补点」——同一业务两份逻辑，改一处忘
另一处就会出现**流式与图产出不一致**（同一行程走两条路径结果不同）。这里收敛
为唯一实现，两处调用同一函数。

去重语义（PR-4 归一）：同日/跨天双通道判重统一为**生成期边判**
（`core/poi_identity.PoiSeenRegistry`：归一化同名 + 落地后同类型近距离 <80m，
酒店豁免）——流式逐天 yield 不可回收，边判是两条链通用的唯一语义；原图路径的
批处理 `drop_cross_day_duplicates` 与流式 `stream_duplicate_dropped` 双轨退场，
统一遥测事件名 `duplicate_dropped`（PR-4 事件序列重认账见 golden）。

依赖：generation_core（脏项过滤）、reference_pool（参考资料池）、grounding
（本地补点）、existence（存在性判定）；无上层依赖。
"""

from typing import Any

from app.agent.core.poi_identity import PoiSeenRegistry, haversine_m
from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.generation.rules.day_policy import MEAL_BRANCH_MAX_DISTANCE_M
from app.agent.generation.rules.generation_core import filter_dirty_items
from app.agent.grounding.facts import has_coord, local_ground
from app.agent.grounding.grounding_evidence import evidence_row
from app.agent.grounding.grounding_labels import (
    FAR_BRANCH_SOURCE,
    apply_label,
    is_authoritative_source,
    label_for_generated_item,
    label_for_landed_item,
)
from app.agent.runtime.trace import record_event


def filter_plan_items(items: list[Any] | None) -> list[dict]:
    """落地前的脏项过滤（两条链路共用的一份实现）。"""
    return filter_dirty_items(items)


def drop_used_items(
    items: list[dict],
    used_names: list[str],
    *,
    day_no: int,
    used_points: list[tuple[str, str, float | None, float | None]] | None = None,
    ref_pool: ReferencePool | None = None,
) -> list[dict]:
    """单日接地后也走生成期判重；已安排名称和同日坐标共用 PoiSeenRegistry。

    `used_points`（P1-B）：跨天重试时已成功天落库的 (名字, 类型, 纬度, 经度)。
    只登记名字时，重生成日换个名称变体（"都厅展望室"→"都厅南展望室"）就绕过
    了坐标通道；带类型+坐标登记后，变体只要落地出真实坐标就会被同类型近距离
    判重抓到。酒店豁免由登记表统一定义；去重清空日程时，交图的失败/修复边界处理。
    `ref_pool`：给出时按池内实体身份区分同楼的不同餐厅（与整段链路同一判据）。
    """
    seen = PoiSeenRegistry()
    for name in used_names:
        seen.register(name, None)
    for point in used_points or []:
        if isinstance(point, tuple) and len(point) == 4:
            seen.register(*point)
    kept = []
    for item in items:
        name, kind = item["poi_name"], item["item_type"]
        latitude, longitude = item.get("latitude"), item.get("longitude")
        entity = ref_pool.entity_of(item) if ref_pool is not None else None
        if seen.is_duplicate(name, kind, latitude, longitude, entity):
            record_event("decision", "duplicate_dropped", metadata={"day_no": day_no, "poi_name": name})
            continue
        seen.register(name, kind, latitude, longitude, entity)
        kept.append(item)
    return kept


def ground_item(item: dict, *, city: str, ref_pool: ReferencePool) -> bool:
    """单个点位的事实落地：参考资料命中即回填权威字段，随后仍交给存在性解析器补坐标。

    返回是否命中参考资料（供调用方决定是否累计 used_names）。解析缓存由
    `existence` 自己按 (城市, 名字) 记忆化，这里不再传 cache。

    命中**不等于位置落地**：联网搜索的池行按设计只有名字/简介/估价，没有坐标
    （见 `web_search.search_places_via_web`）。命中就早退会让"有来源、没坐标"
    成为最终产出——地图钉与路线深链都拿不到位置（真实链路实测：
    `coord_valid_rate=0.25` 而 `existence_checks=1/24`，只有资料未命中的点被解析过）。
    这里因此照样调一次 `local_ground`：它自己在坐标已有效时早退（OTM 池行不多花
    一次外呼），记忆化命中也不重复扣解析额度。
    """
    hit = ref_pool.ground(item)
    local_ground(item, city)
    return hit


#: 去重/否证清空当日的判据标记：同 BUDGET_OVERAGE_MARK 口径，交图的有界重试
#: 按它识别「这条反馈值得再问模型一次」，消息与路由共用一份常量才不会漂。
DEDUP_EMPTIED_MARK = "去重或事实核验后没有可游玩景点"


def require_playable_items(before_names: list[str], items: list[dict], *, day_no: int, playable: bool) -> None:
    """把过滤导致的不可交付缺口连同被删名称反馈给下一次生成。"""
    removed = sorted(set(before_names) - {item["poi_name"] for item in items})
    if removed and playable and not any(item["item_type"] == "attraction" for item in items):
        raise ValueError(
            f"第 {day_no} 天{DEDUP_EMPTIED_MARK}；已移除：{'、'.join(removed)}。"
            "这些地点不可再次用于本日；保留其他天安排，改选目的地其他片区的真实景点和餐厅，补齐本日。"
        )


def drop_refuted_items(items: list[dict], *, city: str, report: dict[str, Any] | None = None) -> list[dict]:
    """删掉"与本次行程矛盾"的点位（PLAN-A1 G5 + 09-19 复评的两条证据）。

    只删两种，都在 `ResolveResult.deletable` 里判定：
    - 解析成功但落在别的城市/国家——这是正面矛盾，不是"我没查到"；
    - 有否证资格的 provider 明确回了"没有"（免费源没有这个资格：实测它们的
      空结果里约 70% 是真实地点，含秦始皇兵马俑与东京迪士尼）。

    已经拿到权威来源的项不再问一遍：落地阶段 `local_ground` 已经把结论存进
    existence 的记忆化表，重复问虽然不外呼但会白吃解析预算，预算要留给真正
    没有证据的名字。删除必须可观测（事件 + 报表计数），否则前端与 eval 只会
    看见"少了一个点"而不知道为什么少。收尾再做一次就近校正（`relocate_far_meals`）：
    远处分店不删点，只换成附近分店或撤销落地——它同样是"落地结论与本次行程矛盾"。
    """
    kept: list[dict] = []
    dropped: list[dict[str, str]] = []
    for item in items:
        name = str(item.get("poi_name") or "").strip()
        if not name or item.get("item_type") == "transport" or is_authoritative_source(item.get("source")):
            kept.append(item)
            continue
        result = _resolve(name, city)
        if result is None or not result.deletable:
            kept.append(item)
            continue
        dropped.append({"name": name, "provider": result.provider, "reason": result.reason or result.state})
    if dropped:
        record_event("decision", "refuted_poi_dropped", metadata={"items": dropped, "count": len(dropped)})
        if report is not None:
            report["refuted_pois_dropped"] = (report.get("refuted_pois_dropped") or []) + dropped
    relocate_far_meals(kept, city=city)
    return kept


def relocate_far_meals(items: list[dict], *, city: str) -> None:
    """落在远处分店的餐饮项：换成当天附近的同名分店，找不到就撤销落地。

    按名解析与参考资料命中两种来源都管——远处的池内行同样是真实实体，只是不在
    这一天的动线上；只校正服务端落地的位置（source 在权威值域内），别的坐标不归本规则。
    锚点是当天已落地的景点与酒店；没有锚点不判（没有参照就不猜）。半径常量与排程前的
    选池资格（`content/reference_pool`）共用 `rules/day_policy` 里的那一个数。
    撤销落地时清掉坐标、poi_id 与地址，标签退回生成态、source 记为 FAR_BRANCH_SOURCE：
    不背书、不查票，远处分店的饮食/类别证据挂不上来；坐标没了，转场修正也不会按
    19 km 去顺延餐次。换成附近分店时标签按该分店的票重算。由 `drop_refuted_items`
    收尾调用：两条链路都在 `fix_transfer_gaps` 之前走它；之后两处按名回填候选快照
    （单日链与 assembly）经 `facts.pool_row_for` 跳过这两类条目。
    """
    anchors = [
        (float(item["latitude"]), float(item["longitude"]))
        for item in items
        if item.get("item_type") in ("attraction", "hotel")
        and has_coord(item.get("latitude"))
        and has_coord(item.get("longitude"))
    ]
    if not anchors:
        return
    for item in items:
        if (
            item.get("item_type") != "food"
            or not is_authoritative_source(item.get("source"))
            or not (has_coord(item.get("latitude")) and has_coord(item.get("longitude")))
        ):
            continue
        lat, lng = float(item["latitude"]), float(item["longitude"])
        detour = min(haversine_m(lat, lng, a_lat, a_lng) for a_lat, a_lng in anchors)
        if detour <= MEAL_BRANCH_MAX_DISTANCE_M:
            continue
        name = str(item.get("poi_name") or "")
        near = next(
            (hit for query in _branch_queries(item) if (hit := _resolve_near(query, city, anchors)) and hit.grounded),
            None,
        )
        metadata = {"poi_name": name, "detour_m": round(detour)}
        if near is not None:
            if near.localized_name:
                item["poi_name"] = near.localized_name
            item["poi_id"] = near.external_id
            item["latitude"], item["longitude"] = near.latitude, near.longitude
            item["address"] = near.address
            item["source"] = near.provider
            item["evidence_id"] = near.evidence_id
            apply_label(item, label_for_landed_item(item, city))
            record_event("decision", "meal_branch_relocated", metadata=metadata)
            continue
        for key in ("latitude", "longitude", "poi_id", "address"):
            item[key] = None
        apply_label(item, label_for_generated_item())
        item["source"] = FAR_BRANCH_SOURCE
        record_event("decision", "far_meal_branch_ungrounded", metadata=metadata)


def _branch_queries(item: dict) -> list[str]:
    """就近重查用的写法：当前名在前，再补模型原写的名字（至多 2 个，每个扣一次解析额度）。

    `local_ground` 会把条目改名为 OSM 的中文名（name:zh），而日本店的 OSM 名多是日文，
    中文名限框重查容易落空。解析时签的票把模型原写的查询名记在 aliases 里。
    """
    name = str(item.get("poi_name") or "").strip()
    snapshot = evidence_row({"name": name, "source": item.get("source"), "evidence_id": item.get("evidence_id")})
    queries = [name, *((snapshot or {}).get("aliases") or ())]
    return list(dict.fromkeys(query for query in queries if query))[:2]


def _resolve_near(name: str, city: str, anchors: list[tuple[float, float]]) -> Any:
    """就近分店判定（延迟导入与故障口径同 `_resolve`）。"""
    from app.agent.grounding.nearby_branch import resolve_poi_near

    try:
        return resolve_poi_near(name, city, anchors, MEAL_BRANCH_MAX_DISTANCE_M)
    except Exception as exc:
        record_event("decision", "existence_check_failed", status="error", metadata={"error": str(exc)[:120]})
        return None


def _resolve(name: str, city: str) -> Any:
    """存在性判定（延迟导入：本模块被落地链调用，不应因判定层出错而拖垮落地）。"""
    from app.agent.grounding.existence import resolve_poi

    try:
        return resolve_poi(name, city)
    except Exception as exc:  # 判定层任何故障都按"未判定"处理，绝不当成"不存在"
        record_event("decision", "existence_check_failed", status="error", metadata={"error": str(exc)[:120]})
        return None
