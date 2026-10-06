"""自然语言增量编辑解析：把用户一句话翻译成结构化操作（不全量重生成）。"""

import json
import logging

from app.common.llm_client import get_llm_client
from app.schemas.trip import EditOp, EditOpRequest

logger = logging.getLogger(__name__)

_ALLOWED = {"delete", "add", "update_time", "move_day", "upgrade_hotel"}


def run_edit_ops(req: EditOpRequest) -> list[EditOp]:
    client = get_llm_client()
    system = (
        "你是行程编辑指令解析器。把用户指令翻译为对现有行程的操作序列，只输出 JSON："
        '{"ops":[{"action":"delete|add|move_day|update_time|upgrade_hotel","day_no":数字或null,'
        '"poi_name":"景点名或null","start_time":"HH:mm或null","tier":"档次或null"}]}。'
        "规则：每条 op 的 day_no 除 upgrade_hotel 外尽量给出；删除=delete；"
        "新增=add(poi_name 用行程中已有名称或知名真实景点)；改时间=update_time；换到第几天=move_day；"
        "对酒店档次/品质/价格不满意要求换酒店=upgrade_hotel(tier 填 经济型/舒适型/高档型/豪华型/奢华型，"
        "未指明档次则填 null)。不要发明其他 action；最多 8 条。"
    )
    raw = client.complete(
        f"目的地 {city_of(req)}，共 {req.days} 天。\n"
        f"当前行程：{json.dumps(_compact(req.plans), ensure_ascii=False)}\n"
        f"用户指令：{req.instruction}",
        system_prompt=system,
        temperature=0,
    )
    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
        ops = [o for o in data.get("ops", []) if o.get("action") in _ALLOWED]
        return [
            EditOp(**{k: o.get(k) for k in ("action", "day_no", "poi_name", "start_time", "tier")}) for o in ops[:8]
        ]
    except Exception as e:
        logger.warning("edit ops parse failed: %s | raw=%s", e, raw[:200])
        raise ValueError("没能理解这条修改指令，请换种说法") from e


def city_of(req: EditOpRequest) -> str:
    return req.city


def _compact(plans: list[dict]) -> list[dict]:
    out = []
    for p in plans:
        out.append(
            {
                "day_no": p.get("day_no"),
                "items": [
                    {"item_type": i.get("item_type"), "poi_name": i.get("poi_name"), "start_time": i.get("start_time")}
                    for i in (p.get("items") or [])
                ],
            }
        )
    return out
