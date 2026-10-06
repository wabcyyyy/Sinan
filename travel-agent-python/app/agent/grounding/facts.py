"""事实落地：把点位名变成带真实坐标与来源的事实（G-2.2 自 day_stream 拆出）。

职责：
- has_coord：坐标存在且非 0/0（0/0 是缺失哨兵）；
- local_ground：开放模式自选点位经**存在性解析器**（`existence`）补真实坐标与来源。

依赖：existence（可插拔 provider、三值结论、名称门槛、位置闸、预算）；无上层依赖。
缓存不在这层：`existence` 自带 (城市, 名字) 记忆化、provider 侧还有 TTL，这里再叠
一层进程内 dict 只会多出第二份真相（旧的 `cache` 参数就是这么长出来的）。
"""

from __future__ import annotations

from typing import Any

from app.agent.grounding.existence import resolve_poi


def has_coord(value: Any) -> bool:
    """坐标有效：非 None 且非 0.0（0/0 是缺失哨兵，与 find_nearby_pois 口径一致）。"""
    try:
        return value is not None and abs(float(value)) > 1e-6
    except (TypeError, ValueError):
        return False


def local_ground(item: dict[str, Any], city: str) -> bool:
    """用存在性解析器落坐标/地址，并把**真跑过的 provider** 写进 item.source。

    返回是否接地成功。三条纪律：
    - 未判定（没问到 / 超时 / 名字对不上）与"解析到别的城市"都原样保留，
      由上层按证据不足降级——绝不把"没查到"当成"不存在"；
    - `source` 只在这里由服务端写入，它是背书的唯一凭据（见 grounding_labels）；
    - 已经拿到系统坐标的项不再解析（参考资料落地给的就够了，省一次外呼）。

    票价/营业时间数据源不提供，恒为 LLM 估价（value_kind=estimated）。
    """
    name = str(item.get("poi_name") or "").strip()
    if not name or (has_coord(item.get("latitude")) and has_coord(item.get("longitude"))):
        return False
    result = resolve_poi(name, city)
    if not result.grounded:
        return False
    item["latitude"] = result.latitude
    item["longitude"] = result.longitude
    if result.address and not item.get("address"):
        item["address"] = result.address
    item["source"] = result.provider
    return True
