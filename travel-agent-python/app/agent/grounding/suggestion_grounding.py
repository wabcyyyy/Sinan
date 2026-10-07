"""备选池的批量后验证（PLAN-A1 G6-B）。

「发现更多」那 24-40 个名字是模型一次性产出的，生成链路既不落地也不反思它们，
所以本模块是它们唯一一次被外部数据源检验。与 `suggestions`（组装/地板/补齐）分开，
是因为两者的调用方完全不同：前者在生成的同步链路里，后者在生成完成后的富化线程里。

依赖：existence（三值判定）、trace（可观测）；无编排依赖。
"""

from app.agent.runtime.trace import record_event
from app.common.config import settings


def verify_suggestion_rows(
    rows: list[dict], city: str, *, limit: int | None = None
) -> tuple[list[dict], dict[str, int]]:
    """逐个问"这个名字真的存在吗"，回填坐标、丢弃与本次行程矛盾的点。

    备选池是模型一次产出的 24-40 个名字，生成链路既不落地也不反思它们，所以这里
    是它们唯一一次被外部数据源检验。三种结局：

    - **证实** → 回填经纬度/地址/poiId：前端能正常打点，用户"加入行程"时也不必
      再解析一次；
    - **与本次行程矛盾**（解析成功但落在别的城市，或有否证资格的源明确回了"没有"）
      → 丢弃：备选面板不需要可疑点（09-18 裁决 D8-B；主行程按同一实测结论不删，
      因为免费源的空结果里约 70% 是真实地点）；
    - **未判定** → 原样保留，坐标留空，前端按名称走关键词深链。

    `limit` 是后台富化的独立预算（默认 `suggestion_resolve_limit`）：这一趟不该
    吃掉下一趟主行程的额度。行可能是生成态（snake_case）也可能是库里读出来的
    （camelCase，`suggestions_json` 的存法），所以 poiId 两种写法都认。
    """
    from app.agent.grounding.existence import resolve_poi
    from app.agent.grounding.facts import has_coord

    cap = settings.suggestion_resolve_limit if limit is None else max(int(limit), 0)
    stats = {"filled": 0, "dropped": 0, "unresolved": 0, "skipped": 0}
    kept: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            kept.append(row)
            continue
        name = str(row.get("name") or row.get("poi_name") or "").strip()
        # 0/0 是缺失坐标的哨兵值，不能当成"已核实过"而跳过
        has_coords = has_coord(row.get("latitude")) and has_coord(row.get("longitude"))
        if not name or has_coords:
            stats["skipped"] += 1  # 池内行本就来自外部数据，不重复问
            kept.append(row)
            continue
        if stats["filled"] + stats["dropped"] + stats["unresolved"] >= cap:
            stats["skipped"] += 1
            kept.append(row)
            continue
        result = resolve_poi(name, city)
        if result.deletable:
            stats["dropped"] += 1
            continue
        if result.grounded:
            if result.localized_name:
                row["name" if "name" in row else "poi_name"] = result.localized_name
            row["latitude"] = result.latitude
            row["longitude"] = result.longitude
            if result.address and not row.get("address"):
                row["address"] = result.address
            if result.external_id and not (row.get("poiId") or row.get("poi_id")):
                _put_poi_id(row, result.external_id)
            stats["filled"] += 1
        else:
            stats["unresolved"] += 1
        kept.append(row)
    if any(stats[key] for key in ("filled", "dropped")):
        record_event("decision", "suggestions_verified", metadata={"city": city, **stats})
    return kept, stats


def _put_poi_id(row: dict, value: str) -> None:
    """按行现有的键写法回填 poiId（camel=库内形状，snake=生成态形状）。"""
    key = "poiId" if "poiId" in row or "poi_id" not in row else "poi_id"
    row[key] = value
