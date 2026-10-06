"""请求内 WorkingMemory：跨节点共享「已用点位 / 选定酒店 / 校验反馈」。

从 day_stream / workflow 中散落的 used set 抽出，保证：
- 多日不重复景点；
- ReferencePool exclude 与 Prompt 规避名单一致；
- 状态可测、可扩展（后续 profile 可并入 context builder）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agent.core.poi_identity import norm_ws_key

_norm = norm_ws_key


@dataclass
class WorkingMemory:
    used_names: set[str] = field(default_factory=set)
    chosen_hotel: str | None = None
    feedback: str = ""

    @classmethod
    def from_iterable(cls, names: list[str] | set[str] | None) -> WorkingMemory:
        return cls(used_names={str(n).strip() for n in (names or []) if str(n).strip()})

    def as_sorted_list(self) -> list[str]:
        return sorted(self.used_names)

    def mark_used(self, name: str | None) -> None:
        name = str(name or "").strip()
        if name:
            self.used_names.add(name)

    def mark_items_used(self, items: list[dict] | None) -> None:
        for item in items or []:
            if isinstance(item, dict):
                self.mark_used(item.get("poi_name") or item.get("name"))

    def exclude_names(self) -> set[str]:
        return set(self.used_names)

    def filter_unused(self, items: list[dict] | None) -> list[dict]:
        """过滤已用点；空集即「候选耗尽」，由调用方降级。

        不得在空集时回退全集：那会把已用 POI 重新注入候选，破坏跨天去重。
        """
        return [i for i in (items or []) if i.get("name") not in self.used_names]

    def choose_hotel(self, name: str | None) -> None:
        name = str(name or "").strip()
        if name and not self.chosen_hotel:
            self.chosen_hotel = name

    def set_feedback(self, feedback: str | None) -> None:
        self.feedback = feedback or ""

    def snapshot(self) -> dict:
        return {
            "used_count": len(self.used_names),
            "chosen_hotel": self.chosen_hotel,
            "has_feedback": bool(self.feedback),
        }
