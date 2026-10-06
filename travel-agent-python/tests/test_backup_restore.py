"""Offline backup/restore tests: real scratch files, mocked Docker subprocesses."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import backup_restore as br

SQL = b"-- SQL\r\nINSERT '\x00\xff\x80';\n"
IDENTITY = b"travel_assistant\tmysql-test\t3306\t8.0.36\n"


@pytest.fixture
def docker(monkeypatch):
    state = SimpleNamespace(count=b"0\n", identity=IDENTITY, fail=None, calls=[], imported=[], dumped=SQL)

    def run(command, *, stdin, stdout, stderr, check):
        assert stderr == subprocess.DEVNULL
        assert check is False
        script = command[-1]
        state.calls.append(command)
        if state.fail and state.fail in script:
            return subprocess.CompletedProcess(command, 9, b"")
        result = b""
        if br.IDENTITY in script:
            result = state.identity
        elif br.EMPTY in script:
            result = state.count
        elif br.DUMP in script:
            assert stdin == subprocess.DEVNULL
            assert stdout != subprocess.PIPE
            assert "b" in stdout.mode
            stdout.write(state.dumped)
        elif br.IMPORT in script:
            assert "b" in stdin.mode
            state.imported.append(stdin.read())
        else:
            pytest.fail(f"unexpected Docker script: {script}")
        return subprocess.CompletedProcess(command, 0, result)

    monkeypatch.setattr(br.subprocess, "run", run)
    return state


def bundle_at(root):
    content = root / "content"
    (content / "data" / "uploads").mkdir(parents=True)
    (content / "data" / "exports" / "empty").mkdir(parents=True)
    (content / "data" / "uploads" / "原图.bin").write_bytes(b"\x00\xfe\r\n")
    (content / "dump.sql").write_bytes(SQL)
    return root


def restore(bundle, target, *extra):
    return br.main(["restore", "--from", str(bundle), "--data-dir", str(target), "--yes", *extra])


def test_backup_binary_data_empty_directories_and_atomic_publication(tmp_path, docker, monkeypatch):
    source = bundle_at(tmp_path / "source") / "content" / "data"
    out = tmp_path / "backup"
    real_rename = Path.rename
    published = []

    def rename(path, target):
        assert target == out / "content"
        assert out.is_dir() and list(out.iterdir()) == []
        assert (path / "dump.sql").read_bytes() == SQL
        assert (path / "data" / "exports" / "empty").is_dir()
        published.append(True)
        return real_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    assert br.main(["backup", "--out", str(out), "--data-dir", str(source)]) == 0
    assert published
    assert (out / "content" / "dump.sql").read_bytes() == SQL
    assert (out / "content" / "data" / "uploads" / "原图.bin").read_bytes() == b"\x00\xfe\r\n"
    assert not list(tmp_path.glob(".backup-*"))
    assert not docker.imported


def test_restore_binary_and_full_data(tmp_path, docker, capsys):
    bundle = bundle_at(tmp_path / "bundle")
    target = tmp_path / "restored"
    assert restore(bundle, target) == 0
    assert docker.imported == [SQL]
    assert (target / "exports" / "empty").is_dir()
    assert (target / "uploads" / "原图.bin").read_bytes() == b"\x00\xfe\r\n"
    assert not list(tmp_path.glob(".restore-*"))
    assert "travel_assistant\tmysql-test\t3306\t8.0.36" in capsys.readouterr().out


def test_confirmation_before_any_io(tmp_path, docker):
    assert br.main(["restore", "--from", str(tmp_path / "missing")]) == 1
    assert docker.calls == []


@pytest.mark.parametrize("count", [b"1", b"7", b"-1", b"", b"garbage", b"0\n1"])
def test_nonempty_or_invalid_database_refused(tmp_path, docker, count, capsys):
    docker.count = count
    target = tmp_path / "target"
    assert restore(bundle_at(tmp_path / "bundle"), target) == 1
    assert not target.exists()
    assert not docker.imported
    assert "not EMPTY" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["directory", "file"])
@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_existing_destination_even_empty_refused(tmp_path, docker, kind, operation):
    bundle = bundle_at(tmp_path / "bundle")
    target = tmp_path / "target"
    if kind == "directory":
        target.mkdir()
    else:
        target.write_bytes(b"keep")
    if operation == "restore":
        code = restore(bundle, target)
    else:
        code = br.main(["backup", "--out", str(target), "--data-dir", str(bundle / "content" / "data")])
    assert code == 1
    assert docker.calls == []
    if kind == "file":
        assert target.read_bytes() == b"keep"


def test_default_nonempty_db_refused(tmp_path, docker, monkeypatch):
    docker.count = b"42"
    target = tmp_path / "default-data"
    monkeypatch.setattr(br, "DEFAULT_DATA_DIR", target)
    assert br.main(["restore", "--from", str(bundle_at(tmp_path / "bundle")), "--yes"]) == 1
    assert not docker.imported
    assert not target.exists()
    assert docker.calls[0][3] == str(br.DEFAULT_COMPOSE_FILE)


@pytest.mark.parametrize("fail", [br.IDENTITY, br.EMPTY])
def test_missing_database_or_preflight_command_failure(tmp_path, docker, fail):
    docker.fail = fail
    target = tmp_path / "target"
    assert restore(bundle_at(tmp_path / "bundle"), target) == 1
    assert not docker.imported
    assert not target.exists()


def test_dump_failure_or_empty_dump_never_publishes(tmp_path, docker):
    source = tmp_path / "data"
    source.mkdir()
    for fail, output in [(br.DUMP, SQL), (None, b"")]:
        docker.fail, docker.dumped = fail, output
        assert br.main(["backup", "--out", str(tmp_path / "out"), "--data-dir", str(source)]) == 1
        assert not (tmp_path / "out").exists()
        assert not list(tmp_path.glob(".backup-*"))


def test_sql_failure_does_not_publish_data_and_reports_partial(tmp_path, docker, capsys):
    docker.fail = br.IMPORT
    target = tmp_path / "target"
    assert restore(bundle_at(tmp_path / "bundle"), target) == 1
    assert not target.exists()
    assert "PARTIAL RESTORE: SQL may be partially applied" in capsys.readouterr().err


@pytest.mark.parametrize("phase", ["stage", "publish"])
def test_data_copy_errors(tmp_path, docker, monkeypatch, phase, capsys):
    bundle = bundle_at(tmp_path / "bundle")
    target = tmp_path / "target"
    real_copy = br.copy_tree

    def copy(src, dst):
        if (phase == "publish") == (dst == target):
            raise OSError("disk full")
        real_copy(src, dst)

    monkeypatch.setattr(br, "copy_tree", copy)
    assert restore(bundle, target) == 1
    assert bool(docker.imported) == (phase == "publish")
    assert ("PARTIAL RESTORE" in capsys.readouterr().err) == (phase == "publish")
    assert not list(tmp_path.glob(".restore-*"))


def test_backup_copy_failure_removes_private_staging(tmp_path, docker, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    monkeypatch.setattr(br, "copy_tree", Mock(side_effect=OSError("copy failed")))
    assert br.main(["backup", "--out", str(tmp_path / "out"), "--data-dir", str(source)]) == 1
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".backup-*"))


def test_backup_concurrent_destination_is_not_replaced(tmp_path, docker, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    out = tmp_path / "out"
    real_copy = br.copy_tree

    def copy(src, dst):
        real_copy(src, dst)
        out.mkdir()
        (out / "keep").write_bytes(b"keep")

    monkeypatch.setattr(br, "copy_tree", copy)
    assert br.main(["backup", "--out", str(out), "--data-dir", str(source)]) == 1
    assert (out / "keep").read_bytes() == b"keep"
    assert not (out / "content").exists()


def test_publish_rename_failure_cleans_reservation(tmp_path, docker, monkeypatch):
    source = tmp_path / "data"
    source.mkdir()
    monkeypatch.setattr(Path, "rename", Mock(side_effect=OSError("rename failed")))
    assert br.main(["backup", "--out", str(tmp_path / "out"), "--data-dir", str(source)]) == 1
    assert not (tmp_path / "out").exists()
    assert not list(tmp_path.glob(".backup-*"))


@pytest.mark.parametrize("entry", ["dump.sql", "data", "data/uploads/原图.bin"])
def test_mocked_reparse_symlinks_refused_without_windows_privileges(tmp_path, docker, monkeypatch, entry):
    bundle = bundle_at(tmp_path / "bundle")
    bad = bundle / "content" / entry
    real_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == bad:
            info = real_lstat(path, *args, **kwargs)
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", lstat)
    assert restore(bundle, tmp_path / "target") == 1
    assert not docker.calls


def test_traversal_and_overlap_refused(tmp_path, docker):
    data = tmp_path / "data"
    data.mkdir()
    for out in [data / "out", tmp_path / ".." / "escape"]:
        assert br.main(["backup", "--out", str(out), "--data-dir", str(data)]) == 1
    assert not docker.calls


@pytest.mark.parametrize("missing", ["dump.sql", "data"])
def test_invalid_bundle_missing_content(tmp_path, docker, missing):
    bundle = bundle_at(tmp_path / "bundle")
    path = bundle / "content" / missing
    path.rename(bundle / missing)
    assert restore(bundle, tmp_path / "target") == 1
    assert not docker.calls


def test_rehearsal_overrides_and_no_password_argv(tmp_path, docker):
    bundle = bundle_at(tmp_path / "bundle")
    compose = tmp_path / "isolated compose.yml"
    assert restore(bundle, tmp_path / "scratch", "--compose-file", str(compose), "--project-name", "rehearsal") == 0
    for command in docker.calls:
        assert command[:9] == [
            "docker",
            "compose",
            "--file",
            str(compose),
            "--project-name",
            "rehearsal",
            "exec",
            "-T",
            "mysql",
        ]
        assert command[9:11] == ["sh", "-c"]
        assert 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"' in command[-1]
        assert '-p"' not in command[-1]
        assert "--password" not in command[-1]
        assert "DROP DATABASE" not in command[-1]
    assert "information_schema.routines" in br.EMPTY
    assert "information_schema.events" in br.EMPTY
    assert "information_schema.triggers" in br.EMPTY
    assert "--databases" not in br.DUMP
    assert "--set-gtid-purged=OFF" in br.DUMP


def test_missing_docker_fails_cleanly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(br.subprocess, "run", Mock(side_effect=FileNotFoundError("docker")))
    assert restore(bundle_at(tmp_path / "bundle"), tmp_path / "target") == 1
    assert "could not execute docker" in capsys.readouterr().err


def test_target_changes_before_import_refused(tmp_path, docker, monkeypatch):
    bundle = bundle_at(tmp_path / "bundle")
    real_copy = br.copy_tree

    def copy(src, dst):
        real_copy(src, dst)
        docker.identity = b"different_db\tother-host\t3306\t8.0\n"

    monkeypatch.setattr(br, "copy_tree", copy)
    assert restore(bundle, tmp_path / "target") == 1
    assert not docker.imported
