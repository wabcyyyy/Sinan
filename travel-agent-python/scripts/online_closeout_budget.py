"""2026-10-25单场景授权：14元累计硬闸，含夜间历史，所有HTTP调用前记账。"""

import hashlib
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from scripts.p4_acceptance_budget import model_upper
from scripts.run_p4_acceptance import save

CAP = Decimal("14")
LIMITS = {"model": 6, "search": 4, "place": 2, "route": 2}
APPROVAL = "2026-10-25-human-approved-cumulative-14"


class OnlineBudget:
    def __init__(self, output: Path, prior: Path, *, approved=False):
        if not approved:
            raise ValueError("本轮付费范围尚未授权")
        self.output = output
        self.path = output / "budget.json"
        self.lock = threading.Lock()
        self.window_started: float | None = None
        previous = json.loads(prior.read_text("utf-8"))
        self.carried = Decimal(previous["total_peak_upper_cny"])
        if self.path.exists():
            self.state = json.loads(self.path.read_text("utf-8"))
            if self.state["approval"] != APPROVAL or Decimal(self.state["carried_cny"]) != self.carried:
                raise ValueError("授权或历史账本发生变化，禁止重置额度")
        else:
            self.state = {
                "approval": APPROVAL,
                "cap_cny": str(CAP),
                "carried_cny": str(self.carried),
                "prior_ledger": str(prior.resolve()),
                "records": [],
                "supplier_invoice_checked": False,
            }

    def total(self):
        return self.carried + sum(
            (Decimal(row.get("settled_cny", row["reserved_cny"])) for row in self.state["records"]), Decimal(0)
        )

    def before(self, request):
        host, path = request.url.host, request.url.path
        if host in {"127.0.0.1", "localhost"}:
            return None
        payload = json.loads(request.content) if request.method == "POST" else {}
        if host == "api.deepseek.com" and path == "/chat/completions":
            kind = "model"
            if (
                request.method != "POST"
                or payload.get("model") != "deepseek-flash"
                or payload.get("thinking") != {"type": "disabled"}
                or not 1 <= payload.get("max_tokens", 0) <= 4096
                or any(key in payload for key in ("tools", "functions", "stream"))
                or len(request.content) > 65536
            ):
                raise ValueError("模型或匿名材料超出本次范围")
            amount = model_upper(payload["max_tokens"])
        elif host == "api.deepseek.com" and path == "/v1/tools/search":
            kind, amount = "search", Decimal("0.005")
            query = str(payload.get("query", "")).casefold()
            if request.method != "POST" or not any(name in query for name in ("louvre", "orsay", "卢浮", "奥赛")):
                raise ValueError("仅允许两家公开博物馆的来源查询")
        elif host == "nominatim.openstreetmap.org" and path == "/search":
            kind, amount = "place", Decimal(0)
            query = str(request.url.params.get("q", "")).casefold()
            if request.method != "GET" or not any(name in query for name in ("louvre", "orsay", "卢浮", "奥赛")):
                raise ValueError("仅允许两家公开地点的命名查询")
        elif host == "routing.openstreetmap.de" and path.startswith("/routed-foot/table/v1/foot/"):
            kind, amount = "route", Decimal(0)
            if request.method != "GET":
                raise ValueError("只允许静态步行表查询")
        else:
            raise ValueError(f"本次未授权的外部来源：{host}")
        if request.url.scheme != "https":
            raise ValueError("外部调用必须使用HTTPS")
        signature = hashlib.sha256(str(request.url).encode() + request.content).hexdigest()
        with self.lock:
            rows = self.state["records"]
            now = datetime.now(UTC).timestamp()
            if self.window_started is None:
                self.window_started = now
                self.state.setdefault("call_windows", []).append({"started_epoch": now})
            if now - self.window_started >= 80:
                raise TimeoutError("本次80秒在线调用窗口已结束，停止新增请求")
            if any(row["signature"] == signature for row in rows):
                raise ValueError("禁止重复请求或自动重试")
            if sum(row["kind"] == kind for row in rows) >= LIMITS[kind]:
                raise ValueError(f"本轮{kind}次数已用完")
            if self.total() + amount > CAP:
                raise ValueError("累计费用上界将超过14元")
            identifier = f"{kind}-{len(rows) + 1}"
            rows.append(
                {
                    "id": identifier,
                    "kind": kind,
                    "signature": signature,
                    "reserved_cny": str(amount),
                    "status": "issued_usage_unknown",
                    "issued_at": datetime.now(UTC).isoformat(),
                }
            )
            self.output.mkdir(parents=True, exist_ok=True)
            save(self.output / f"{identifier}-request.json", {"url": str(request.url), "payload": payload})
            self.persist()
            return identifier

    def after(self, identifier, response):
        if identifier is None:
            return
        response.read()
        (self.output / f"{identifier}-response.json").write_bytes(response.content)
        try:
            data = response.json()
        except ValueError:
            return  # 无可核对用量，完整保留预留，不重试。
        with self.lock:
            row = next(row for row in self.state["records"] if row["id"] == identifier)
            row["http_status"] = response.status_code
            usage = data.get("usage") or {} if isinstance(data, dict) else {}
            if row["kind"] == "model" and all(
                type(usage.get(key)) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens")
            ):
                row["settled_cny"] = str(
                    (Decimal(usage["prompt_tokens"]) * 2 + Decimal(usage["completion_tokens"]) * 8) / 1_000_000
                )
                row["usage"] = usage
                row["status"] = "usage_known"
            elif row["kind"] != "model":
                row["status"] = "response_recorded"
            self.persist()

    def persist(self):
        save(self.path, {**self.state, "total_peak_upper_cny": str(self.total())})
