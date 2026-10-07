"""生成出口的内容校验与有界修复：JSON 语法正确不等于行程可用。

parse_llm_json_with_repair 保留原请求及校验原因重试一次；validate_day_output /
validate_trip_output 钉住必需的行程骨架，可选叙事仍由 narrative 清洗。
"""

from collections.abc import Callable

from app.agent.core.json_utils import LlmJsonError, parse_llm_json
from app.agent.runtime.trace import record_event
from app.common.model_registry import model_for


def validate_day_output(data: dict) -> None:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        raise LlmJsonError("行程缺少非空 items，必须包含本日景点、餐饮与时间安排")
    for item in items:
        if not isinstance(item, dict) or not str(item.get("poi_name") or "").strip():
            raise LlmJsonError("行程条目必须包含非空 poi_name")
        if item.get("item_type") not in {"attraction", "food", "hotel"}:
            raise LlmJsonError("行程条目 item_type 必须为 attraction、food 或 hotel")
    if not any(item["item_type"] != "hotel" for item in items):
        raise LlmJsonError("行程不能只有住宿，必须包含本日活动")


def validate_trip_output(data: dict, days: int) -> None:
    plans = data.get("daily_plans")
    if not isinstance(plans, list) or not plans:
        raise LlmJsonError("整段行程缺少非空 daily_plans")
    seen = set()
    for plan in plans:
        if not isinstance(plan, dict):
            raise LlmJsonError("daily_plans 每一天必须为对象")
        day_no = plan.get("day_no")
        if type(day_no) is not int or not 1 <= day_no <= days or day_no in seen:
            raise LlmJsonError("day_no 必须在请求天数内且不重复")
        seen.add(day_no)
        validate_day_output(plan)
    # 缺少的日期仍由 open_plans 的逐日路径补齐，已有效的日期无需丢弃。


def parse_llm_json_with_repair(
    raw: object,
    client,
    *,
    model: str | None = None,
    repair_max_tokens: int = 4000,
    response_format: dict | None = None,
    messages: list[dict] | None = None,
    validate: Callable[[dict], None] | None = None,
) -> dict:
    """语法或内容失败 → 携原请求修复一次 → 再失败向上抛，禁止空对象冒充成功。"""
    try:
        data = parse_llm_json(raw)
        if validate:
            validate(data)
        return data
    except LlmJsonError as exc:
        reason = str(exc)
        record_event("decision", "llm_json_repair", metadata={"raw_len": len(str(raw or "")), "reason": reason})
    context = list(messages or [{"role": "system", "content": "只输出语法正确的 JSON 对象，不要解释。"}])
    if str(raw or "").strip():
        context.append({"role": "assistant", "content": str(raw)[:32000]})
    context.append(
        {
            "role": "user",
            "content": (
                f"上一轮输出不可用：{reason}。依据原请求和输出契约重新输出完整 JSON 对象；不得以空对象或空行程替代。"
            ),
        }
    )
    repaired = client.chat(
        context,
        temperature=0,
        max_tokens=repair_max_tokens,
        model=model or model_for("fast"),
        json_mode=response_format is None,
        response_format=response_format,
        enable_thinking=False,
    )
    data = parse_llm_json(repaired)
    if validate:
        validate(data)
    return data
