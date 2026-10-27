"""本夜跨批次累计30元闸；用量已知按高峰价结算，未知保持整上下文预留，禁止重试。"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx

from scripts.p4_acceptance_budget import model_upper
from scripts.run_p4_acceptance import save

DEADLINE = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)  # Asia/Shanghai 2026-10-25 07:00
CARRIED = Decimal("0.068188")  # P3高峰0.023728 + P4旧0.01622 + 本轮已知0.02324 + 超时Fetch0.005


class NightBudget:
    def __init__(self, path):
        self.path = Path(path)
        if self.path.exists():
            self.state = json.loads(self.path.read_text("utf-8"))
            if self.state["cap_cny"] != "30":
                raise ValueError("夜间账本上限不符，禁止另开额度")
        else:
            self.state = {
                "cap_cny": "30",
                "carried_peak_upper_cny": str(CARRIED),
                "records": [],
                "deadline_utc": DEADLINE.isoformat(),
                "supplier_invoice_checked": False,
                "carry_sources": [
                    "sinan-upgrade-p3-real-vision-20261024.md",
                    "sinan-upgrade-p4-real-acceptance-20261024.md",
                    "sinan-upgrade-p4-live-30-20261024.md",
                ],
            }

    def total(self):
        return Decimal(self.state["carried_peak_upper_cny"]) + sum(
            (Decimal(r.get("settled_peak_cny", r["reserved_cny"])) for r in self.state["records"]), Decimal(0)
        )

    def reserve(self, identifier, payload):
        if datetime.now(UTC) >= DEADLINE:
            raise TimeoutError("已到07:00，停止新模型调用")
        if any(r["id"] == identifier for r in self.state["records"]):
            raise ValueError("此实验请求已消耗，禁止自动重试")
        if (
            payload.get("model") != "deepseek-flash"
            or payload.get("thinking") != {"type": "disabled"}
            or not 1 <= payload.get("max_tokens", 0) <= 4096
            or any(k in payload for k in ("tools", "functions", "stream"))
        ):
            raise ValueError("未核价的模型、思考或请求类型")
        amount = model_upper(payload["max_tokens"])
        if self.total() + amount > Decimal(30):
            raise ValueError("累计费用上界将超过30元；继续免费工作")
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

    def settle(self, identifier, data):
        usage = data.get("usage") or {}
        row = next(r for r in self.state["records"] if r["id"] == identifier)
        if all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")):
            row["settled_peak_cny"] = str(
                (Decimal(usage["prompt_tokens"]) * 2 + Decimal(usage["completion_tokens"]) * 8) / 1_000_000
            )
            row["status"] = "usage_known"
            row["usage"] = usage
        save(self.path, {**self.state, "total_peak_upper_cny": str(self.total())})


class NightClient:
    def __init__(self, ledger, output, binding):
        if binding.base_url.rstrip("/") != "https://api.deepseek.com" or binding.model != "deepseek-flash":
            raise ValueError("当前模型/endpoint偏离已核价范围")
        self.ledger, self.output, self.binding = ledger, Path(output), binding
        self.model, self.base_url = binding.model, binding.base_url
        self.output.mkdir(parents=True, exist_ok=True)
        self.identifier: str | None = None

    def json_response_format(self, *_args):
        return {"type": "json_object"}

    def complete(
        self, prompt, *, system_prompt, temperature: float = 0, max_tokens=512, response_format=None, json_mode=False
    ):
        if self.identifier is None:
            raise ValueError("模型请求缺少预登记实验ID")
        payload = {
            "model": "deepseek-flash",
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": response_format or {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 32768:
            raise ValueError("本批匿名文本超出32KiB；禁止扩大材料")
        return self._send(payload)

    def chat(self, messages, *, model, temperature, max_tokens, json_mode):
        if model != self.model or not json_mode or len(messages) != 1 or messages[0].get("role") != "user":
            raise ValueError("本轮视觉仅允许单图JSON抽取")
        blocks = messages[0].get("content", [])
        images = [b for b in blocks if b.get("type") == "image_url"]
        if len(blocks) != 2 or len(images) != 1 or not images[0]["image_url"]["url"].startswith("data:image/"):
            raise ValueError("只发送已核对的本地匿名图，不接受外部URL或多图")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        if len(json.dumps(payload, ensure_ascii=False).encode()) > 2 * 1024 * 1024:
            raise ValueError("本次自制图请求超出2MiB")
        return self._send(payload)

    def _send(self, payload):
        if self.identifier is None:
            raise ValueError("模型请求缺少预登记实验ID")
        request_file = self.output / f"{self.identifier}-request.json"
        response_file = self.output / f"{self.identifier}-response.json"
        if request_file.exists() and response_file.exists():
            if json.loads(request_file.read_text("utf-8")) != payload:
                raise ValueError("同一实验ID的提示已改变，禁止冒充原始输出")
            return json.loads(response_file.read_text("utf-8"))["choices"][0]["message"]["content"]
        self.ledger.reserve(self.identifier, payload)
        save(request_file, payload)
        with httpx.Client(timeout=httpx.Timeout(12, connect=4), follow_redirects=False) as client:
            response = client.post(
                "https://api.deepseek.com/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {self.binding.provider.api_key}"},
            )
        response_file.write_bytes(response.content)
        response.raise_for_status()
        data = response.json()
        self.ledger.settle(self.identifier, data)
        return data["choices"][0]["message"]["content"]
