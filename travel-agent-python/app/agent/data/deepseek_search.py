"""DeepSeek 独立 Search API：只返回实际搜索结果，不拿聊天记忆当来源。"""

from __future__ import annotations

import json

import httpx

from app.agent.runtime.run_limits import current_limits
from app.agent.runtime.trace import record_event
from app.common.http_client import api_client
from app.common.http_deadline import bounded_chunks
from app.common.model_registry import RoleBinding

SEARCH_REQUEST_CNY = 0.005  # 官方 2026-10-21：每千次成功且非空搜索 5 元，不包含模型 Token。


def search_pages(question: str, bound: RoleBinding) -> list[dict]:
    limits = current_limits()
    timeout = min(20.0, max(0.001, limits.remaining())) if limits else 20.0
    if limits:
        # 上层已预占检索次数；这里只检查在途截止期/取消，不能再拦掉最后一次额度。
        limits.check("http")
    success = False
    billable = None
    rows: list[dict] = []
    try:
        with api_client().stream(
            "POST",
            "https://api.deepseek.com/v1/tools/search",
            headers={"Authorization": f"Bearer {bound.provider.api_key}"},
            json={"query": question.strip()[:128], "count": 4, "need_content": True},
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                billable = False
            response.raise_for_status()
            body = bytearray()
            for chunk in bounded_chunks(response, limits):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("搜索响应超过 2 MiB")
            data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("搜索响应缺少 results 数组")
        success = True
        billable = bool(data["results"])
        for row in data["results"][:4]:
            if not isinstance(row, dict) or not str(row.get("url") or "").startswith(("https://", "http://")):
                continue
            rows.append(
                {
                    "url": row["url"],
                    "title": str(row.get("title") or "")[:200],
                    "snippet": str(row.get("snippet") or "")[:600],
                    "summary": str(row.get("summary") or "")[:600],
                    "content": str(row.get("content") or "")[:2400],
                }
            )
        return rows
    finally:
        record_event(
            "retrieval",
            "web_search.request",
            status="ok" if success else "error",
            metadata={
                "provider": "deepseek",
                "results": len(rows),
                "billable": billable,
                "estimated_cny": None if billable is None else SEARCH_REQUEST_CNY if billable else 0,
            },
        )


def matched_source(name: str, rows: list[dict]) -> str | None:
    """只为确实出现在搜索正文/摘要中的地点名附来源；未知实体不进入候选池。"""
    from app.agent.core.poi_identity import norm_poi_key

    needle = norm_poi_key(name)
    if not needle:
        return None
    for row in rows:
        text = " ".join(str(row.get(key) or "") for key in ("title", "snippet", "summary", "content"))
        if needle in norm_poi_key(text):
            return row["url"]
    return None
