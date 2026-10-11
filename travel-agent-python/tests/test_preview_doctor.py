"""Runtime diagnostic paths with mocked HTTP; no live dependencies."""

import importlib.util
import json
from pathlib import Path

from app.common.config import settings

spec = importlib.util.spec_from_file_location(
    "preview_doctor", Path(__file__).resolve().parents[1] / "scripts/preview_doctor.py"
)
assert spec and spec.loader
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


def test_dependency_checks_and_no_model_calls(monkeypatch, capsys):
    monkeypatch.setattr(type(settings), "validate_boot", lambda self: None)
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(doctor.logging, "disable", lambda _: None)
    urls = []

    def request(url, token=""):
        urls.append(url)
        if url.endswith("/deep"):
            return json.dumps({"data": {"mysql": "up", "redis": "up"}}).encode()
        if url.endswith("/demo"):
            return b'<div id="app"></div>'
        return json.dumps({"code": 200}).encode()

    monkeypatch.setattr(doctor, "request", request)
    assert doctor.diagnose()
    assert len(urls) == 4 and all(url.startswith("http://") for url in urls)
    assert "not configured" in capsys.readouterr().out


def test_failures_do_not_echo_exception_or_key(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise ValueError("private-value-must-not-appear")

    monkeypatch.setattr(type(settings), "validate_boot", fail)
    monkeypatch.setattr(doctor, "request", fail)
    monkeypatch.setattr(doctor.logging, "disable", lambda _: None)
    assert not doctor.diagnose()
    output = capsys.readouterr().out
    assert "FAIL" in output and "private-value-must-not-appear" not in output
