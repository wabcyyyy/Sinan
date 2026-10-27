"""Historical SINAN-NL-01 protocol reader; its old limits are not defaults for new experiments.

OriginalBudget reuses ledger reading/accounting and overrides request validation with current project options.
This legacy reserve method is retained only for the old bounded protocol and its reproducibility tests.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from scripts.p4_acceptance_budget import model_upper
from scripts.run_p4_acceptance import save
from scripts.sinan_night_budget import NightBudget

APPROVAL = "SINAN-NL-01-20261025-human-allowed-new-CNY10"
NIGHT_UPGRADE = "SINAN-NIGHT-20261026-human-approved-cumulative-CNY30"
HUMAN_UNLIMITED = "SINAN-HUMAN154-20261026-human-explicit-unlimited"


class NLBudget(NightBudget):
    def __init__(self, path, *, approved=False):
        if not approved:
            raise ValueError("SINAN-NL-01 requires its own explicit approval")
        self.path = Path(path)
        self.state = (
            json.loads(self.path.read_text("utf8"))
            if self.path.exists()
            else {
                "approval": APPROVAL,
                "cap_cny": "10",
                "carried_peak_upper_cny": "0",
                "records": [],
                "pricing_url": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
                "pricing_checked_utc": "2026-10-25T15:00:00Z",
                "input_peak_cny_per_million": "2",
                "output_peak_cny_per_million": "8",
                "supplier_invoice_checked": False,
            }
        )
        unlimited = (
            self.state.get("cap_cny") is None
            and self.state.get("cap_mode") == "unlimited"
            and self.state.get("active_authorization", {}).get("id") == HUMAN_UNLIMITED
        )
        if (
            self.state["approval"] != APPROVAL
            or (self.state["cap_cny"] not in {"10", "30"} and not unlimited)
            or self.state["carried_peak_upper_cny"] != "0"
            or (self.state["cap_cny"] == "30" and self.state.get("approval_upgrade") != NIGHT_UPGRADE)
        ):
            raise ValueError("authorization/cap/carry changed; do not reset the ledger")

    def reserve(self, identifier, payload):
        if self.state["cap_cny"] is None:
            raise ValueError("legacy NL client is outside the Human154 unlimited authorization")
        if any(row["id"] == identifier for row in self.state["records"]):
            raise ValueError("already issued; inspect original output, no HTTP retry")
        if (
            payload.get("model") != "deepseek-flash"
            or payload.get("thinking") != {"type": "disabled"}
            or not 1 <= payload.get("max_tokens", 0) <= 4096
            or any(key in payload for key in ("tools", "functions", "stream"))
            or len(json.dumps(payload, ensure_ascii=False).encode()) > 32768
        ):
            raise ValueError("model/output/tool/text scope differs from the priced experiment")
        amount = model_upper(payload["max_tokens"])
        if self.total() + amount > Decimal(self.state["cap_cny"]):
            raise ValueError(f"SINAN-NL-01 cumulative CNY{self.state['cap_cny']} bound would be exceeded")
        self.state["records"].append(
            {
                "id": identifier,
                "reserved_cny": str(amount),
                "status": "issued_usage_unknown",
                "issued_at": datetime.now(UTC).isoformat(),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        save(self.path, {**self.state, "total_peak_upper_cny": str(self.total())})
