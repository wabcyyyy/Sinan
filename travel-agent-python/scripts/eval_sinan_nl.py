"""SINAN-NL-01 sequential, resumable three-arm experiment; frozen inputs, no result overwrite."""

import argparse
import json
import time
from pathlib import Path
from unittest.mock import patch

from app.agent.editing import clarify
from app.agent.generation.orchestration import constraint_planning as planner
from app.schemas.planning import ConstraintPlanningOptions
from app.schemas.trip import ClarifyRequest, GenerateRequest
from app.schemas.trip_requirements import IntakeState, TripRequirements
from scripts.run_p4_acceptance import save
from scripts.sinan_night_budget import NightClient
from scripts.sinan_nl_budget import NLBudget
from scripts.sinan_nl_cases import OPTIONS, hhmm, prepare, public_case
from scripts.sinan_nl_check import evaluate, runtime_feedback, schedule_checks
from scripts.sinan_nl_freeze import require_frozen

SYSTEM = """你是同城景点规划器，只依据原始NL与给定的固定事实输出JSON，不读取任何隐藏标准。
这是条件规划实验：facts内营业窗/门票/时长和有向转场为共同假设，不保证现实有效；不能发外部请求。
先报告你理解的state，再直接安排。输出：
{"state":{"city":"目的城市","days":整数,"persons":整数,"budget":数字,"start_date":"YYYY-MM-DD",
"requirements":{"day_windows":[{"day_no":整数,"not_before":"HH:MM","finish_by":"HH:MM"}],
"required_places":[{"constraint_id":"唯一ID","name":"完整候选名","day_no":整数或null}],
"excluded_places":["完整候选名"],"max_attractions_per_day":整数,
"budget_policy":{"mode":"hard_cap或target","include_intercity_transport":布尔值},
"place_relations":[{"constraint_id":"唯一ID","kind":"before或one_of","place_names":["完整名A","完整名B"],
"day_no":整数,"strength":"hard或soft"}],"unresolved_requests":["无法表达的原话"]}},
"status":"FEASIBLE或INFEASIBLE或UNKNOWN","plans":[{"day_no":整数,"items":[{"id":"候选id","start":"HH:MM","end":"HH:MM"}]}],
"reason":"简短原因"}。
before硬关系表示两处都必去且当日A游玩结束后B开始；one_of为至少一个，允许都去。
软偏好不是硬要求，不能将软地点擅改为必去。只抽取明确要求，不编造约束。所有模型解释均非人审。
给定游玩时长不可缩短；每段转场必须留足有向表分钟，活动全程不重复。每天至少一处景点，最多6处且有效活动总时长<=480分钟；
未给双方界或给出>=8小时的普通全天，活动至少240分钟；明确短于8小时的起止窗口不套240分钟下限。
费用为给定每人门票乘人数再求和。关键事实未知只能条件试排并标UNKNOWN，不得把缺证据说成确定无解。
INFEASIBLE须是共同事实与自己理解的硬要求不能同时成立。只输出JSON。"""


class ArmClient(NightClient):
    def __init__(self, ledger, output, binding, prefix):
        super().__init__(ledger, output, binding)
        if not binding.provider.api_key.strip():
            raise ValueError("live credentials unavailable; refuse before any reservation")
        self.prefix, self.calls = prefix, 0

    def complete(self, *args, **kwargs):
        if self.calls >= 3:
            raise ValueError("per-arm model-call bound exhausted")
        self.identifier = f"{self.prefix}-{self.calls}"
        self.calls += 1
        return super().complete(*args, **kwargs)

    def chat(self, messages, *, model=None, temperature=0, max_tokens=4096, json_mode=False, response_format=None):
        # Text content repair uses the same per-arm limit and cumulative ledger.
        if self.calls >= 3 or model not in (None, self.model):
            raise ValueError("per-arm model/model-call bound exhausted")
        if any(not isinstance(message.get("content"), str) for message in messages):
            raise ValueError("NL repair only allows anonymous text")
        self.identifier = f"{self.prefix}-{self.calls}"
        self.calls += 1
        return self._send(
            {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format or {"type": "json_object"},
                "thinking": {"type": "disabled"},
            }
        )


def context_for(public):
    rows = []
    synthetic = public["id"].startswith("diag-")
    for item in public["facts"]["candidates"]:
        evidence = {
            "provider": "anonymous-synthetic-diagnostic" if synthetic else "ChinaTravel-benchmark-snapshot",
            "source_url": (
                "https://example.com/sinan-nl-01/anonymous-diagnostics"
                if synthetic
                else "https://huggingface.co/datasets/LAMDA-NeSy/ChinaTravel-Sandbox"
            ),
            "retrieved_at": "2026-10-25T15:00:00Z",
            "verification_status": "partially_verified",
            "value_kind": "observed",
        }
        hours = (
            None
            if item["opening"] is None
            else {
                "status": "known",
                "rules": [
                    {
                        "intervals": [{"start": hhmm(a), "end": hhmm(b)} for a, b in item["opening"]],
                        "source_text": "frozen dataset interval, not live opening verification",
                    }
                ],
            }
        )
        rows.append(
            {
                "name": item["name"],
                "id": item["id"],
                "country_code": "CN",
                "source": "anonymous-synthetic" if synthetic else "ChinaTravel-adapted",
                "source_url": evidence["source_url"],
                "source_fetched_at": evidence["retrieved_at"],
                "latitude": item.get("latitude", 30.0),
                "longitude": item.get("longitude", 120.0),
                "cost": item["price"],
                "cost_currency": "CNY",
                "duration_min": item["duration_min"],
                "categories": [],
                "fact_evidence": {
                    "cost": evidence,
                    "duration_min": evidence,
                    "open_time": {**evidence, "opening_hours": hours},
                },
            }
        )
    return {"candidates": rows, "foods": [], "hotels": []}


def solve_state(public, state):
    req = TripRequirements.model_validate(state.get("requirements") or {})
    req.constraint_planning = ConstraintPlanningOptions.model_validate(OPTIONS)
    request = GenerateRequest(
        city=state["city"],
        days=state["days"],
        persons=state["persons"],
        budget=state.get("budget"),
        start_date=state.get("start_date"),
        requirements_struct=req,
    )
    matrix = {
        (a, b): {"duration_min": duration, "source": "benchmark-synthetic", "mode": "walking"}
        for a, b, duration in public["facts"]["routes"]
    }
    original_solve = planner.solve_trip

    def bounded(*args, **kwargs):
        return original_solve(*args, **{**kwargs, "time_limit_s": min(2.0, kwargs.get("time_limit_s", 2.0))})

    with patch.object(planner, "get_route_matrix", return_value=matrix), patch.object(planner, "solve_trip", bounded):
        plans = planner.run_constraint_planning(request, context_for(public), stay_nights=0)
    assessments = [plan.constraint_planning for plan in plans]
    assert all(a is not None for a in assessments)
    reports = [a.model_dump(mode="json") for a in assessments if a is not None]
    # Only the experiment's explicitly fixed synthetic road assumption is normalized here.
    # Product UNKNOWN remains in the trace; no claim of verified real road-network delivery.
    unknown = [
        c
        for a in reports
        for c in a["report"]["checks"]
        if c["status"] == "unknown"
        and not (
            c["constraint_id"] == "trip:missing-facts"
            and c["reason"] == "部分转场缺少真实路网数据，仅按估算/未知下界试排"
        )
    ]
    status = (
        "INFEASIBLE"
        if any(a["status"] == "INFEASIBLE" for a in reports)
        else (
            "FEASIBLE"
            if not unknown and all(a["numerical_status"] in {"OPTIMAL", "FEASIBLE"} for a in reports)
            else "UNKNOWN"
        )
    )
    output = {
        "state": state,
        "status": status,
        "plans": [
            {"day_no": p.day_no, "items": [{"id": i.poi_id, "start": i.start_time, "end": i.end_time} for i in p.items]}
            for p in plans
        ],
        "product_assessments": reports,
        "conditional_benchmark_normalization": True,
    }
    return output


def run_arm(public, arm, client):
    if arm == "C":
        initial = IntakeState(requirements=TripRequirements(constraint_planning=OPTIONS))
        with patch.object(clarify, "get_llm_client", return_value=client):
            response = clarify.run_clarify(ClarifyRequest(message=public["nl"], state=initial))
        state = response.state.model_dump(mode="json")
        output = solve_state(public, state)
        return {"first": output, "final": output, "clarify": response.model_dump(mode="json"), "calls": client.calls}
    prompt = json.dumps(public, ensure_ascii=False)
    attempts = []
    for _attempt in range(3 if arm == "B" else 1):
        raw = client.complete(prompt, system_prompt=SYSTEM, max_tokens=4096, temperature=0)
        try:
            output = json.loads(raw)
            state = IntakeState.model_validate(output["state"])
            output["state"] = state.model_dump(mode="json")
            feedback = runtime_feedback(public, output)
        except (ValueError, KeyError, TypeError) as exc:
            output = {"status": "ERROR", "state": {}, "plans": [], "raw": raw}
            feedback = [f"输出格式不合法: {type(exc).__name__}: {exc}"]
        attempts.append({"output": output, "feedback": feedback})
        if not feedback or arm != "B":
            break
        prompt = json.dumps(
            {
                "original": public,
                "previous": output,
                "feedback": feedback,
                "instruction": "仅依原话和共同事实修复；不得为让检查通过丢掉要求或改硬软优先级。",
            },
            ensure_ascii=False,
        )
    return {
        "first": attempts[0]["output"],
        "final": attempts[-1]["output"],
        "attempts": attempts,
        "calls": client.calls,
    }


def reference(root):
    target = root / "reference.json"
    if target.exists():
        raise FileExistsError(target)
    design = json.loads((root / "design.json").read_text("utf8"))
    rows = []
    for row in design["rows"]:
        result = solve_state(public_case(row), row["gold"])
        check = schedule_checks(row, row["gold"], result["plans"])
        if result["status"] == "FEASIBLE" and check["errors"]:
            raise ValueError({"id": row["id"], "reference_errors": check["errors"]})
        rows.append({"id": row["id"], "expected": result["status"], "output": result, "independent_check": check})
    save(target, {"rows": rows, "note": "CP reference with independently checked witnesses; no human labels"})
    return {
        "count": len(rows),
        "statuses": {s: sum(r["expected"] == s for r in rows) for s in ("FEASIBLE", "INFEASIBLE", "UNKNOWN")},
    }


def execute(root, split, start, stop, *, approved=False, phase="initial", arms="ABC"):
    from app.common.model_registry import binding

    design = json.loads((root / "design.json").read_text("utf8"))
    reference_rows = json.loads((root / "reference.json").read_text("utf8"))["rows"]
    if split == "diagnostic":
        design = json.loads((root / "diagnostic-design.json").read_text("utf8"))
        reference_rows = design["rows"]
    if phase not in {"initial", "calibrated", "calibrated2"} or arms not in {"ABC", "C"}:
        raise ValueError("unsupported experiment phase/arms")
    if split != "dev" and (phase != "initial" or arms != "ABC"):
        raise ValueError("confirmation/diagnostics are single frozen three-arm evaluations")
    if split != "dev":
        require_frozen(root, split=split)
    expected = {row["id"]: row["expected"] for row in reference_rows}
    cases = [row for row in design["rows"] if row["split"] == split]
    if not 0 <= start < stop <= len(cases) or stop - start > 2:
        raise ValueError("batch must contain at most two frozen cases")
    ledger = NLBudget(root / "budget.json", approved=approved)
    result_dir = root / ("results" if phase == "initial" else f"dev-{phase}")
    result_dir.mkdir(exist_ok=True)
    deadline = time.monotonic() + 78
    completed = []
    for row in cases[start:stop]:
        for arm in arms:
            path = result_dir / f"{row['id']}-{arm}.json"
            if path.exists():
                continue
            if time.monotonic() >= deadline:
                return {"stopped_at_budget": True, "completed": completed, "cost": str(ledger.total())}
            prefix = f"{row['id']}-{arm}" if phase == "initial" else f"{row['id']}-{phase}-{arm}"
            client = ArmClient(ledger, root / "models", binding("main"), prefix)
            started = time.perf_counter()
            try:
                result = run_arm(public_case(row), arm, client)
            except Exception as exc:
                failure = {"status": "ERROR", "state": {}, "plans": [], "error": f"{type(exc).__name__}: {exc}"}
                result = {"first": failure, "final": failure, "calls": client.calls}
            save(
                path,
                {
                    "id": row["id"],
                    "split": split,
                    "arm": arm,
                    "result": result,
                    "first_evaluation": evaluate(row, result["first"], expected[row["id"]]),
                    "evaluation": evaluate(row, result["final"], expected[row["id"]]),
                    "elapsed_s": time.perf_counter() - started,
                },
            )
            completed.append(path.name)
    return {"completed": completed, "cost": str(ledger.total())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--reference", action="store_true")
    parser.add_argument("--split", default="dev", choices=("dev", "confirm", "diagnostic"))
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--stop", type=int, default=2)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            prepare(args.root)
            if args.prepare
            else reference(args.root)
            if args.reference
            else execute(args.root, args.split, args.start, args.stop, approved=args.approved),
            ensure_ascii=True,
        )
    )
