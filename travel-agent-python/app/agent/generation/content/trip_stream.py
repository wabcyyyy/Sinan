"""整趟生成的流式逐项形态（M5a，spec §10.1）。

llm_open_trip_stream：与 day_prompts.llm_open_trip 同参同源（同一 prompt 组装、
输出 schema、max_tokens 公式、模型角色与联网开关），只把阻塞 complete 换成
stream_chat_deltas——文本增量逐段喂 TripPlanStreamParser（跨 chunk 字符串/转义/
嵌套/闭合由 ijson 负责），候选闭合即产出、EOF 过顶层校验与有界修复后才给终态。

消息契约（首元素 Literal 判别，先候选后终态）：
- ``("item_preview", day_no, item_ordinal, item)``：item JSON 完整闭合且过基础
  形状校验即发布；候选未过 ground，坐标/关键事实保持未知，仅供上层预览事件，
  不落库；
- ``("final", cleaned_plans, suggestions)``：EOF 后全文过顶层校验 + 有界修复
  （parse_llm_json_with_repair：解析失败/截断先携原请求重试一次，仍失败向上抛，
  上层走逐日 fallback——半截候选不能当成功）后才发布。

open_trip_streamed：候选泵——供 open_plans 的候选挂点消费，逐候选回调并回填
已发布序（整段腿失败时据此显式撤回，spec §10.2）。

**可 mock 契约**：单测经本模块的 `get_llm_client` / `llm_open_trip_stream` 注入桩
（调用期解析）；整腿泵经 `open_plans.open_trip_streamed` 注入。独立成模块是因为
day_prompts 已顶在代码规模门禁的 400 行上（G-2.6），不是随手拆分。

依赖：day_prompts（prompt/schema/温度常量）、incremental_plans（增量解析）、
json_output（顶层校验与有界修复）、common.llm_client / model_registry。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any, Literal

from app.agent.generation.content.day_prompts import GENERATION_TEMPERATURE, open_trip_output_schema, open_trip_prompt
from app.agent.generation.content.incremental_plans import TripPlanStreamParser, TruncatedTripPlanError
from app.agent.generation.content.json_output import parse_llm_json_with_repair, validate_trip_output
from app.agent.generation.content.narrative import assemble_trip_output
from app.common.config import settings
from app.common.llm_client import get_llm_client
from app.common.model_registry import json_response_format, model_for
from app.schemas.trip import GenerateDayRequest

logger = logging.getLogger(__name__)

#: llm_open_trip_stream 的两种消息（首元素 Literal 判别，供消费方收窄）：
#: 候选（day_no, item_ordinal, item 开放形状 dict）与终态（清洗后 plans, suggestions）。
TripStreamMessage = (
    tuple[Literal["item_preview"], int, int, dict[str, Any]] | tuple[Literal["final"], list[dict], list[dict]]
)


def llm_open_trip_stream(
    req: GenerateDayRequest, *, cancel: threading.Event | None = None
) -> Iterator[TripStreamMessage]:
    """开放模式多日流式生成（M5a，spec §10.1）：阻塞 complete 换 stream_chat_deltas。

    与 llm_open_trip 同参同源（open_trip_prompt / open_trip_output_schema /
    json_response_format / max_tokens 公式 / model_for("fast") / 联网开关 /
    enable_thinking=False），只把交付形态改为「边流边解析」。

    cancel：客户端断开信号，透传 stream_chat_deltas（取消以 StreamCancelled
    终止，不产出 final）。不挂 @traced：生成器函数被装饰会改变惰性语义，
    且 stream_chat_deltas 已自带 llm.stream_request 轨迹。
    """
    client = get_llm_client()
    system, user = open_trip_prompt(req)
    days = req.days or 1
    output_schema = open_trip_output_schema()
    # 预算公式与 llm_open_trip 同源等比（见该函数注），流式同样按天给足，
    # 不以截断换首字节延迟。
    repair_tokens = max(2800, min(16000, days * 8000))
    response_format = json_response_format("fast", "trip_output", output_schema)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    parser = TripPlanStreamParser()
    parts: list[str] = []
    deltas = client.stream_chat_deltas(
        messages,
        temperature=GENERATION_TEMPERATURE,
        max_tokens=repair_tokens,
        model=model_for("fast"),
        response_format=response_format,
        enable_search=settings.llm_generation_web_search,
        cancel=cancel,
        enable_thinking=False,
    )
    for delta in deltas:
        parts.append(delta)
        for candidate in parser.feed(delta):
            yield ("item_preview", candidate.day_no, candidate.item_ordinal, candidate.item)
    raw = "".join(parts)
    try:
        # EOF 顶层完整性门：未闭合 = TruncatedTripPlanError。此处只记日志不直接
        # 失败——截断文本交给下方 parse_llm_json_with_repair 走一次有界修复
        # （全文 parse 必然失败 → 携原请求重生成一次），修复再失败才向上抛。
        parser.finish()
    except TruncatedTripPlanError as exc:
        logger.warning("open trip stream ended truncated (%s days requested): %s", days, exc)
    data = parse_llm_json_with_repair(
        raw,
        client,
        model=model_for("fast"),
        repair_max_tokens=repair_tokens,
        response_format=response_format,
        messages=messages,
        validate=lambda data: validate_trip_output(data, days),
    )
    cleaned_plans, suggestions = assemble_trip_output(data, days)
    yield ("final", cleaned_plans, suggestions)


def open_trip_streamed(
    trip_req: GenerateDayRequest,
    on_item_preview: Callable[[int, int, dict], None],
    published: list[tuple[int, int]],
) -> tuple[list[dict], list[dict]]:
    """M5a 流式整段腿的候选泵：逐候选回调，final 段原样返回给调用方。

    `published` 由本函数回填（day_no, item_ordinal）序——整段腿失败时调用方
    据此逐个撤回已发布候选（spec §10.2：淘汰候选显式撤回，不静默换成别的地点）。
    """
    for message in llm_open_trip_stream(trip_req):
        match message:
            case ("item_preview", day_no, item_ordinal, item):
                published.append((int(day_no), int(item_ordinal)))
                on_item_preview(int(day_no), int(item_ordinal), dict(item))
            case ("final", cleaned_plans, suggestions):
                return cleaned_plans, suggestions
    raise ValueError("llm_open_trip_stream 未产出 final（生成器提前结束，不冒充成功）")
