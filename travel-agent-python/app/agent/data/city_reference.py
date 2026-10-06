"""城市级参考数据（MySQL 只读）：city_geo 字典与 city_consumption 消费基准。

POI 权威库退役后 agent 层仅存的数据库面，只含**城市级**事实——城市字典与消费
基准不随语料腐烂、不随行数增长；具体景点事实一律走 `app.agent.data.places`
（OpenTripMap/Nominatim）与 LLM，不再落库。

实现要点：
- 经 `app.common.db_pool` 连接池访问 MySQL；
- 统一 try/except 吞异常记日志，失败返回 None/空而非抛错，不中断生成链路；
- `is_domestic` 未命中字典时默认 False（海外），与 Atlas 的 dictMiss 口径一致：
  禁止默认按国内处理。

依赖：app.common.db_pool。
"""

from __future__ import annotations

import logging
from typing import Any, cast

from app.common import db_pool

logger = logging.getLogger(__name__)

_CITY_GEO_COLUMNS = "city_name, country, country_code, lat, lng, is_domestic"


def get_city_geo(city: str) -> dict | None:
    """城市字典行（含国内/海外判定与兜底坐标；坐标可能为 NULL——真实来源才填）。"""
    sql = f"SELECT {_CITY_GEO_COLUMNS} FROM city_geo WHERE city_name = %s LIMIT 1"
    try:
        with db_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (str(city or "").strip(),))
            return cast("dict[str, Any] | None", cursor.fetchone())
    except Exception as e:
        logger.error("get_city_geo failed: %s", e)
        return None


def get_city_consumption(city: str) -> dict | None:
    """城市消费基准（餐饮/交通/酒店价格档），预算与 Reflect 的城市级依据。"""
    sql = "SELECT city, level, meal_price, transport_price, hotel_price FROM city_consumption WHERE city = %s LIMIT 1"
    try:
        with db_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, (city,))
            row = cursor.fetchone()
            return cast("dict[str, Any] | None", row)
    except Exception as e:
        logger.error("get_city_consumption failed: %s", e)
        return None


def known_city_hits(text: str) -> list[str]:
    """文本中命中的字典城市名（按出现位置排序）；库不可达返回空列表=启发式不触发。

    供 clarify 的行程级荒谬预检用（2026-10-06 残留③：一天四城类输入的确定性
    识别）。字典只有 70 行整表扫描，days=1 才会走到这里，无需缓存。
    """
    sql = "SELECT city_name FROM city_geo"
    try:
        with db_pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cast("list[dict[str, Any]]", cursor.fetchall())
            names = [str(row["city_name"]) for row in rows]
    except Exception as e:
        logger.error("known_city_hits failed: %s", e)
        return []
    positions = [(text.find(name), name) for name in names if name and name in text]
    return [name for _, name in sorted(positions)]
