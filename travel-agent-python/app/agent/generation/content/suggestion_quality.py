"""备选点的设施语义检查：交通线路与有旅游价值的设施区分。"""

import re


def is_transport_corridor(name: str) -> bool:
    """整条交通线路不是游览点；保留明确的铁路博物馆、遗产及观光铁路。

    OTM 的铁路线可被上游标为 bridges/viaducts，不能仅凭 attraction 类别
    放行。按名称中的设施类型判定，不硬编码城市或某一条路线。
    """
    if re.search(r"museum|heritage|historic|scenic|tourist|博物馆|遗产|历史|观光|景观", name, re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"\b(?:railways?|railroads?|expressways?|motorways?)$|(?:铁路线?|高铁线|高速公路)$", name, re.IGNORECASE
        )
    )
