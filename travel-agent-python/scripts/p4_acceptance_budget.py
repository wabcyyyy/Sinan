"""P4 最小付费试验的请求前预算闸；准备阶段不包含任何发网代码。"""

import json
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

INPUT_PEAK = Decimal("2")
OUTPUT_PEAK = Decimal("8")
SEARCH_PEAK = Decimal("0.005")
CONTEXT_UPPER = 1_048_576  # 官方上下文 1M，向 2^20 取大；不用字符/token 比例担保费用。
HARD_BUDGET = Decimal("5")
PRICING_URL = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"


def model_upper(max_output):
    return (Decimal(CONTEXT_UPPER) * INPUT_PEAK + Decimal(max_output) * OUTPUT_PEAK) / 1_000_000


class AcceptanceBudget:
    """一次授权仅允许清单中的每个 payload 发出一次，失败也不回收次数/预留。"""

    def __init__(self, plan, ledger_path, *, approved=False, hard_budget=HARD_BUDGET, kind_limits=None):
        self.plan = plan
        self.path = Path(ledger_path)
        if self.path.exists():
            raise ValueError("预算记录已存在，禁止覆盖/重复执行；先核对已发出的请求")
        self.approved = approved
        self.hard_budget = Decimal(hard_budget)
        self.kind_limits = {"model": 2, "search": 2} if kind_limits is None else dict(kind_limits)
        if not self.hard_budget.is_finite() or self.hard_budget <= 0:
            raise ValueError("硬预算必须为有限正数")
        if not self.kind_limits or any(
            k not in {"model", "search", "fetch"} or v < 1 for k, v in self.kind_limits.items()
        ):
            raise ValueError("付费请求类型或次数无效")
        self.records = []
        self.reserved = Decimal("0")

    def before(self, request):
        if not self.approved:
            raise ValueError("尚未取得本轮用户付费授权")
        url = urlsplit(str(request.url))
        if request.method != "POST" or url.scheme != "https" or url.netloc != "api.deepseek.com":
            raise ValueError("不在本次付费 endpoint 清单")
        payload = json.loads(request.content)
        matches = [r for r in self.plan["requests"] if r["path"] == url.path and r["payload"] == payload]
        if url.query or url.fragment or len(matches) != 1:
            raise ValueError("payload 或路径与已审阅清单不一致")
        row = matches[0]
        if row["kind"] not in self.kind_limits:
            raise ValueError("未登记的付费请求类型")
        if sum(r["kind"] == row["kind"] for r in self.records) >= self.kind_limits[row["kind"]]:
            raise ValueError("本轮该类付费 HTTP 请求次数已用尽")
        if any(r["id"] == row["id"] for r in self.records):
            raise ValueError("同一请求已消耗一次，禁止自动修复/重试")
        amount = model_upper(payload["max_tokens"]) if row["kind"] == "model" else SEARCH_PEAK
        if self.reserved + amount > self.hard_budget:
            raise ValueError("将超过人民币硬预算")
        self.reserved += amount
        self.records.append(
            {"id": row["id"], "kind": row["kind"], "reserved_cny": str(amount), "status": "issued_usage_unknown"}
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 写失败则 HTTP 不得发出；记录只含匿名请求 ID/额度，不保存 Authorization。
        self.path.write_text(
            json.dumps(
                {"records": self.records, "reserved_cny": str(self.reserved), "hard_budget_cny": str(self.hard_budget)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
