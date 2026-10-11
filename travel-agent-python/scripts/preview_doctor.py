"""Read-only runtime diagnostics. Run inside agent-python; no model calls or writes."""

import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def request(url: str, token: str = "") -> bytes:
    headers = {"X-Agent-Token": token} if token else {}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=8) as response:
        return response.read(2_000_000)


def diagnose() -> bool:
    from app.common.config import settings

    # Do not print dependency exceptions: URLs may embed passwords.
    logging.disable(logging.CRITICAL)
    checks: list[tuple[str, bool]] = []
    try:
        settings.validate_boot()
        checks.append(("configuration", True))
    except (RuntimeError, ValueError):
        checks.append(("configuration (check secret placeholders and binding)", False))
    token = settings.agent_internal_token
    try:
        request("http://127.0.0.1:8000/api/agent/health", token)
        checks.append(("backend process", True))
        body = json.loads(request("http://127.0.0.1:8000/api/agent/health/deep", token)).get("data", {})
        checks.extend((name, body.get(name) == "up") for name in ("mysql", "redis"))
    except (OSError, ValueError):
        checks.append(("backend/dependencies (inspect compose health and env precedence)", False))
    try:
        shell = request("http://frontend-react/demo")
        checks.append(("frontend /demo SPA fallback", b'id="app"' in shell))
        hello = json.loads(request("http://frontend-react/api/test/hello"))
        checks.append(("business API proxy", hello.get("code") == 200))
    except (OSError, ValueError):
        checks.append(("frontend/proxy (inspect frontend-react logs)", False))
    for label, passed in checks:
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    print("LLM: " + ("configured; not called" if settings.llm_api_key.strip() else "not configured; /demo available"))
    return all(passed for _, passed in checks)


if __name__ == "__main__":
    try:
        healthy = diagnose()
    except Exception:
        print("FAIL: configuration import; check environment syntax (values withheld)")
        healthy = False
    raise SystemExit(0 if healthy else 1)
