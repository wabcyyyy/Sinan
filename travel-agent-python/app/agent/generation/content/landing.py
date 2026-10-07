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

from app.agent.core.poi_identity import PoiSeenRegistry
from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.generation.rules.generation_core import filter_dirty_items
from app.agent.grounding.facts import local_ground
from app.agent.grounding.grounding_labels import is_authoritative_source
from app.agent.runtime.trace import record_event


def filter_plan_items(items: list[Any] | None) -> list[dict]:
    """落地前的脏项过滤（两条链路共用的一份实现）。"""
    return filter_dirty_items(items)


def drop_used_items(items: list[dict], used_names: list[str], *, day_no: int) -> list[dict]:
    """单日接地后也走生成期判重；已安排名称和同日坐标共用 PoiSeenRegistry。

    酒店豁免由登记表统一定义；去重清空日程时，交图的失败/修复边界处理。
    """
    seen = PoiSeenRegistry()
    for name in used_names:
        seen.register(name, None)
    kept = []
    for item in items:
        name, kind = item["poi_name"], item["item_type"]
        latitude, longitude = item.get("latitude"), item.get("longitude")
        if seen.is_duplicate(name, kind, latitude, longitude):
            record_event("decision", "duplicate_dropped", metadata={"day_no": day_no, "poi_name": name})
            continue
        seen.register(name, kind, latitude, longitude)
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


def drop_refuted_items(items: list[dict], *, city: str, report: dict[str, Any] | None = None) -> list[dict]:
    """删掉"与本次行程矛盾"的点位（PLAN-A1 G5 + 09-19 复评的两条证据）。

    只删两种，都在 `ResolveResult.deletable` 里判定：
    - 解析成功但落在别的城市/国家——这是正面矛盾，不是"我没查到"；
    - 有否证资格的 provider 明确回了"没有"（免费源没有这个资格：实测它们的
      空结果里约 70% 是真实地点，含秦始皇兵马俑与东京迪士尼）。

    已经拿到权威来源的项不再问一遍：落地阶段 `local_ground` 已经把结论存进
    existence 的记忆化表，重复问虽然不外呼但会白吃解析预算，预算要留给真正
    没有证据的名字。删除必须可观测（事件 + 报表计数），否则前端与 eval 只会
    看见"少了一个点"而不知道为什么少。
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
    return kept


def _resolve(name: str, city: str) -> Any:
    """存在性判定（延迟导入：本模块被落地链调用，不应因判定层出错而拖垮落地）。"""
    from app.agent.grounding.existence import resolve_poi

    try:
        return resolve_poi(name, city)
    except Exception as exc:  # 判定层任何故障都按"未判定"处理，绝不当成"不存在"
        record_event("decision", "existence_check_failed", status="error", metadata={"error": str(exc)[:120]})
        return None
