"""Replay saved raw trip answers through the PRODUCTION open_plans._generate_drafts + validate_plans (no model calls).

Network-free: local_ground (name->coordinate resolver) is stubbed, so only reference-pool rows get coordinates;
this isolates prompt output vs. deterministic postprocess. Usage: python postprocess.py <out.json> <sample.json>...
Each sample file must contain "raw": [<first call content>, ...] (budgets.py / compare.py format).
"""

import json
import sys

from common import ROOT, frozen_context, requirements, trip_request

from app.agent.core.json_utils import parse_llm_json
from app.agent.generation.content import landing
from app.agent.generation.content.day_prompts import assemble_trip_output
from app.agent.generation.content.reference_pool import ReferencePool
from app.agent.generation.content.reflect import hard_day_issues, validate_plans
from app.agent.generation.orchestration import open_plans

events: list[dict] = []
open_plans.record_event = lambda kind, name, **kw: events.append({"name": name, **(kw.get("metadata") or {})})
landing.local_ground = lambda item, city: None
open_plans.llm_open_day = lambda req, used: (_ for _ in ()).throw(RuntimeError("day fallback not replayed"))


def replay(raw: str) -> dict:
    data = parse_llm_json(raw)
    plans_in, _ = assemble_trip_output(data, 2)
    raw_days = {
        p["day_no"]: [(i.get("item_type"), i.get("poi_name")) for i in p.get("items") or [] if isinstance(i, dict)]
        for p in plans_in
    }
    open_plans.llm_open_trip = lambda req: (json.loads(json.dumps(plans_in)), [])
    events.clear()
    ctx = frozen_context()
    req = trip_request()
    plans, _, errors = open_plans._generate_drafts(req, "", ctx, ReferencePool(ctx), {})
    reqs = requirements()
    issues, _ = validate_plans(plans, budget=3000, persons=2, requirements=reqs)
    days = []
    for plan in plans:
        items = plan["items"]
        raw_items = raw_days.get(plan["day_no"], [])
        final_names = {i["poi_name"] for i in items}
        days.append(
            {
                "dayNo": plan["day_no"],
                "rawMeals": [n for t, n in raw_items if t == "food"],
                "finalMeals": [
                    {"name": i["poi_name"], "time": f"{i.get('start_time')}-{i.get('end_time')}", "diet": sorted(k for k in i.get("fact_evidence", {}) if k.startswith("diet:"))}
                    for i in items
                    if i["item_type"] == "food"
                ],
                "finalAttractions": [i["poi_name"] for i in items if i["item_type"] == "attraction"],
                "retyped": [
                    {"name": i["poi_name"], "to": i["item_type"]}
                    for i in items
                    if (i["item_type"], i["poi_name"]) not in raw_items and i["item_type"] != "hotel"
                    and any(n == i["poi_name"] for _, n in raw_items)
                ],
                "removed": [n for _, n in raw_items if n not in final_names],
                "blockers": hard_day_issues(plan, reqs),
            }
        )
    return {
        "errors": errors,
        "issues": issues,
        "rulePass": not issues and all(not d["blockers"] for d in days),
        "drops": [e for e in events if e["name"] == "duplicate_dropped"],
        "days": days,
    }


rows = []
for path in sys.argv[2:]:
    rec = json.loads(open(path, encoding="utf-8").read())
    raw = next((r for r in rec.get("raw") or [] if r), "")
    try:
        result = replay(raw)
    except Exception as exc:
        result = {"replayError": f"{type(exc).__name__}: {exc}"[:300]}
    rows.append({"file": path, "variant": rec.get("variant") or rec.get("entry"), **result})
    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
(ROOT / ".tmp-release-audit/lowthink" / sys.argv[1]).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
