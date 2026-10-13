"""Step 2: real low-thinking calls through each production entry; record finish_reason / tokens / time / JSON.

Usage: uv run python ../.tmp-release-audit/lowthink/budgets.py <out_dir> [entries...]
entries: trip trip_stream day repair clarify (default all)
"""

import json
import sys
import time

from common import ROOT, CallLog, day_request, frozen_context, strip_content, thinking_snapshot

from app.agent.editing.clarify import run_clarify
from app.agent.generation.content import day_prompts, trip_stream
from app.agent.generation.content.json_output import parse_llm_json_with_repair, validate_trip_output
from app.agent.generation.orchestration import day_stream
from app.common.llm_client import get_llm_client
from app.common.model_registry import json_response_format, model_for
from app.schemas.trip import ClarifyRequest

out = ROOT / ".tmp-release-audit/lowthink" / sys.argv[1]
out.mkdir(parents=True, exist_ok=True)
entries = sys.argv[2:] or ["trip", "trip_stream", "day", "repair", "clarify"]
log = CallLog()
log.install()
results: list[dict] = []
(out / "thinking.json").write_text(json.dumps(thinking_snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
CLARIFY_MSG = "东京2天，2个人，预算3000元人民币，带70岁老人，慢节奏，不去寺庙，每天最多两个景点，素食可吃蛋奶。"


def run(entry: str, fn) -> None:
    log.entry = entry
    start = len(log.calls)
    t0 = time.monotonic()
    rec: dict = {"entry": entry}
    try:
        value = fn()
        rec["outcome"] = "ok"
        rec["result"] = value
    except Exception as exc:
        rec["outcome"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    rec["seconds"] = round(time.monotonic() - t0, 2)
    rec["calls"] = strip_content(log.calls[start:])
    raw = [c.get("content") for c in log.calls[start:]]
    results.append(rec)
    n = len(results)
    (out / f"{n:02d}-{entry}.json").write_text(
        json.dumps({**rec, "raw": raw}, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    (out / "summary.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "result"} for r in results], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in rec.items() if k != "result"}, ensure_ascii=False), flush=True)


ctx = frozen_context()
for entry in entries:
    if entry == "trip":
        run(entry, lambda: day_prompts.llm_open_trip(day_request(ctx))[0])
    elif entry == "trip3":
        run(entry, lambda: day_prompts.llm_open_trip(day_request(ctx, days=3))[0])
    elif entry == "trip_stream":
        run(entry, lambda: [m for m in trip_stream.llm_open_trip_stream(day_request(ctx)) if m[0] == "final"][0][1])
    elif entry == "day":
        run(entry, lambda: day_stream.llm_open_day(day_request(ctx, day_no=1), set()))
    elif entry == "repair":
        # Forced repair: hand the repair path a truncated real-shaped answer with the original request.
        req = day_request(ctx)
        system, user = day_prompts.open_trip_prompt(req)
        fmt = json_response_format("fast", "trip_output", day_prompts.open_trip_output_schema())
        truncated = '{"trip_theme":"东京素食慢游","daily_plans":[{"day_no":1,"theme":"新宿","items":[{"item_type":"attraction","poi_name":"新宿御苑"'
        run(
            entry,
            lambda: parse_llm_json_with_repair(
                truncated,
                get_llm_client(),
                model=model_for("fast"),
                repair_max_tokens=max(2800, min(16000, 2 * 8000)),
                response_format=fmt,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                validate=lambda d: validate_trip_output(d, 2),
            ),
        )
    elif entry == "clarify":
        run(entry, lambda: run_clarify(ClarifyRequest(message=CLARIFY_MSG)).model_dump(mode="json", by_alias=True))
