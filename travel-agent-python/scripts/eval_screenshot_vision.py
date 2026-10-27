"""P3 授权四图验收；默认仅预检，--execute 后最多四个 HTTP 请求，无重试/重定向。"""

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.editing import screenshot as vision
from app.agent.runtime.run_limits import RunLimits, use_task_limits
from app.common.config import settings
from app.common.llm_client import LLMClient
from app.common.model_registry import binding
from app.services.image_intent import _to_data_uri
from scripts.screenshot_vision_checks import check_extraction

NAMES = ("guide", "booking", "guide_kyoto", "transport")
ROOT = Path(__file__).resolve().parents[1] / "tests/fixtures/screenshots"
INPUT_RATE = Decimal("2")  # 官方高峰、缓存未命中，元 / 百万 token；2026-10-24 核对。
OUTPUT_RATE = Decimal("8")
HARD_BUDGET = Decimal("1")
MAX_OUTPUT = 4096


def cost_upper(input_tokens, output_tokens=MAX_OUTPUT):
    return (Decimal(input_tokens) * INPUT_RATE + Decimal(output_tokens) * OUTPUT_RATE) / 1_000_000


def capture_payload(uri, kind, bound):
    class Capture:
        model, base_url = bound.model, bound.base_url

        def chat(self, messages, **kwargs):
            self.payload = {
                "model": self.model,
                "messages": messages,
                "temperature": kwargs["temperature"],
                "max_tokens": kwargs["max_tokens"],
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
            }
            return "{}"

    capture = Capture()
    with patch.object(vision, "get_llm_client", return_value=capture):
        vision.extract_screenshot(uri, kind)
    return capture.payload


class BudgetGuard:
    def __init__(self, payloads, output):
        self.allowed = payloads
        self.output = output
        self.records = []
        self.reserved = Decimal("0")
        self.active = None

    def save(self):
        self.output["calls"] = self.records
        self.output["reserved_upper_cny"] = str(self.reserved)
        target = Path(self.output["output_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def before(self, request):
        parsed = urlsplit(str(request.url))
        payload = json.loads(request.content)
        if (
            request.method != "POST"
            or payload.get("model") != "deepseek-flash"
            or payload.get("max_tokens") != MAX_OUTPUT
            or payload.get("thinking") != {"type": "disabled"}
            or payload.get("tools")
        ):
            raise ValueError("unapproved model/output/thinking/tools")
        if parsed.scheme != "https" or parsed.netloc != "api.deepseek.com" or parsed.path != "/chat/completions":
            raise ValueError("unapproved endpoint")
        if payload not in self.allowed or len(self.records) >= 4:
            raise ValueError("unapproved payload or four-call limit")
        index = self.allowed.index(payload)
        if any(r["fixture"] == NAMES[index] for r in self.records):
            raise ValueError("this fixture already consumed its one call; retry denied")
        upper_input = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 4096
        amount = cost_upper(upper_input)
        if self.reserved + amount > HARD_BUDGET:
            raise ValueError("hard CNY budget would be exceeded")
        self.reserved += amount
        self.active = {
            "fixture": NAMES[index],
            "started_utc": datetime.now(UTC).isoformat(),
            "input_token_upper": upper_input,
            "output_token_upper": MAX_OUTPUT,
            "reserved_cny": str(amount),
            "status": "issued_usage_unknown",
        }
        self.records.append(self.active)
        self.save()  # HTTP 发出前持久保留整笔上界，失败不退款、不重试。

    def after(self, response):
        response.read()
        record = self.active
        if record is None:
            raise ValueError("视觉响应缺少对应的调用前费用预留")
        record["http_status"] = response.status_code
        if response.status_code == 200:
            body = response.json()
            record["response_model"] = body.get("model")
            record["usage"] = body.get("usage")
            record["raw_choices"] = body.get("choices", [])
            record["status"] = "response_received"
            usage = body.get("usage") or {}
            if (
                usage.get("prompt_tokens", 0) > record["input_token_upper"]
                or usage.get("completion_tokens", 0) > MAX_OUTPUT
            ):
                self.output["stopped"] = "provider usage exceeded reservation; no more calls"
        self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("结果文件已存在；禁止覆盖或重复付费运行")
    bound = binding("vision")
    if bound.base_url.rstrip("/") != "https://api.deepseek.com" or bound.model != "deepseek-flash":
        raise SystemExit("当前视觉配置不是已核价的官方 endpoint/model，停止")
    if not bound.provider.api_key:
        raise SystemExit("未配置凭据，停止")
    uris = [_to_data_uri((ROOT / f"{name}.png").read_bytes()) for name in NAMES]
    payloads = [
        capture_payload(uri, "guide" if name.startswith("guide") else "booking", bound)
        for name, uri in zip(NAMES, uris, strict=True)
    ]
    bounds = [len(json.dumps(p, ensure_ascii=False).encode("utf-8")) + 4096 for p in payloads]
    total = sum((cost_upper(n) for n in bounds), Decimal("0"))
    if total > HARD_BUDGET:
        raise SystemExit(f"四次最坏预留 {total} 超过 1 元，停止")
    output = {
        "task": "SINAN-UPGRADE-P3 authorized four-image vision acceptance",
        "output_path": str(args.output.resolve()),
        "created_utc": datetime.now(UTC).isoformat(),
        "mode": "execute" if args.execute else "preflight_only",
        "model": bound.model,
        "endpoint": "https://api.deepseek.com/chat/completions",
        "thinking": "disabled",
        "max_calls": 4,
        "hard_budget_cny": "1",
        "all_calls_upper_cny": str(total),
        "input_upper_method": (
            "entire JSON UTF-8 bytes including base64, one token per byte plus 4096 framing reserve; "
            "image official cap 1024"
        ),
        "input_peak_cny_per_million": "2",
        "output_peak_cny_per_million": "8",
        "max_output_tokens": 4096,
        "pricing_source": "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/",
        "image_source": "https://api-docs.deepseek.com/guides/vision/",
        "results": [],
    }
    guard = BudgetGuard(payloads, output)
    guard.save()
    print(json.dumps({"mode": output["mode"], "all_calls_upper_cny": str(total), "max_calls": 4}), flush=True)
    if not args.execute:
        return
    # 实验进程内配置；不修改本地 .env 或正式模式设置。
    settings.llm_enable_thinking = False
    limits = RunLimits(deadline_seconds=80, max_llm_calls=4, max_tokens=1_000_000)
    with httpx.Client(
        transport=httpx.HTTPTransport(retries=0),
        follow_redirects=False,
        event_hooks={"request": [guard.before], "response": [guard.after]},
    ) as http:

        class Client(LLMClient):
            def _http(self):
                return http

        client = Client(
            base_url=bound.base_url,
            api_key=bound.provider.api_key,
            model=bound.model,
            capabilities=bound.capabilities(),
            max_attempts=1,
            follow_redirects=False,
            timeout=35,
        )
        with patch.object(vision, "get_llm_client", return_value=client), use_task_limits(limits):
            for name, uri in zip(NAMES, uris, strict=True):
                if output.get("stopped"):
                    break
                if limits.remaining() <= 1:
                    output["stopped"] = "80 second execution budget"
                    break
                started = time.monotonic()
                row: dict = {"fixture": name}
                try:
                    result, extractor = vision.extract_screenshot(
                        uri, "guide" if name.startswith("guide") else "booking"
                    )
                    row.update(extraction=result.model_dump(mode="json", by_alias=True), extractor=extractor)
                    reference = json.loads((ROOT / f"{name}.json").read_text(encoding="utf-8"))
                    row["evaluation"] = check_extraction(name, row["extraction"], reference)
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                    row["error"] = str(exc)[:300]
                row["seconds"] = round(time.monotonic() - started, 3)
                output["results"].append(row)
                guard.save()
                print(
                    json.dumps(
                        {
                            "fixture": name,
                            "seconds": row["seconds"],
                            "passed": row.get("evaluation", {}).get("passed"),
                            "error": row.get("error_type"),
                        }
                    ),
                    flush=True,
                )
    output["finished_utc"] = datetime.now(UTC).isoformat()
    guard.save()


if __name__ == "__main__":
    main()
