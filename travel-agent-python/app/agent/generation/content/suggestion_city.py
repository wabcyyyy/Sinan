"""备选池城市归属校验（BIZ-3 起自 stream_branch 收拢为全链单一真源）。

三条生成链——整段（assembly）/ 逐日（day_stream）/ 流式（stream_branch）——的
「发现更多」备选池都在 build_suggestions(dest_city=...) 里过这道校验，模型自报
城市与目的地不符的建议（如把示例城市的店铺照抄进来）在此出局。

依赖：poi_identity 的归一化键（core 之下，无编排依赖）。
"""

from __future__ import annotations

import re

from app.agent.core.poi_identity import norm_poi_key
from app.agent.runtime.trace import record_event

_CJK_RE = re.compile(r"[一-鿿]")


def _city_label_match(raw_city: str, dest_city: str) -> bool:
    """suggestions 城市归属校验：归一化全等或互为包含（兼容中英注记）。

    "巴塞罗那（Barcelona）" 这类双语注记：除整体外，把括号内的外文名单独成词
    参与比对，避免括号剥离后拉丁名丢失导致误杀。
    BIZ-3：纯外文城市名（raw="Barcelona"、dest="巴塞罗那"）在 norm 无翻译的
    前提下不可比——两边文字体系不同（一边含汉字一边纯拉丁）时宁可保留：
    误杀真建议的伤害大于放过一条外城建议，后者还有终检与前端地图兜底。
    """
    b = norm_poi_key(dest_city)
    if not b:
        return False
    dest_has_cjk = bool(_CJK_RE.search(dest_city))
    text = str(raw_city or "")
    tokens = [text, *re.findall(r"[（(【\[〔]([^）)】\]〕]*)[）)】\]〕]", text)]
    for token in tokens:
        a = norm_poi_key(token)
        if not a:
            continue
        if a == b or a in b or b in a:
            return True
        if bool(_CJK_RE.search(token)) != dest_has_cjk:
            return True  # 跨文字体系不可比：不做判定，保留
    return False


def filter_suggestions_by_city(raw: list[dict], city: str) -> list[dict]:
    """丢弃模型自报城市与目的地不符的建议（如把示例城市的店铺照抄进来）。"""
    kept: list[dict] = []
    dropped = 0
    for s in raw:
        c = str(s.get("city") or s.get("poiCity") or "").strip()
        if c and not _city_label_match(c, city):
            dropped += 1
            record_event(
                "decision",
                "suggestion_city_mismatch_dropped",
                metadata={"city": c, "poi_name": str(s.get("poi_name") or "")},
            )
            continue
        kept.append(s)
    if dropped:
        record_event(
            "decision", "suggestion_city_filter", metadata={"dest": city, "dropped": dropped, "kept": len(kept)}
        )
    return kept
