"""DashScope OpenAI 兼容网关 structured output 能力刺探（PR-5 前置 spike，D3 定档依据）。

用法（需真实 LLM_API_KEY；无密钥如实跳过，不冒充结果）：
    uv run python tests/probe_structured_output.py

刺探四问（每问一次最小 chat/completions 调用，max_tokens 从紧）：
1. `response_format={"type":"json_schema","json_schema":{...,"strict":true}}` 是否被网关接受？
2. strict 是否真被执行：诱导模型输出**违 schema** 的值（day_no 超 maximum、多余键），
   看输出是否被强约束纠正；
3. 对照组：同诱导在 `json_mode`（json_object）下的行为——两相对照才能区分
   「网关接受参数但不执行」与「真·强约束」；
4. 非 strict 的 json_schema 又如何（有的网关只支持非严格形态）。

结果直接打 stdout，记入 PR 描述按 D3 定档：
- 支持且执行 → 强约束档（生成主链路换 json_schema strict）；
- 不支持 → schema-in-prompt + Pydantic 校验 + 单次修复重试档（推广
  editing/chat_draft/decide.py 的已验证模式）。

退出码：0=刺探完成（无论支持与否）；2=无密钥跳过；3=网络层全部失败（结果不可判定）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.common.config import settings  # noqa: E402

# 刺探用的最小 schema：天序号有上界、条目闭合（additionalProperties: false）
DAY_SCHEMA = {
    "type": "object",
    "properties": {
        "day_no": {"type": "integer", "minimum": 1, "maximum": 7},
        "note": {"type": "string"},
    },
    "required": ["day_no", "note"],
    "additionalProperties": False,
}

# 故意诱导违规：要求 day_no=99（超上界）并带一个 schema 外键（strict 必须拦下）
TRAP_MESSAGES = [
    {
        "role": "user",
        "content": (
            "输出一个 JSON 对象：day_no 必须是 99，note 写「刺探」，"
            "另外再加一个 extra 字段随便给个值。只输出 JSON，不要解释。"
        ),
    }
]


def _call(label: str, response_format: dict | None) -> None:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": TRAP_MESSAGES,
        "temperature": 0,
        "max_tokens": 200,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    try:
        resp = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        print(f"[{label}] NETWORK-ERROR {exc}")
        return
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    if resp.status_code != 200:
        detail = json.dumps(body.get("error") or body, ensure_ascii=False)[:300]
        print(f"[{label}] REJECTED status={resp.status_code} error={detail}")
        return
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    print(f"[{label}] ACCEPTED raw={content[:200]!r}")
    try:
        data = json.loads(content)
    except ValueError:
        print(f"[{label}]   -> 输出不是纯 JSON（strict 未约束？）")
        return
    violations = []
    if not isinstance(data, dict):
        violations.append("非对象")
    else:
        if data.get("day_no") != 99:
            violations.append(f"day_no 被约束为 {data.get('day_no')!r}（诱导值 99 未出现 = 强约束生效）")
        else:
            violations.append("day_no=99 透传（上界未被执行）")
        if "extra" in data:
            violations.append("extra 键透传（闭合约束未被执行）")
        else:
            violations.append("extra 键被拒（闭合约束生效）")
    print(f"[{label}]   -> {'; '.join(violations)}")


def main() -> int:
    if not settings.llm_api_key:
        print("未配置 LLM_API_KEY：跳过刺探（不冒充结果）")
        return 2
    print(f"model={settings.llm_model} base={settings.llm_base_url}")
    _call(
        "A-json_schema-strict",
        {"type": "json_schema", "json_schema": {"name": "trip_day", "strict": True, "schema": DAY_SCHEMA}},
    )
    _call(
        "B-json_schema-nonstrict",
        {"type": "json_schema", "json_schema": {"name": "trip_day", "strict": False, "schema": DAY_SCHEMA}},
    )
    _call("C-json_object-baseline", {"type": "json_object"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
