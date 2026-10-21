"""内置编辑规划的唯一数据入口；静态展示从同一 JSON 导出，不依赖数据库种子。"""

import json
from functools import lru_cache
from pathlib import Path

from app.schemas.template import TemplateDetailVO

CATALOG_PATH = Path(__file__).with_name("curated_itineraries.json")


@lru_cache(maxsize=1)
def curated_templates() -> tuple[TemplateDetailVO, ...]:
    """读取并校验规划参考；负数 ID 保留给内置内容，与数据库主键区分。"""
    rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    templates = tuple(TemplateDetailVO.model_validate(row) for row in rows)
    if any(row.id >= 0 or row.sourceType != "editorial" for row in templates):
        raise ValueError("内置规划必须使用负数 ID 并明确编辑来源")
    return templates
