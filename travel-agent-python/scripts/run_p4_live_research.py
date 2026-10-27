"""匿名杭州案例的有界真实单域研究与营业取证；单次授权，15元执行闸/30元总上限。"""

import argparse
import json
import os
import re
import sys
import threading
import time
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p4_acceptance_budget import AcceptanceBudget, model_upper
from scripts.run_p4_acceptance import PaidRun, save

MUSEUM = "中国丝绸博物馆"
OFFICIAL = "https://www.chinasilkmuseum.com/cgjyy/info_6.aspx?itemid=31249"
LIMITS = {"model": 6, "search": 4, "fetch": 2}


class LiveRun(PaidRun):
    def __init__(self, output, prior_runs=()):
        self.plan = {"scope_id": "SINAN-P4-LIVE-30-20261024", "requests": []}
        self.output = output
        self.deadline = time.monotonic() + 60
        prior = [json.loads((Path(p) / "paid-ledger.json").read_text("utf-8")) for p in prior_runs]
        prior_reserved = sum((Decimal(p["reserved_cny"]) for p in prior), Decimal(0))
        remaining = {k: cap - sum(r["kind"] == k for p in prior for r in p["records"]) for k, cap in LIMITS.items()}
        self.guard = AcceptanceBudget(
            self.plan,
            output / "paid-ledger.json",
            approved=True,
            hard_budget=Decimal(15) - prior_reserved,
            kind_limits={k: v for k, v in remaining.items() if v > 0},
        )
        self.responses = {}

    def before(self, request):
        payload = json.loads(request.content)
        path = request.url.path
        if path == "/chat/completions":
            kind = "model"
            if (
                payload.get("model") != "deepseek-flash"
                or payload.get("thinking") != {"type": "disabled"}
                or not 1 <= payload.get("max_tokens", 0) <= 4096
                or "tools" in payload
                or [m.get("role") for m in payload.get("messages", [])] != ["system", "user"]
                or len(request.content) > 32768
            ):
                raise ValueError("模型或输入/输出超出本轮范围")
        elif path == "/v1/tools/search":
            kind = "search"
            if MUSEUM not in payload.get("query", "") or set(payload) != {"query", "count", "need_content"}:
                raise ValueError("只允许本轮场馆公开来源检索")
        elif path == "/v1/tools/fetch":
            kind = "fetch"
            if payload != {"url": OFFICIAL}:
                raise ValueError("只允许已核对的公开官方开放公告")
        else:
            raise ValueError("未批准的 endpoint")
        if any(r["payload"] == payload and r["path"] == path for r in self.plan["requests"]):
            raise ValueError("不允许自动重试或重复 payload")
        self.plan["requests"].append(
            {"id": f"{kind}-{len(self.plan['requests']) + 1}", "kind": kind, "path": path, "payload": payload}
        )
        save(self.output / "manifest.json", self.plan)
        super().before(request)

    def after(self, response):
        super().after(response)
        identifier = response.request.extensions["acceptance_id"]
        row = self.responses[identifier]
        data = response.json()
        kind = identifier.split("-", 1)[0]
        row["kind"] = kind
        if kind == "model" and row["usage"]:
            usage = row["usage"]
            row["peak_price_upper_cny"] = str(
                (Decimal(usage["prompt_tokens"]) * 2 + Decimal(usage["completion_tokens"]) * 8) / 1_000_000
            )
        elif kind in {"search", "fetch"}:
            row["billable"] = response.status_code == 200 and bool(
                data.get("results" if kind == "search" else "content")
            )
            row["peak_price_upper_cny"] = "0.005" if row["billable"] else "0"
        save(self.output / "response-metadata.json", self.responses)


def candidate(rows):
    """只用本轮实际正文确定免票；无明确依据则费用保持未知。"""
    from app.schemas.trip import FactEvidence

    raw: dict = {
        "name": MUSEUM,
        "id": "official-name:chinasilkmuseum.com:hangzhou",
        "source": "live-public-search",
        "country_code": "CN",
        "duration_min": 240,
        "remark": "本轮真实公开检索资料；240分钟是匿名验收请求的计划停留时长，不是官方游览建议",
    }
    for row in rows:
        body = row.get("content", "")
        if (
            urlsplit(row["url"]).hostname
            not in {"www.chinasilkmuseum.com", "chinasilkmuseum.com", "wgly.hangzhou.gov.cn"}
            or MUSEUM not in row.get("title", "") + body
        ):
            continue
        for sentence in re.split(r"[。；\n]", body):
            if (
                re.search(r"免费开放|免费参观|免费入馆", sentence)
                and MUSEUM in sentence
                and not re.search(r"曾|原|历史|部分|限时|活动期间|特展|儿童", sentence)
                and (not re.search(r"200\s*\d\s*年", sentence) or "起" in sentence)
            ):
                fact = FactEvidence(
                    source_url=row["url"],
                    retrieved_at=row["retrieved_at"],
                    provider="live-public-search",
                    verification_status="partially_verified",
                    value_kind="observed",
                    excerpt=sentence[:1000],
                )
                raw.update(
                    cost=0, cost_currency="CNY", ticket_price=0, fact_evidence={"cost": fact.model_dump(mode="json")}
                )
                raw["remark"] += "；公开正文原句：" + sentence[:600]
                return raw
    return raw


def execute(output):
    from datetime import date

    from app.agent.data import deepseek_search, web_search
    from app.agent.editing import clarify
    from app.agent.generation.content.constraint_facts import prepare_candidates
    from app.agent.generation.content.constraint_trip_solver import solve_trip
    from app.agent.generation.orchestration.active_evidence import drive_evidence
    from app.agent.generation.orchestration.constraint_planning import _problem_snapshots
    from app.agent.generation.orchestration.evidence_tools import query_evidence
    from app.agent.research import factory, reasoning
    from app.agent.research.evidence import ResearchTask
    from app.agent.runtime.run_limits import RunLimits, use_task_limits
    from app.agent.tools import impl as tools
    from app.common import llm_client
    from app.common.config import settings
    from app.common.model_registry import binding
    from app.schemas.active_evidence import ActiveEvidenceOptions
    from app.schemas.planning import ConstraintPlanningOptions
    from app.schemas.trip import ClarifyRequest, GenerateRequest
    from app.schemas.trip_requirements import IntakeState, TripRequirements

    bound = binding("main")
    if bound.base_url.rstrip("/") != "https://api.deepseek.com" or bound.model != "deepseek-flash":
        raise ValueError("未核价的模型或 endpoint")
    output.mkdir(parents=True, exist_ok=False)
    save(
        output / "authorization.json",
        {
            "user_cap_cny": "30",
            "execution_cap_cny": "15",
            "limits": LIMITS,
            "max_output_tokens": 4096,
            "reserved_all_upper_cny": str(model_upper(4096) * 6 + Decimal("0.03")),
            "privacy": "anonymous Hangzhou intent and public museum sources only; no private trip/account/history",
            "no_retries": True,
            "pricing_rechecked": "2026-10-24",
        },
    )
    run = LiveRun(output)

    def stop():
        save(output / "deadline-stop.json", {"status": "incomplete", "seconds": 60})
        os._exit(124)

    timer = threading.Timer(60, stop)
    timer.daemon = True
    timer.start()
    result: dict = {
        "method": (
            "fresh anonymous clarify; attraction research graph over same-run public snapshot; real opening search"
        )
    }
    started = time.monotonic()
    try:
        with (
            ExitStack() as stack,
            httpx.Client(
                follow_redirects=False, event_hooks={"request": [run.before], "response": [run.after]}
            ) as http,
        ):
            stack.enter_context(patch.object(settings, "llm_enable_thinking", False))
            stack.enter_context(patch.object(settings, "trial_billing_enabled", False))
            stack.enter_context(patch.object(llm_client, "_record_usage", lambda *_a, **_kw: None))
            client = llm_client.LLMClient(
                base_url=bound.base_url,
                api_key=bound.provider.api_key,
                model=bound.model,
                capabilities=bound.capabilities(),
                timeout=12,
                max_attempts=1,
                follow_redirects=False,
            )
            stack.enter_context(patch.object(client, "_http", return_value=http))
            stack.enter_context(patch.object(clarify, "get_llm_client", return_value=client))
            stack.enter_context(patch.object(reasoning, "get_llm_client", return_value=client))
            stack.enter_context(patch.object(web_search, "get_llm_client", return_value=client))
            stack.enter_context(patch.object(deepseek_search, "api_client", return_value=http))
            state = IntakeState(
                city="杭州",
                days=1,
                persons=1,
                budget=100,
                start_date="2026-11-03",
                stay_nights=0,
                requirements=TripRequirements(
                    constraint_planning=ConstraintPlanningOptions(
                        country_code="CN", time_zone="Asia/Shanghai", currency="CNY"
                    )
                ),
            )
            response = clarify.run_clarify(
                ClarifyRequest(
                    state=state,
                    message=(
                        "11月3日杭州玩1天，1个人不住宿，活动预算100元是硬上限，每天最多一个景点；"
                        "必须去中国丝绸博物馆，在馆内计划停留4小时，第一天09:00后开始、13:00前结束。"
                    ),
                )
            )
            result["clarify"] = response.model_dump(mode="json")
            save(output / "result.json", result)
            try:
                fetched = http.post(
                    "https://api.deepseek.com/v1/tools/fetch",
                    headers={"Authorization": f"Bearer {bound.provider.api_key}"},
                    json={"url": OFFICIAL},
                )
                result["official_fetch_status"] = fetched.status_code
            except Exception as exc:
                result["official_fetch_error"] = type(exc).__name__
            rows = web_search.web_search_sources("site:chinasilkmuseum.com 中国丝绸博物馆 免费开放 参观 杭州")
            result["research_sources"] = rows
            poi = candidate(rows)
            result["candidate"] = poi
            stack.enter_context(patch.object(tools, "search_attractions", return_value=[poi]))
            original_evaluate = reasoning.evaluate_research
            result["research_verdicts"] = []

            def evaluate(*args):
                verdict = original_evaluate(*args)
                result["research_verdicts"].append({"items": args[1], "verdict": verdict})
                return verdict

            stack.enter_context(patch.object(reasoning, "evaluate_research", evaluate))
            # 主检索消费同轮真实网页快照；研究额度单独有界，不冒充完整三域研究。
            limits = RunLimits(deadline_seconds=30, max_llm_calls=4, max_retrievals=6, max_research_calls=1)
            with use_task_limits(limits):
                pack = factory.run_research(
                    ResearchTask(
                        domain="attraction", city="杭州", budget=100, budget_currency="CNY", persons=1, limit=1
                    )
                )
            result["research_pack"] = pack.to_dict()
            save(output / "result.json", result)
            req = response.state.requirements.model_copy(deep=True)
            assert req.constraint_planning is not None
            req.constraint_planning.active_evidence = ActiveEvidenceOptions(
                max_queries=1, budget_units=2, time_limit_seconds=12, allow_source_search=True
            )
            request = GenerateRequest(
                city="杭州", days=1, persons=1, budget=100, start_date="2026-11-03", requirements_struct=req
            )
            prepared = prepare_candidates({"candidates": [poi]}, req.constraint_planning)
            _, problems = _problem_snapshots(request, prepared, date(2026, 11, 3))
            observations = []
            from dataclasses import asdict

            def observe(*args):
                obs = query_evidence(*args)
                observations.append(asdict(obs))
                return obs

            results, report = drive_evidence(
                problems,
                prepared,
                request,
                options=req.constraint_planning.active_evidence,
                solve=lambda current, seconds: solve_trip(current, budget=Decimal(100), time_limit_s=seconds),
                observe=observe,
            )
            result["planning"] = {
                "results": [asdict(r) for r in results],
                "report": report.model_dump(mode="json"),
                "observations": observations,
            }
            save(output / "result.json", result)
            task = ResearchTask(domain="attraction", city="杭州", budget=100, budget_currency="CNY", persons=1, limit=1)
            result["research_controls"] = {
                "missing_price": reasoning.evaluate_research(task, [{"name": MUSEUM, "remark": "未提供票价资料"}], 1),
                "synthetic": reasoning.evaluate_research(
                    task, [{"name": "地点A", "ticket_price": 20, "remark": "自制匿名合成资料，不是真实景点"}], 1
                ),
            }
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        timer.cancel()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["http_attempts"] = len(run.guard.records)
        result["reserved_upper_cny"] = str(run.guard.reserved)
        save(output / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    if not args.approved:
        raise SystemExit("需要本轮用户预算授权")
    report = execute(args.output)
    print(
        json.dumps({k: report.get(k) for k in ("error_type", "elapsed_seconds", "http_attempts", "reserved_upper_cny")})
    )
