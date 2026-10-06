"""参考资料池：权威候选 → 带编号参考资料 → 模型引用落地（G-2.2 自 generators 拆出）。

职责：
- normalize_poi_name：名称归一，供引用匹配；
- ReferencePool：把本次候选编成可被模型引用的 [Rn] 资料块，并把模型输出的
  refs 引用落地回权威字段。

实现要点：
- 背书判定不在本模块：来源是否可信、缺坐标怎么降级，全部交给
  `grounding_labels.label_for_evidence_row`（三条链路共用的唯一实现）；
- 来源值域校验不可省：/v1/generate-day 的 context 由调用方传入属不可信输入，
  不做值域校验则调用方可伪造 "opentripmap" 让幻觉事实获得外部背书。

依赖：existence（same_entity 名称门槛）、grounding_labels；无上层依赖。
"""

import re

from app.agent.grounding.existence import same_entity
from app.agent.grounding.grounding_labels import (
    UNTRUSTED_SOURCE,
    apply_label,
    has_valid_coords,
    is_trusted_row,
    label_for_evidence_row,
)

# 开放模式参考资料注入规模：把本次候选池（OTM/联网搜索/LLM）拿到的点位按编号
# 提供给模型，选点优先从资料中挑并输出 refs 引用编号；规模过大不再加。
REFERENCE_ATTRACTION_LIMIT = 16
REFERENCE_FOOD_LIMIT = 6
REFERENCE_HOTEL_LIMIT = 4

_NAME_NORMALIZE_RE = re.compile(r"[\s（）()【】\[\]·]")


def normalize_poi_name(name: str | None) -> str:
    """归一化地点名：去空白/括号/间隔号，供引用匹配使用。"""
    return _NAME_NORMALIZE_RE.sub("", str(name or "")).strip()


class ReferencePool:
    """开放模式的权威参考资料池：编号、注入 Prompt、行程项匹配与使用统计。

    P1 引用式生成的核心：候选池不再只是保底链路的白名单，还被格式化为
    带编号参考资料 [R1]…[Rn] 注入开放模式 Prompt；模型选点时输出 refs
    编号，生成后由 ground_reference_item 落地为权威字段并回填真实来源。
    """

    def __init__(self, context: dict | None, exclude_names: set[str] | None = None) -> None:
        self.references: list[dict] = self._collect(context, exclude_names)
        self.by_name: dict[str, dict] = {}
        self.by_normalized: dict[str, dict] = {}
        for poi in self.references:
            name = str(poi.get("name") or "").strip()
            if name:
                self.by_name.setdefault(name, poi)
                self.by_normalized.setdefault(normalize_poi_name(name), poi)
        self.cited_names: set[str] = set()
        self.stats: dict[str, int] = {
            "references": len(self.references),
            "items": 0,
            "grounded": 0,
            "refs_cited": 0,
            "refs_valid": 0,
            "cited_references": 0,
        }

    @staticmethod
    def _collect(context: dict | None, exclude_names: set[str] | None = None) -> list[dict]:
        """收集参考资料；exclude_names 过滤"已排入/已去过"的 POI。

        注意：过滤会改变 refs 编号，Prompt 渲染（block）与落地（ground）
        必须使用同一 exclude_names 构造的同一个池，编号才对齐。
        """
        ctx = context or {}
        excluded = exclude_names or set()
        collected: list[dict] = []
        seen: set[str] = set()
        for key, limit in (
            ("candidates", REFERENCE_ATTRACTION_LIMIT),
            ("foods", REFERENCE_FOOD_LIMIT),
            ("hotels", REFERENCE_HOTEL_LIMIT),
        ):
            rows = ctx.get(key)
            if not isinstance(rows, list):
                continue
            kept = 0
            for poi in rows:
                if kept >= limit:
                    break
                if not isinstance(poi, dict):
                    continue
                name = str(poi.get("name") or "").strip()
                if not name or name in seen or name in excluded:
                    continue
                seen.add(name)
                collected.append(poi)
                kept += 1
        return collected

    def __len__(self) -> int:
        return len(self.references)

    def block(self) -> str:
        """渲染为注入 Prompt 的参考资料块；空池返回空串。"""
        lines: list[str] = []
        for no, poi in enumerate(self.references, start=1):
            segments = [str(poi.get("category") or "attraction")]
            if poi.get("ticket_price") is not None:
                segments.append(f"票价{float(poi['ticket_price']):g}")
            for key in ("open_time", "address"):
                value = str(poi.get(key) or "").strip()
                if value:
                    segments.append(value)
            lines.append(f"[R{no}] {poi.get('name')}｜" + "｜".join(segments))
        if not lines:
            return ""
        return (
            "权威参考资料（本地知识库，事实可信；行程与备选点优先从这里选，"
            "选中时必须在 item 中输出 refs:[对应编号，如 3]；"
            "资料中没有合适点位时才可用你的知识补充真实存在的地点，无需 refs，禁止编造）：\n" + "\n".join(lines)
        )

    def match(self, item: dict) -> dict | None:
        """行程项 → 参考资料匹配：名称精确/归一化优先，其次 refs 编号。"""
        name = str(item.get("poi_name") or "").strip()
        poi = self.by_name.get(name) or self.by_normalized.get(normalize_poi_name(name))
        if poi is not None:
            return poi
        for ref in item.get("refs") or []:
            if isinstance(ref, bool) or not isinstance(ref, int):
                continue
            if 1 <= ref <= len(self.references):
                return self.references[ref - 1]
        return None

    def _count_refs(self, item: dict) -> None:
        refs = item.get("refs") or []
        valid_ints = [
            r for r in refs if isinstance(r, int) and not isinstance(r, bool) and 1 <= r <= len(self.references)
        ]
        if refs:
            self.stats["refs_cited"] += 1
            if valid_ints:
                self.stats["refs_valid"] += 1

    def ground(self, item: dict) -> bool:
        """把命中参考资料的行程项落地为权威字段；返回是否命中。

        名称命中时保留模型名称；refs 命中时名称归一为权威名，保证后续
        按名查找（format_output / 单日 lookup）一致。transport 为合成项，
        不参与匹配。refs 是生成中间产物，计数与匹配完成后移除。
        """
        if item.get("item_type") == "transport" or not str(item.get("poi_name") or "").strip():
            item.pop("refs", None)
            return False
        self.stats["items"] += 1
        self._count_refs(item)
        name = str(item.get("poi_name") or "").strip()
        poi = self.match(item)
        item.pop("refs", None)  # 匹配完成后移除中间产物，避免进入 TripItem
        if poi is None:
            return False
        # match() 仅在名称查找（原名/归一化名）都未命中时才落到 refs 编号，
        # 因此这里直接按名称查找即可区分两种引用方式。
        matched_by_name = name in self.by_name or normalize_poi_name(name) in self.by_normalized
        if not matched_by_name and not same_entity(name, str(poi.get("name") or "")):
            # refs 编号与名字对不上：名称通道没命中、只凭模型给的编号就把权威行的
            # 坐标 graft 到一个不相干的名字上，等于给幻觉发证据（D7：池命中也要
            # 过同一实体判定）。宁可退回"位置待核实"，也不交出可能被指错的位置。
            self.stats["refs_gate_rejected"] = self.stats.get("refs_gate_rejected", 0) + 1
            return False
        self.stats["grounded"] += 1
        poi_name = str(poi.get("name") or "")
        self.cited_names.add(poi_name)
        self.stats["cited_references"] = len(self.cited_names)
        if not matched_by_name:
            item["poi_name"] = poi_name
        item["item_type"] = poi.get("category") or item.get("item_type") or "attraction"
        # 背书判定唯一实现：值域外的来源（例如客户端伪造的 context）改写为
        # client-context 并降级；缺 source 的池内行按 pool-unknown 记账——
        # 既不挂"权威知识库"的名号（该表已于 V4 退役），也不谎称是客户端传入。
        label = label_for_evidence_row(poi)
        apply_label(item, label)
        if label.source == UNTRUSTED_SOURCE:
            self.stats["untrusted_grounded"] = self.stats.get("untrusted_grounded", 0) + 1
        # 来源不可信的行一个字段都不采纳：伪 source 不只是让徽章失真，还能把
        # 真实景点指到调用自选的坐标/poi_id 上（P6）。
        if not is_trusted_row(poi):
            return True
        item["poi_id"] = str(poi.get("id") or "") or item.get("poi_id")
        item["address"] = poi.get("address")
        if has_valid_coords(poi):
            item["latitude"] = float(poi["latitude"])
            item["longitude"] = float(poi["longitude"])
        if poi.get("duration_min"):
            item["duration_min"] = int(poi["duration_min"])
        if poi.get("open_time"):
            item["open_time"] = poi.get("open_time")
        if poi.get("ticket_price") is not None:
            item["cost"] = float(poi["ticket_price"])
        return True
