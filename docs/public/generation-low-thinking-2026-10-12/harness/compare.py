"""A/B: production open_trip prompt (v1.5) vs candidate, same frozen input, global low thinking, same budget.

Only `open_generation.open_trip_system_prompt` is swapped; dynamic blocks (references, pricing, requirements) are
identical. Usage: python compare.py <out_dir> <pairs>
"""

import hashlib
import json
import sys
import time

from candidate_prompt import open_trip_system_prompt as candidate_base
from common import ROOT, CallLog, day_request, frozen_context, strip_content, thinking_snapshot

from app.agent.generation.content import day_prompts

out = ROOT / ".tmp-release-audit/lowthink" / sys.argv[1]
out.mkdir(parents=True, exist_ok=True)
pairs = int(sys.argv[2])
log = CallLog()
log.install()
production_base = day_prompts.open_trip_system_prompt
ctx = frozen_context()
req = day_request(ctx)
prompts = {}
for arm, base in (("A_v1.5", production_base), ("B_candidate", candidate_base)):
    day_prompts.open_trip_system_prompt = base
    prompts[arm] = day_prompts.open_trip_prompt(req)
day_prompts.open_trip_system_prompt = production_base
assert prompts["A_v1.5"][1] == prompts["B_candidate"][1], "user message must be identical"
(out / "setup.json").write_text(
    json.dumps(
        {
            "thinking": thinking_snapshot(),
            "context": ctx,
            "prompts": {k: {"system": s, "user": u, "sha256": hashlib.sha256(s.encode()).hexdigest()} for k, (s, u) in prompts.items()},
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ),
    encoding="utf-8",
)
summary = []
n = 0
arms = (("A_v1.5", production_base), ("B_candidate", candidate_base))
if len(sys.argv) > 3:
    arms = tuple(a for a in arms if a[0] in sys.argv[3:])
for _ in range(pairs):
    for arm, base in arms:
        day_prompts.open_trip_system_prompt = base
        log.entry = arm
        start = len(log.calls)
        t0 = time.monotonic()
        rec = {"sample": n, "variant": arm}
        try:
            day_prompts.llm_open_trip(day_request(ctx))
            rec["outcome"] = "ok"
        except Exception as exc:
            rec["outcome"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        finally:
            day_prompts.open_trip_system_prompt = production_base
        rec["seconds"] = round(time.monotonic() - t0, 2)
        rec["calls"] = strip_content(log.calls[start:])
        rec["raw"] = [c.get("content") for c in log.calls[start:]]
        (out / f"sample-{n:02d}-{arm}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        summary.append({k: v for k, v in rec.items() if k != "raw"})
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary[-1], ensure_ascii=False), flush=True)
        n += 1
