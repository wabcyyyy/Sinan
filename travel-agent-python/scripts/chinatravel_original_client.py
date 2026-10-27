"""Same supplier and ledger; full-context peak reservation, serial calls, unknown costs never released."""

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from app.agent.generation.content.constraint_candidate_policy import COMPLETE_OUTPUT_TOKENS
from app.common.llm_client import _apply_provider_options
from scripts.chinatravel_original_data import OUTPUT, SOURCE, save
from scripts.p4_acceptance_budget import model_upper
from scripts.sinan_budget_lock import atomic_ledger_save, ledger_lock
from scripts.sinan_nl_budget import HUMAN_UNLIMITED, NLBudget


class OriginalBudget(NLBudget):
    def __init__(self, path, *, approved=False):
        with ledger_lock(path):
            if not Path(path).is_file():
                raise ValueError("existing cumulative ledger required; no implicit budget or reset")
            super().__init__(path, approved=approved)

    def reserve(self, identifier, payload):
        with ledger_lock(self.path):
            self.state = json.loads(self.path.read_text("utf8"))
            self._reserve(identifier, payload)

    def _reserve(self, identifier, payload):
        # The original whole-trip authorization includes public official facts beyond the prior 32KiB POI scope.
        if any(row["id"] == identifier for row in self.state["records"]):
            raise ValueError("request already issued; no duplicate HTTP call")
        current_options = {}
        _apply_provider_options(current_options, "https://api.deepseek.com", False)
        if (
            payload.get("model") != "deepseek-flash"
            or any(payload.get(key) != current_options.get(key) for key in ("thinking", "reasoning_effort"))
            or not 1 <= payload.get("max_tokens", 0) <= COMPLETE_OUTPUT_TOKENS
            or any(key in payload for key in ("tools", "functions", "stream"))
            or not payload.get("messages")
            or any(not isinstance(message.get("content"), str) for message in payload["messages"])
            or len(json.dumps(payload, ensure_ascii=False).encode()) > 262144
        ):
            raise ValueError("outside current model/text/output/thinking scope")
        amount = model_upper(payload["max_tokens"])
        cap = self.state["cap_cny"]
        if cap is None:
            scope = self.state.get("active_authorization", {})
            prefixes = [f"human-v1-{uid}" for uid in scope.get("uids", [])] + scope.get("proof_prefixes", [])
            if (
                scope.get("id") != HUMAN_UNLIMITED
                or self.state.get("cap_mode") != "unlimited"
                or not any(identifier.startswith(prefix + "-") for prefix in prefixes)
            ):
                raise ValueError("unlimited authorization covers Human154 only")
        elif self.total() + amount > Decimal(cap):
            raise ValueError(f"shared cumulative CNY{cap} upper bound would be exceeded")
        self.state["original_task_authorization"] = (
            HUMAN_UNLIMITED if cap is None else "2026-10-26-human-approved-official-original-A-B-C-and-product"
        )
        self.state["latest_priced_reservation_utc"] = datetime.now(UTC).isoformat()
        self.state["records"].append(
            {
                "id": identifier,
                "reserved_cny": str(amount),
                "status": "issued_usage_unknown",
                "issued_at": datetime.now(UTC).isoformat(),
                "request_state": "in_flight",
                "authorization_id": self.state["original_task_authorization"],
            }
        )
        self._persist()

    def _persist(self):
        self.state["total_peak_upper_cny"] = str(self.total())
        atomic_ledger_save(self.path, self.state)

    def settle(self, identifier, data):
        with ledger_lock(self.path):
            self.state = json.loads(self.path.read_text("utf8"))
            row = next(r for r in self.state["records"] if r["id"] == identifier)
            usage = data.get("usage") or {}
            if all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")):
                cost = (
                    sum(
                        Decimal(usage[k]) * Decimal(self.state.get(rate, default))
                        for k, rate, default in (
                            ("prompt_tokens", "input_peak_cny_per_million", "2"),
                            ("completion_tokens", "output_peak_cny_per_million", "8"),
                        )
                    )
                    / 1_000_000
                )
                row.update(settled_peak_cny=str(cost), status="usage_known", usage=usage)
            row["request_state"] = "settled" if row["status"] == "usage_known" else "completed_usage_unknown"
            self._persist()

    def fail(self, identifier, failure_type):
        with ledger_lock(self.path):
            self.state = json.loads(self.path.read_text("utf8"))
            row = next(r for r in self.state["records"] if r["id"] == identifier)
            row.update(request_state="failed", failure_type=failure_type)
            self._persist()  # Unknown reservations stay charged at their full upper bound.


class OriginalClient:
    def __init__(self, prefix, *, call_limit=3, deadline=None, output=None):
        from app.common.model_registry import binding

        self.binding = binding("main")
        if (
            self.binding.base_url.rstrip("/") != "https://api.deepseek.com"
            or self.binding.model != "deepseek-flash"
            or not self.binding.provider.api_key.strip()
        ):
            raise ValueError("existing priced supplier/model credentials unavailable")
        self.ledger = OriginalBudget(SOURCE / "budget.json", approved=True)
        self.prefix, self.call_limit, self.calls = prefix, call_limit, 0
        self.deadline = deadline if deadline is not None else time.monotonic() + 78
        self.model = "deepseek-flash"
        self.output = output if output is not None else OUTPUT

    def json_response_format(self, *_args):
        return {"type": "json_object"}

    def complete(self, prompt, *, system_prompt, temperature=0, max_tokens=4096, response_format=None, **kwargs):
        if kwargs.get("enable_search"):
            raise ValueError("official source isolation forbids web search")
        return self.chat(
            [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
        )

    def chat(self, messages, *, temperature=0, max_tokens=4096, response_format=None, **_kwargs):
        if self.calls >= self.call_limit or self.deadline - time.monotonic() < 5:
            raise TimeoutError("bounded batch/call limit exhausted")
        identifier = f"{self.prefix}-{self.calls}"
        self.calls += 1
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": min(max_tokens, COMPLETE_OUTPUT_TOKENS),
            "response_format": response_format or {"type": "json_object"},
        }
        _apply_provider_options(payload, self.binding.base_url, False)
        request_file = self.output / f"models/{identifier}-request.json"
        response_file = self.output / f"models/{identifier}-response.json"
        if request_file.exists():
            if not response_file.exists() or json.loads(request_file.read_text("utf8")) != payload:
                raise ValueError("issued request cannot be changed or retried")
            data = json.loads(response_file.read_text("utf8"))
            return data["choices"][0]["message"]["content"]
        self.ledger.reserve(identifier, payload)
        try:
            save(request_file, payload)
            with httpx.Client(
                timeout=httpx.Timeout(min(60, self.deadline - time.monotonic()), connect=5), follow_redirects=False
            ) as client:
                response = client.post(
                    "https://api.deepseek.com/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.binding.provider.api_key}"},
                )
            response_file.write_bytes(response.content)
            response.raise_for_status()
            data = response.json()
            self.ledger.settle(identifier, data)
            return data["choices"][0]["message"]["content"]
        except Exception as exc:
            self.ledger.fail(identifier, type(exc).__name__)
            raise
