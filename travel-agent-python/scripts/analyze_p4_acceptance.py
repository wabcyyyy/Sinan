"""复用一次真实输出归因；可选仅抓审批单中的两张免费官方页，不调用模型。"""

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_p4_acceptance import save


def billing(metadata):
    rows = []
    for identifier, row in metadata.items():
        usage = row.get("usage")
        if identifier.startswith("M") and usage:
            price = (Decimal(usage["prompt_tokens"]) * 2 + Decimal(usage["completion_tokens"]) * 8) / 1_000_000
        elif identifier.startswith("S") and row.get("nonempty_results"):
            price = Decimal("0.005")
        else:
            price = None
        rows.append(
            {"id": identifier, "usage": usage, "peak_price_upper_cny": str(price) if price is not None else None}
        )
    return {
        "rows": rows,
        "usage_priced_upper_cny": str(sum(Decimal(r["peak_price_upper_cny"] or "0") for r in rows)),
        "all_usage_known": all(r["peak_price_upper_cny"] is not None for r in rows),
        "note": "按实际 usage 和高峰价格保守计算，不是供应商账单；原预留不返还供额外调用",
    }


def replay_online(run_dir):
    from app.agent.data import deepseek_search
    from scripts.p4_acceptance_results import online

    calls = []

    def respond(request):
        expected = {"query": "杭州 中国丝绸博物馆 官方 开放时间 闭馆日 停止入馆", "count": 4, "need_content": True}
        if (
            request.method != "POST"
            or str(request.url) != "https://api.deepseek.com/v1/tools/search"
            or json.loads(request.content) != expected
            or calls
        ):
            raise ValueError("回放仅允许原 S1，不允许任意网络/查询")
        calls.append("S1")
        return httpx.Response(200, content=(run_dir / "S1-response.json").read_bytes())

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch.object(deepseek_search, "api_client", return_value=client),
    ):
        result = online()
    return {
        "method": "offline replay of original S1, zero new paid requests",
        "transport_replays": calls,
        "online": result,
    }


def public_pages(plan, run_dir):
    output = run_dir / "public-pages.json"
    if output.exists():
        raise ValueError("免费页面记录已存在，本次不重复 GET")
    allowed = tuple(plan["public_pages"])
    attempts = []

    def before(request):
        if (
            request.method != "GET"
            or str(request.url) not in allowed
            or len(attempts) >= 4
            or "authorization" in request.headers
        ):
            raise ValueError("不在免费 GET 审批范围")
        attempts.append({"url": str(request.url), "observed_at": datetime.now(UTC).isoformat(), "status": "issued"})
        save(output, attempts)

    with httpx.Client(
        timeout=httpx.Timeout(3, connect=2), follow_redirects=False, event_hooks={"request": [before]}
    ) as client:
        for index, url in enumerate(allowed, 1):
            try:
                response = client.get(url)
                row = attempts[-1]
                row["http_status"] = response.status_code
                response.raise_for_status()
                (run_dir / f"public-{index}.html").write_bytes(response.content)
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", response.text))
                row["opening_related_excerpts"] = [
                    text[max(0, m.start() - 90) : m.end() + 230]
                    for m in list(re.finditer(r"开放时间|周一|闭馆|暂停开放|2025年|2023年", text))[:12]
                ]
                row["status"] = "received"
            except Exception as exc:
                attempts[-1]["status"] = type(exc).__name__
            save(output, attempts)
    return attempts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approved-public-get", action="store_true")
    args = parser.parse_args()
    run_dir = args.run_dir
    metadata = json.loads((run_dir / "response-metadata.json").read_text("utf-8"))
    report: dict = {"billing": billing(metadata), "online_replay": replay_online(run_dir)}
    if args.approved_public_get:
        report["public_pages"] = public_pages(json.loads(args.manifest.read_text("utf-8")), run_dir)
    save(run_dir / "analysis.json", report)
    print(
        json.dumps(
            {"billing": report["billing"], "free_get_attempts": len(report.get("public_pages", []))}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
