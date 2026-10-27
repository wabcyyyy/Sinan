"""仅生成 P4 匿名输入/费用预检文件；没有 --execute，没有网络请求。"""

import argparse
import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def build_plan():
    from app.agent.editing import clarify
    from app.agent.research import reasoning
    from app.agent.research.evidence import ResearchTask
    from app.common.config import settings
    from app.common.model_registry import binding
    from app.schemas.planning import ConstraintPlanningOptions
    from app.schemas.trip import ClarifyRequest
    from app.schemas.trip_requirements import IntakeState, PlaceRelation, TripRequirements
    from scripts.p4_acceptance_budget import CONTEXT_UPPER, HARD_BUDGET, PRICING_URL, SEARCH_PEAK, model_upper

    bindings = [binding(role) for role in ("main", "fast", "search")]
    for bound in bindings:
        if bound.base_url.rstrip("/") != "https://api.deepseek.com" or bound.model != "deepseek-flash":
            raise ValueError("当前角色不在已核价官方 endpoint/model，停止准备付费请求")
    state = IntakeState(
        city="Lisboa",
        days=1,
        persons=1,
        budget=100,
        start_date="2026-11-02",
        stay_nights=0,
        requirements=TripRequirements(
            constraint_planning=ConstraintPlanningOptions(country_code="PT", time_zone="Europe/Lisbon", currency="EUR"),
            place_relations=[
                PlaceRelation(constraint_id="anonymous-choice", kind="one_of", place_names=["地点A", "地点B"], day_no=1)
            ],
        ),
    )
    message = (
        "11月2日去里斯本玩1天，1个人，不住宿。活动总预算100欧元是硬上限，每天最多一个景点，"
        "不去地点C；第一天09:00以后开始、13:00以前结束。"
    )
    request = ClarifyRequest(message=message, state=state)
    task = ResearchTask(domain="attraction", city="Lisboa", budget=100, budget_currency="EUR", persons=1, limit=2)
    items = [
        {"name": "地点A", "ticket_price": 20, "tags": ["合成候选"], "remark": "匿名冻结资料；不是实际景点/票价"},
        {"name": "地点B", "ticket_price": 40, "tags": ["合成候选"], "remark": "匿名冻结资料；不是实际景点/票价"},
    ]
    captured = []

    class Capture:
        def json_response_format(self, *_args):
            return {"type": "json_object"}

        def complete(self, prompt, *, system_prompt, max_tokens, temperature, response_format=None, json_mode=False):
            captured.append(
                {
                    "model": "deepseek-flash",
                    "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": response_format or {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                }
            )
            return '{"sufficient":true,"extra_keywords":[]}'

    with patch.object(clarify, "get_llm_client", return_value=Capture()):
        clarify._ask(request, clarify._slots_projection(state), state)
    with (
        patch.object(reasoning, "get_llm_client", return_value=Capture()),
        patch.object(reasoning, "configured", return_value=True),
    ):
        reasoning.evaluate_research(task, items, 1)
    if len(captured) != 2 or any(len(json.dumps(p, ensure_ascii=False).encode()) > 32768 for p in captured):
        raise ValueError("匿名文本 payload 超出两份/32 KiB 限制，停止")
    requests = [
        {
            "id": name,
            "kind": "model",
            "path": "/chat/completions",
            "payload": payload,
            "reserved_upper_cny": str(model_upper(payload["max_tokens"])),
        }
        for name, payload in zip(("M1-clarify", "M2-research-evaluation"), captured, strict=True)
    ]
    queries = [
        "杭州 中国丝绸博物馆 官方 开放时间 闭馆日 停止入馆",
        "site:chinasilkmuseum.com 中国丝绸博物馆 开放 闭馆 2026",
    ]
    requests.extend(
        {
            "id": f"S{i}",
            "kind": "search",
            "path": "/v1/tools/search",
            "payload": {"query": query, "count": 4, "need_content": True},
            "reserved_upper_cny": str(SEARCH_PEAK),
        }
        for i, query in enumerate(queries, 1)
    )
    total = sum((model_upper(p["max_tokens"]) for p in captured), start=SEARCH_PEAK * len(queries))
    if total > HARD_BUDGET:
        raise ValueError("全清单计费上界超过硬预算，停止")
    return {
        "scope_id": "SINAN-P4-MIN-20261024",
        "mode": "preparation_only",
        "paid_authorized": False,
        "created_utc": datetime.now(UTC).isoformat(),
        "requests": requests,
        "limits": {
            "model_http_attempts": 2,
            "search_http_attempts": 2,
            "fetch_api_attempts": 0,
            "automatic_retries": 0,
            "automatic_json_repairs": 0,
            "provider_fallbacks": 0,
            "model_concurrency": 1,
            "execution_wall_seconds": 80,
            "public_page_get_attempts": 4,
        },
        "billing": {
            "hard_budget_cny": str(HARD_BUDGET),
            "all_requests_upper_cny": str(total),
            "input_context_token_upper_each": CONTEXT_UPPER,
            "input_peak_cny_per_million": "2",
            "output_peak_cny_per_million": "8",
            "search_cny_each": str(SEARCH_PEAK),
            "pricing_url": PRICING_URL,
            "source_prices_checked_date": "2026-10-24",
            "method": (
                "reserve published 1M context rounded upward to 2^20 input tokens; "
                "max_tokens output caps; no cache/off-peak/free-period assumptions"
            ),
        },
        "privacy": {
            "model_body": "one synthetic anonymous Lisbon request + fixed A/B evidence; original system prompts only",
            "no_upload": [
                "screenshots",
                "orders",
                "real user trips",
                "accounts",
                "chat histories",
                "local env",
                "repository files",
            ],
            "service_metadata": "provider sees API account and network IP; bodies contain no personal information",
        },
        "bindings": [
            {"role": b.role, "endpoint": b.base_url, "model": b.model, "key_configured": bool(b.provider.api_key)}
            for b in bindings
        ],
        "process_overrides_after_approval": {
            "thinking": "disabled",
            "max_attempts": 1,
            "follow_redirects": False,
            "note": "local process only; no .env or deployment change",
        },
        "current_config": {
            "thinking": settings.llm_enable_thinking,
            "web_search": settings.web_search_enabled,
            "osrm_configured": bool(settings.osrm_base_url),
            "ors_configured": bool(settings.ors_base_url),
        },
        "public_pages": [
            "https://www.chinasilkmuseum.com/cgjyy/info_6.aspx?itemid=31249",
            "https://www.chinasilkmuseum.com/xwdtIR/info_9.aspx?itemid=31595&parent=",
        ],
        "resource": {
            "disk_free_bytes": shutil.disk_usage(Path.cwd()).free,
            "min_free_memory_gib": 2,
            "suggested_cpus": 2,
            "artifact_budget_mib": 50,
            "gpu": False,
            "deployment": False,
            "new_dependencies": [],
            "regional_route_download": False,
        },
        "acceptance": [
            (
                "M1 actual clarify preserves confirmed one_of and extracts budget/window/exclusion; "
                "missing constraints fail, no manual fill"
            ),
            "M2 actual research evaluator consumes fixed evidence; null/insufficient stays explicit",
            (
                "reuse M1/M2 outputs for fixed/random/confidence/bottleneck with same limits; "
                "not four independent model samples"
            ),
            ("S1 enters opening_evidence/P4 update/re-solve; S2 is independent source/date inspection if needed"),
            (
                "check endpoint success, entity/date applicability, field adoption and business result separately; "
                "stale/ambiguous pages remain unknown"
            ),
        ],
        "not_covered": [
            "full three-domain research loop",
            "real models in every policy arm",
            "full live browser E2E",
            "online total cost advantage",
            "city coverage or statistical success rate",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise SystemExit("预检目录已存在，保留旧结果，请使用新目录")
    plan = build_plan()
    args.output_dir.mkdir(parents=True)
    (args.output_dir / "manifest.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in plan["requests"]:
        (args.output_dir / f"{row['id']}.json").write_text(
            json.dumps(row["payload"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(
        json.dumps(
            {
                "mode": plan["mode"],
                "paid_authorized": False,
                "paid_calls_made": 0,
                "upper_cny": plan["billing"]["all_requests_upper_cny"],
                "hard_budget_cny": plan["billing"]["hard_budget_cny"],
                "output": str(args.output_dir.resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
