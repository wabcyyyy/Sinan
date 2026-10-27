"""续用本轮来源与意图：真实研究正向评估 + 营业采纳/拒绝 + 业务求解；不覆盖旧失败。"""

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_p4_acceptance import save
from scripts.run_p4_live_research import LiveRun, candidate


def execute(root, output):
    from app.agent.data import web_search
    from app.agent.generation.content import opening_evidence
    from app.agent.generation.orchestration.constraint_planning import run_constraint_planning
    from app.agent.research import factory, reasoning
    from app.agent.research.evidence import ResearchTask
    from app.agent.runtime.run_limits import RunLimits, use_task_limits
    from app.agent.tools import impl as tools
    from app.common import llm_client
    from app.common.config import settings
    from app.common.model_registry import binding
    from app.schemas.active_evidence import ActiveEvidenceOptions
    from app.schemas.trip import GenerateRequest
    from app.schemas.trip_requirements import FixedArrangement, IntakeState

    first = root / "sinan-upgrade-p4-live-30-20261024"
    probe = root / "sinan-upgrade-p4-live-30-sources-20261024"
    original = root / "sinan-upgrade-p4-real-20261024"
    old = json.loads((first / "result.json").read_text("utf-8"))
    rows = json.loads((probe / "sources.json").read_text("utf-8"))[0]["rows"]
    stamp = json.loads((probe / "response-metadata.json").read_text("utf-8"))["search-1"]["observed_at"]
    rows = [{**row, "retrieved_at": stamp} for row in rows]
    poi = candidate(rows)
    if poi.get("cost") != 0:
        raise ValueError("新增公开来源仍无可归属的基础入馆免费依据，不能强行补值")
    state = IntakeState.model_validate(old["clarify"]["state"])
    output.mkdir(parents=True, exist_ok=False)
    run = LiveRun(output, prior_runs=[first, probe])
    result = {
        "reuse": [str(first), str(probe), str(original)],
        "candidate": poi,
        "scope": "same-round public source snapshots; existing real intent; one new real research evaluation",
    }
    with (
        ExitStack() as stack,
        httpx.Client(follow_redirects=False, event_hooks={"request": [run.before], "response": [run.after]}) as http,
    ):
        stack.enter_context(patch.object(settings, "llm_enable_thinking", False))
        stack.enter_context(patch.object(settings, "trial_billing_enabled", False))
        stack.enter_context(patch.object(llm_client, "_record_usage", lambda *_a, **_kw: None))
        bound = binding("main")
        client = llm_client.LLMClient(
            base_url=bound.base_url,
            api_key=bound.provider.api_key,
            model=bound.model,
            capabilities=bound.capabilities(),
            max_attempts=1,
            timeout=12,
            follow_redirects=False,
        )
        stack.enter_context(patch.object(client, "_http", return_value=http))
        stack.enter_context(patch.object(reasoning, "get_llm_client", return_value=client))
        stack.enter_context(patch.object(web_search, "get_llm_client", return_value=client))
        stack.enter_context(patch.object(tools, "search_attractions", return_value=[poi]))
        verdicts = []
        original_eval = reasoning.evaluate_research

        def evaluate(*args):
            value = original_eval(*args)
            verdicts.append(value)
            return value

        stack.enter_context(patch.object(reasoning, "evaluate_research", evaluate))
        with use_task_limits(RunLimits(deadline_seconds=20, max_llm_calls=2, max_retrievals=3, max_research_calls=3)):
            pack = factory.run_research(
                ResearchTask(domain="attraction", city="杭州", budget=100, budget_currency="CNY", persons=1, limit=1)
            )
        result.update(research_pack=pack.to_dict(), research_verdicts=verdicts)
        save(output / "result.json", result)
        # 确认表单的固定时间与真实 M1 原话“馆内计划4小时/09–13”一致，单列为场景输入，不冒充 NL 抽取。
        req = state.requirements.model_copy(deep=True)
        req.fixed_arrangements = [
            FixedArrangement(day_no=1, name=poi["name"], item_type="attraction", start_time="09:00", end_time="13:00")
        ]
        assert req.constraint_planning is not None
        req.constraint_planning.active_evidence = ActiveEvidenceOptions(
            max_queries=1, budget_units=2, time_limit_seconds=4, allow_source_search=True
        )
        result["confirmed_form_inputs"] = [f.model_dump(mode="json") for f in req.fixed_arrangements]
        # 原真实 S1 正文和原获取时刻缓存复用；不把回放时刻当新的在线获取时刻。
        opening_rows = json.loads((original / "S1-response.json").read_text("utf-8"))["results"]
        observed = json.loads((original / "response-metadata.json").read_text("utf-8"))["S1"]["observed_at"]
        opening_rows = [{**row, "retrieved_at": observed} for row in opening_rows]
        stack.enter_context(patch.object(opening_evidence, "web_search_sources", return_value=opening_rows))
        result["planning_cases"] = []
        for day in ("2026-11-03", "2026-11-02"):
            request = GenerateRequest(
                city="杭州", days=1, persons=1, budget=100, start_date=day, requirements_struct=req
            )
            plans = run_constraint_planning(request, {"candidates": [poi]})
            result["planning_cases"].append({"date": day, "plans": [p.model_dump(mode="json") for p in plans]})
        result["clarify_state"] = state.model_dump(mode="json")
        result["context"] = {"candidates": [poi]}
        result["form_requirements"] = req.model_dump(mode="json")
        result["opening_source_rows"] = opening_rows
    result["new_model_calls"] = sum(r["kind"] == "model" for r in run.guard.records)
    result["reserved_upper_cny"] = str(run.guard.reserved)
    save(output / "result.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--approved", action="store_true")
    a = p.parse_args()
    if not a.approved:
        raise SystemExit("本轮用户预算授权后方可执行")
    result = execute(a.root, a.output)
    print(
        json.dumps({"models": result["new_model_calls"], "verdicts": result["research_verdicts"]}, ensure_ascii=False)
    )
