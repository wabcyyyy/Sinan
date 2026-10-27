"""P4 本地正式页面/API/鉴权/数据库/求解活栈；冻结研究和取证，禁止模型与公网。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_app(run_dir):
    from scripts.serve_screenshot_browser import build_app as build_base

    app = build_base(run_dir)
    patcher = app.state.p3_patcher
    from app.agent.generation.content.evidence_observations import FactObservation
    from app.agent.generation.orchestration import evidence_tools
    from app.schemas.trip import ClarifyResponse
    from app.schemas.trip_requirements import IntakeState, requirements_issues
    from app.services import itinerary_city, itinerary_generation
    from tests.active_evidence_cases import environment, observation

    request, prepared, _, truth = environment({"hidden_truth": {"forward_route": 15, "closed": None, "price_b": 20}})
    assert request.requirements_struct is not None
    original = request.requirements_struct.model_copy(deep=True)
    original.constraint_planning = None  # 必须由真实页面显式启用，不在场景中偷开开关。
    state = {"method": "frozen source observations; real UI/auth/API/SQLite/worker/CP-SAT", "queries": []}

    def save():
        (run_dir / "p4-sources.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def clarify(body):
        current = (
            body.state.model_copy(deep=True)
            if body.state
            else IntakeState(
                city="Paris",
                days=1,
                persons=1,
                budget=100,
                start_date="2026-10-25",
                stay_nights=0,
                requirements=original.model_copy(deep=True),
            )
        )
        for key, value in body.slots.items():
            if (
                key in {"city", "days", "persons", "budget", "start_date", "stay_nights", "preferences"}
                and value is not None
            ):
                setattr(current, key, value)
        issues = requirements_issues(current.requirements, days=current.days)
        return ClarifyResponse(
            state=current,
            ready=not issues,
            blocked=bool(issues),
            next="confirm",
            reply="；".join(issues) if issues else "本地冻结场景，请核对资料核实设置和两项预约。",
            slots={
                k: getattr(current, k)
                for k in ("city", "days", "persons", "budget", "start_date", "stay_nights", "preferences")
            },
        )

    def query(gap, problems, candidates, req, options):
        assert not options.allow_source_search, "浏览器验收不允许模型来源搜索"
        phase = "replan" if candidates[0].fact.candidate_id.startswith("item:") else "create"
        # 问题场景只替换外部工具回答，不改变循环或业务写入。
        if "追问" in req.preferences:
            observed = FactObservation(
                gap.kind,
                tuple(p.fact.source_entity_id for p in candidates[:2]),
                question="请核对 A 到 B 的转场来源，当前路段仍待确认。",
                tool="ask_user",
            )
        else:
            observed = observation(gap, candidates, truth)
            if phase == "create" and gap.kind == "price" and gap.names == ("A",):
                observed = FactObservation(
                    gap.kind, observed.entity_ids, question="A 的费用仍待确认，请核对票价来源。", tool="ask_user"
                )
            if phase == "replan" and gap.kind == "price" and gap.names == ("A",):
                observed.value = 15
        state["queries"].append(
            {
                "phase": phase,
                "kind": gap.kind,
                "gap": gap.gap_id,
                "entity_ids": list(observed.entity_ids),
                "question": observed.question,
                "model_calls": 0,
            }
        )
        save()
        return observed

    patcher.setattr(itinerary_city, "run_clarify", clarify)
    patcher.setattr(
        itinerary_generation, "run_plan_context", lambda *_a, **_k: {"candidates": [p.raw for p in prepared]}
    )
    patcher.setattr(evidence_tools, "query_evidence", query)
    save()
    return app


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18074)
    args = parser.parse_args()
    uvicorn.run(
        build_app(args.run_dir.resolve()), host="127.0.0.1", port=args.port, lifespan="off", log_level="warning"
    )
