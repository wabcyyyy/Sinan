"""配置数值范围规则表（G-1.5 配置静态自洽的机检数据源）。

职责：
- 持有 Settings 全部数值字段的 (键, 人话要求, 下界, 上界) 规则与判定函数，
  供 `app/common/config.py` 的 `_static_validation` 消费——规则错在任何实例化
  （含测试 import 期）都以清晰文案炸出。

实现要点：
- 键名是 Settings 字段名的字符串镜像：改字段名时同步改这里，漏改会在
  `_static_validation` 里 KeyError（fail-loud，属于想要的行为）；
- 边界语义固定：下界开区间、上界闭区间（`low < value <= high`）；
- 从 config.py 拆出（2026-10-07，审计 §3.4.2/§3.3.3 修复批）：config.py 已顶到
  规模门禁的 400 行红线，本表是纯数据 + 纯函数、零内部依赖，是最安全的拆分面。

依赖：无（纯数据 + 纯函数）。
"""

from __future__ import annotations

# 数值范围规则：(键, 人话要求, 下界(开区间), 上界(闭区间))；默认值必须全部通过。
# max_replans 下界 -1 表示允许 0（显式关闭重规划）。
NUMERIC_RULES: tuple[tuple[str, str, float, float], ...] = (
    ("llm_timeout", "必须 > 0", 0, float("inf")),
    ("llm_connect_timeout", "必须 > 0", 0, float("inf")),
    ("agent_deadline_seconds", "必须 > 0", 0, float("inf")),
    ("db_port", "必须在 1-65535 之间", 0, 65535),
    ("db_pool_max", "必须 >= 1", 0, float("inf")),
    ("tool_max_calls", "必须 >= 1", 0, float("inf")),
    ("max_llm_calls", "必须 >= 1", 0, float("inf")),
    ("user_llm_runs_per_minute", "必须 >= 1", 0, float("inf")),
    ("user_daily_llm_runs", "必须 >= 1", 0, float("inf")),
    ("user_live_quotes_per_minute", "必须 >= 1", 0, float("inf")),
    ("agent_rate_limit_per_minute", "必须 >= 1", 0, float("inf")),
    ("max_retrievals", "必须 >= 1", 0, float("inf")),
    ("research_call_limit", "必须 >= 0", -1, float("inf")),
    ("research_workers", "必须 >= 1", 0, float("inf")),
    ("poi_search_workers", "必须 >= 1", 0, float("inf")),
    ("max_replans", "必须 >= 0", -1, float("inf")),
    ("jwt_expire_hours", "必须 >= 1", 0, float("inf")),
    ("login_ip_rate_per_minute", "必须 >= 1", 0, float("inf")),
    ("cover_upload_max_bytes", "必须 > 0", 0, float("inf")),
    ("otm_radius_m", "必须 > 0", 0, float("inf")),
    ("otm_limit", "必须 >= 1", 0, float("inf")),
    ("places_timeout_seconds", "必须 > 0", 0, float("inf")),
    ("existence_resolve_limit", "必须 >= 1", 0, float("inf")),
    ("existence_area_radius_m", "必须 > 0", 0, float("inf")),
    ("entity_name_similarity_min", "必须在 0-1 之间", 0, 1),
    ("suggestion_resolve_limit", "必须 >= 1", 0, float("inf")),
    ("share_rate_limit_per_minute", "必须 >= 1", 0, float("inf")),
    ("public_rate_limit_per_minute", "必须 >= 1", 0, float("inf")),
    ("serpapi_monthly_quota", "必须 >= 1", 0, float("inf")),
)
_RANGES = {key: (low, high) for key, _req, low, high in NUMERIC_RULES}


def numeric_ok(key: str, value: float) -> bool:
    low, high = _RANGES[key]
    return low < value <= high
