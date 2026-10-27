"""一次性执行已批准的 P4 最小清单；禁止扩大输入、重试或覆盖付费记录。"""

import argparse
import json
import os
import sys
import threading
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.p4_acceptance_budget import AcceptanceBudget


def save(path, value):
    def encode(item):
        if isinstance(item, Decimal):
            return str(item)
        return item.model_dump(mode="json")

    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=encode), encoding="utf-8")


def intake_request(plan):
    from app.schemas.trip import ClarifyRequest
    from app.schemas.trip_requirements import IntakeState

    lines = plan["requests"][0]["payload"]["messages"][1]["content"].splitlines()
    slots = json.loads(next(line.removeprefix("已有槽位：") for line in lines if line.startswith("已有槽位：")))
    requirements = json.loads(
        next(line.removeprefix("已有需求约束：") for line in lines if line.startswith("已有需求约束："))
    )
    message = next(line.removeprefix("用户说：") for line in lines if line.startswith("用户说："))
    return ClarifyRequest(message=message, state=IntakeState(**slots, requirements=requirements))


class PaidRun:
    def __init__(self, plan, output, *, approved, seconds=65):
        self.plan, self.output = plan, output
        self.deadline = time.monotonic() + seconds
        self.guard = AcceptanceBudget(plan, output / "paid-ledger.json", approved=approved)
        self.responses = {}

    def before(self, request):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("本次验收执行截止，不再发送请求")
        self.guard.before(request)
        request.extensions["acceptance_id"] = self.guard.records[-1]["id"]
        request.extensions["timeout"] = httpx.Timeout(min(18, remaining), connect=min(5, remaining)).as_dict()

    def after(self, response):
        identifier = response.request.extensions["acceptance_id"]
        body = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() >= self.deadline:
                raise TimeoutError("响应读取达到验收截止")
            body.extend(chunk)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError("响应超过 2 MiB")
        response._content = bytes(body)  # 缓存同一响应供原有非流式/流式适配器读取，不再发请求。
        (self.output / f"{identifier}-response.json").write_bytes(body)
        try:
            data = json.loads(body)
        except ValueError:
            data = {}
        self.responses[identifier] = {
            "http_status": response.status_code,
            "observed_at": datetime.now(UTC).isoformat(),
            "model": data.get("model"),
            "usage": data.get("usage"),
            "nonempty_results": bool(data.get("results")) if identifier.startswith("S") else None,
        }
        save(self.output / "response-metadata.json", self.responses)


def execute(plan, output, *, approved):
    from app.agent.data import deepseek_search
    from app.agent.editing import clarify
    from app.agent.research import reasoning
    from app.agent.research.evidence import ResearchTask
    from app.common import llm_client
    from app.common.config import settings
    from app.common.model_registry import binding
    from scripts.p4_acceptance_results import online, policy_comparison

    for role in ("main", "fast", "search"):
        bound = binding(role)
        if bound.base_url.rstrip("/") != "https://api.deepseek.com" or bound.model != "deepseek-flash":
            raise ValueError("角色绑定已偏离审批单，停止")
        if not bound.provider.api_key:
            raise ValueError("缺少 API 凭据，停止")
    output.mkdir(parents=True, exist_ok=False)
    save(
        output / "authorization.json",
        {
            "scope_id": plan["scope_id"],
            "approved": approved,
            "user_total_cap_cny": "10",
            "execution_cap_cny": "5",
            "approved_at": datetime.now(UTC).isoformat(),
            "no_retries": True,
            "pricing_rechecked": "2026-10-24",
        },
    )
    run = PaidRun(plan, output, approved=approved)

    def stop():
        save(output / "deadline-stop.json", {"status": "incomplete", "reason": "65-second execution limit"})
        os._exit(124)

    watchdog = threading.Timer(65, stop)
    watchdog.daemon = True
    watchdog.start()
    result = {"scope_id": plan["scope_id"], "started_utc": datetime.now(UTC).isoformat()}
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
            # 供应商原始 usage 已独立持久化；不写私人产品用户账本。
            stack.enter_context(patch.object(llm_client, "_record_usage", lambda *_a, **_kw: None))
            clients = {}
            for role in ("main", "fast"):
                bound = binding(role)
                client = llm_client.LLMClient(
                    base_url=bound.base_url,
                    api_key=bound.provider.api_key,
                    model=bound.model,
                    capabilities=bound.capabilities(),
                    timeout=18,
                    max_attempts=1,
                    follow_redirects=False,
                )
                stack.enter_context(patch.object(client, "_http", return_value=http))
                clients[role] = client
            stack.enter_context(patch.object(clarify, "get_llm_client", return_value=clients["main"]))
            stack.enter_context(patch.object(reasoning, "get_llm_client", return_value=clients["fast"]))
            stack.enter_context(patch.object(deepseek_search, "api_client", return_value=http))
            response = clarify.run_clarify(intake_request(plan))
            result["clarify"] = response.model_dump(mode="json")
            save(output / "result.json", result)
            task = ResearchTask(
                domain="attraction", city="Lisboa", budget=100, budget_currency="EUR", persons=1, limit=2
            )
            # 从已审阅的 M2 提示中读取同一冻结资料；不新增任何发送材料。
            text = plan["requests"][1]["payload"]["messages"][1]["content"]
            items = json.loads(text.split("条：", 1)[1].split("。判断", 1)[0])
            result["research"] = reasoning.evaluate_research(task, items, 1)
            save(output / "result.json", result)
            result["policies"] = policy_comparison(response.state, result["research"])
            save(output / "result.json", result)
            result["online"] = online()
            save(output / "result.json", result)
            if time.monotonic() < run.deadline - 2:
                # 独立时效/来源诊断，不把 S1 失败包装成重试或增加模型。
                try:
                    result["source_diagnostic"] = deepseek_search.search_pages(
                        plan["requests"][3]["payload"]["query"], binding("search")
                    )
                except Exception as exc:
                    result["source_diagnostic"] = {"error_type": type(exc).__name__}
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        watchdog.cancel()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["http_attempts"] = len(run.guard.records)
        result["reserved_upper_cny"] = str(run.guard.reserved)
        save(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approved", action="store_true")
    args = parser.parse_args()
    if not args.approved:
        raise SystemExit("需要本轮用户明确授权；本脚本不提供默认付费行为")
    result = execute(json.loads(args.manifest.read_text(encoding="utf-8")), args.output, approved=True)
    print(
        json.dumps({k: result.get(k) for k in ("error_type", "elapsed_seconds", "http_attempts", "reserved_upper_cny")})
    )


if __name__ == "__main__":
    main()
