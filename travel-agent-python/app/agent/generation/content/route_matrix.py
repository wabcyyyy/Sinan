"""按日构建路线时间矩阵（reflect 与 format 两个阶段共用）。

只为同一天的相邻候选构建矩阵，避免跨日无效路线调用。逐日调用会让工具预算
按整个 run 累计（reflect N 天 + format N 天 = 2N 次），因此 `get_route_matrix`
的 max_calls 必须覆盖最长行程；这里同时跳过空日，减少无意义消耗。
同日重复查询由 RouteService 的进程内缓存吸收。
"""

from app.agent.tools.registry import registry
from app.common.config import settings


def route_matrix_for_plans(plans: list[dict]) -> dict:
    matrix: dict = {}
    for plan in plans:
        active = [item for item in plan.get("items") or [] if item.get("item_type") in ("attraction", "food")]
        if len(active) < 2:
            continue
        matrix.update(
            registry.invoke(
                "get_route_matrix",
                {
                    "items": active,
                    "mode": settings.route_mode,
                },
            )
        )
    return matrix
