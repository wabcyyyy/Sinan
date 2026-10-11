"""Initialization operates only on temporary directories, never developer env/data."""

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/preview_setup.py"
spec = importlib.util.spec_from_file_location("preview_setup", SCRIPT)
assert spec and spec.loader
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
check_data = setup.check_data


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    repo = SCRIPT.parents[2]
    (tmp_path / "travel-agent-python").mkdir()
    for name in (".env.compose.example", "travel-agent-python/.env.example"):
        shutil.copyfile(repo / name, tmp_path / name)
    # Workflow unit tests need no chown privileges; actual UID checks are tested separately.
    monkeypatch.setattr(setup, "check_data", lambda path: None)
    return tmp_path


def test_fresh_and_repeated_initialization(fresh, capsys):
    setup.initialize(fresh)
    root_bytes = (fresh / ".env").read_bytes()
    app_bytes = (fresh / "travel-agent-python/.env").read_bytes()
    infra = setup.read_values(fresh / ".env")
    app = setup.read_values(fresh / "travel-agent-python/.env")
    assert app["LLM_API_KEY"] == ""
    assert app["DB_PASSWORD"] == infra["MYSQL_ROOT_PASSWORD"]
    assert app["REDIS_PASSWORD"] == infra["REDIS_PASSWORD"]
    assert all(len(value) == 64 for value in (app["JWT_SECRET"], app["AGENT_INTERNAL_TOKEN"], infra["REDIS_PASSWORD"]))
    setup.initialize(fresh)
    setup.check(fresh)
    assert (fresh / ".env").read_bytes() == root_bytes
    assert (fresh / "travel-agent-python/.env").read_bytes() == app_bytes
    output = capsys.readouterr().out
    assert app["JWT_SECRET"] not in output and infra["MYSQL_ROOT_PASSWORD"] not in output


def test_existing_infrastructure_preserved(fresh):
    original = "MYSQL_ROOT_PASSWORD=existing-local-value\nREDIS_PASSWORD=existing-cache-value\nMYSQL_PORT=3336\n"
    (fresh / ".env").write_text(original)
    setup.initialize(fresh)
    assert (fresh / ".env").read_text() == original
    assert setup.read_values(fresh / "travel-agent-python/.env")["DB_PORT"] == "3336"


def test_missing_template_does_not_create_env(fresh):
    (fresh / "travel-agent-python/.env.example").unlink()
    with pytest.raises(OSError):
        setup.initialize(fresh)
    assert not (fresh / ".env").exists()


def test_missing_config_is_failure(fresh):
    with pytest.raises(ValueError, match="missing"):
        setup.check(fresh)


def test_exclusive_create_and_template_alignment(fresh):
    target = fresh / ".env"
    target.write_text("keep-me")
    with pytest.raises(FileExistsError):
        setup.create_env(target, fresh / ".env.compose.example", {})
    assert target.read_text() == "keep-me"
    with pytest.raises(ValueError, match="lacks"):
        setup.render(fresh / ".env.compose.example", {"UNKNOWN_KEY": "value"})


def test_permission_error_is_redacted(fresh, monkeypatch, capsys):
    def denied(*args, **kwargs):
        raise PermissionError("sensitive-local-value")

    monkeypatch.setattr(setup.os, "open", denied)
    monkeypatch.setattr(sys, "argv", ["setup", "init", "--root", str(fresh)])
    assert setup.main() == 1
    assert "sensitive-local-value" not in capsys.readouterr().out


def test_incomplete_existing_config_is_diagnosed_without_changes(fresh, monkeypatch, capsys):
    original = "MYSQL_ROOT_PASSWORD=private-value\nREDIS_PASSWORD=\n"
    (fresh / ".env").write_text(original)
    monkeypatch.setattr(sys, "argv", ["setup", "init", "--root", str(fresh)])
    assert setup.main() == 1
    output = capsys.readouterr().out
    assert "REDIS_PASSWORD" in output and "private-value" not in output
    assert (fresh / ".env").read_text() == original
    assert not (fresh / "travel-agent-python/.env").exists()


def test_check_rejects_empty_effective_credentials(fresh):
    setup.initialize(fresh)
    with (fresh / ".env").open("a") as handle:
        handle.write("JWT_SECRET=\n")
    with pytest.raises(setup.SetupError, match="JWT_SECRET"):
        setup.check(fresh)


@pytest.mark.skipif(setup.os.name == "nt", reason="Linux container UID permission semantics")
def test_existing_data_permissions_are_checked_and_preserved(fresh, monkeypatch, capsys):
    monkeypatch.setattr(setup, "check_data", check_data)
    data = fresh / "travel-agent-python/data"
    data.mkdir(mode=0o500)
    original = data.stat()
    monkeypatch.setattr(sys, "argv", ["setup", "init", "--root", str(fresh)])
    assert setup.main() == 1
    assert "UID 1000" in capsys.readouterr().out
    assert data.stat().st_mode == original.st_mode
    assert data.stat().st_uid == original.st_uid
    assert not (fresh / ".env").exists()
