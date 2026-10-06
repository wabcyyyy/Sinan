"""Back up/restore MySQL and the complete backend data directory (stdlib only).

Caller MUST stop ALL application/database/data-directory writers before either
operation and keep them stopped until completion; leave MySQL running. This tool
never stops services. Use only trusted, unmodified bundles produced by this tool:
SQL executes as root and is not sandboxed. Redis, env secrets and MySQL accounts
are not included. Point --data-dir at the actual mounted backend data directory.

A bundle is <out>/content/{dump.sql,data/}. Build in a private sibling temp dir,
reserve <out> exclusively, then atomically rename the completed content into it.
Existing output paths (including empty directories) are never replaced. Restore
requires --yes, an EXISTING EMPTY DB and an ABSENT data target (even empty existing
directories are refused). SQL import is nontransactional: failures can leave a
partial DB; no automatic rollback, DROP DATABASE, or cleanup of user data occurs.
Use --compose-file AND --project-name AND --data-dir for isolated rehearsal; the
alternate compose file must itself use isolated containers/volumes/ports.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import BinaryIO

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "travel-agent-python" / "data"
DEFAULT_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# Credentials expand ONLY inside the container, into the client environment,
# never into either host or mysql/mysqldump argv. Suppress raw client errors since
# they may contain SQL/user data; report operation and exit code instead.
AUTH = 'set -eu; : "${MYSQL_DATABASE:?}" "${MYSQL_ROOT_PASSWORD:?}"; export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '
CLIENT = 'mysql --no-defaults --user=root --batch --skip-column-names --database="$MYSQL_DATABASE" '
IDENTITY = CLIENT + '--execute="SELECT DATABASE(), @@hostname, @@port, VERSION();"'
EMPTY = CLIENT + (
    '--execute="SELECT '
    "(SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()) + "
    "(SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema=DATABASE()) + "
    "(SELECT COUNT(*) FROM information_schema.events WHERE event_schema=DATABASE()) + "
    '(SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema=DATABASE());"'
)
DUMP = (
    "mysqldump --no-defaults --user=root --single-transaction --quick --hex-blob "
    "--routines --events --triggers --no-tablespaces --set-gtid-purged=OFF "
    '--skip-add-drop-table --skip-add-locks -- "$MYSQL_DATABASE"'
)
IMPORT = CLIENT + "--binary-mode=1"


class BackupError(RuntimeError):
    """A refusal or failed operation, safe to display to the caller."""


def run_mysql(
    args: argparse.Namespace,
    script: str,
    operation: str,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> bytes:
    command = ["docker", "compose", "--file", str(checked_path(args.compose_file))]
    if args.project_name:
        command += ["--project-name", args.project_name]
    command += ["exec", "-T", "mysql", "sh", "-c", AUTH + "exec " + script]
    try:
        result = subprocess.run(
            command,
            stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as exc:
        raise BackupError(f"{operation}: could not execute docker ({type(exc).__name__})") from exc
    if result.returncode:
        raise BackupError(f"{operation}: docker/client exited {result.returncode}; raw output suppressed")
    return result.stdout or b""


def checked_path(value: str | Path) -> Path:
    """Reject lexical traversal and symlink/junction/reparse ancestors before resolving."""
    path = Path(value).expanduser()
    if ".." in path.parts:
        raise BackupError(f"parent traversal is not allowed: {path}")
    path = path.absolute()
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise BackupError(f"symlink/junction/reparse path refused: {component}")
    return path


def require_absent(path: Path) -> None:
    checked_path(path)
    if os.path.lexists(path):
        raise BackupError(f"path already exists (never overwritten, even if empty): {path}")
    if not path.parent.is_dir():
        raise BackupError(f"parent directory must already exist: {path.parent}")


def validate_tree(root: Path) -> None:
    checked_path(root)
    if not root.is_dir():
        raise BackupError(f"data directory missing: {root}")
    for child in root.iterdir():
        checked_path(child)
        mode = child.lstat().st_mode
        if stat.S_ISDIR(mode):
            validate_tree(child)
        elif not stat.S_ISREG(mode):
            raise BackupError(f"non-regular data file refused: {child}")


def copy_tree(src: Path, dst: Path) -> None:
    validate_tree(src)
    # Preserve empty directories as well as every file; no merging/overwriting.
    shutil.copytree(src, dst, copy_function=shutil.copy2)


def disjoint(first: Path, second: Path) -> None:
    if first.is_relative_to(second) or second.is_relative_to(first):
        raise BackupError(f"paths must not overlap: {first} and {second}")


def target_identity(args: argparse.Namespace) -> bytes:
    identity = run_mysql(args, IDENTITY, "read existing target identity").strip()
    if len(identity.split(b"\t")) != 4 or b"\n" in identity:
        raise BackupError("invalid target identity response")
    print(f"Compose: {checked_path(args.compose_file)}; project: {args.project_name or '(compose default)'}")
    print(f"Target DB / host / port / version: {identity.decode('utf-8', errors='replace')}", flush=True)
    return identity


def require_empty(args: argparse.Namespace) -> None:
    count = run_mysql(args, EMPTY, "check target emptiness").strip()
    if count != b"0":
        raise BackupError("target DB is not EMPTY (or invalid count); tables/views/routines/events/triggers refused")


def do_backup(args: argparse.Namespace) -> None:
    out = checked_path(args.out)
    data = checked_path(args.data_dir)
    require_absent(out)
    disjoint(out, data)
    validate_tree(data)
    target_identity(args)
    # mkdtemp is exclusive, private and on the destination filesystem.
    stage = Path(tempfile.mkdtemp(prefix=".backup-", dir=out.parent))
    reserved = False
    try:
        with (stage / "dump.sql").open("xb") as sql:
            run_mysql(args, DUMP, "SQL backup", stdout=sql)
            sql.flush()
            os.fsync(sql.fileno())
        if not (stage / "dump.sql").stat().st_size:
            raise BackupError("SQL backup was empty")
        copy_tree(data, stage / "data")
        require_absent(out)
        out.mkdir(mode=0o700)  # Exclusive reservation: never rename OVER an existing directory.
        reserved = True
        stage.rename(out / "content")  # Atomic publication inside our reserved bundle.
    except BaseException:
        if reserved:
            out.rmdir()  # Only our empty reservation, never recursively remove a destination.
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(f"Backup complete: {out}")


def do_restore(args: argparse.Namespace) -> None:
    if not args.yes:
        raise BackupError("explicit confirmation required: --yes (trusted bundle, stopped writers, EMPTY DB)")
    bundle = checked_path(args.from_dir)
    data = checked_path(args.data_dir)
    require_absent(data)
    disjoint(bundle, data)
    content = checked_path(bundle / "content")
    validate_tree(content)
    if {p.name for p in content.iterdir()} != {"dump.sql", "data"}:
        raise BackupError("invalid bundle content: expected dump.sql and data only")
    dump = content / "dump.sql"
    if not dump.is_file() or not dump.stat().st_size:
        raise BackupError("missing or empty dump.sql")
    validate_tree(content / "data")
    identity = target_identity(args)
    require_empty(args)
    # Stage all filesystem work before SQL is touched. No data target is created yet.
    stage = Path(tempfile.mkdtemp(prefix=".restore-", dir=data.parent))
    sql_started = False
    sql_done = False
    try:
        copy_tree(content / "data", stage / "data")
        require_absent(data)
        if target_identity(args) != identity:
            raise BackupError("target identity changed during preflight")
        require_empty(args)  # Recheck immediately before import; writers must remain stopped.
        with dump.open("rb") as sql:
            sql_started = True
            run_mysql(args, IMPORT, "SQL restore", stdin=sql)
        sql_done = True
        require_absent(data)
        # Reserve target exclusively; move staged entries without overwriting user files.
        # The whole data directory cannot be atomically published without a portable
        # no-replace directory rename. Exclusive copytree fails if ANY target exists.
        copy_tree(stage / "data", data)
    except BaseException as exc:
        if sql_started:
            state = "completed" if sql_done else "may be partially applied"
            print(
                f"PARTIAL RESTORE: SQL {state}; data target may be absent or incomplete: {data}. "
                "No DB rollback or data deletion performed. Keep writers stopped, inspect manually; "
                f"do not blindly retry. Failure: {type(exc).__name__}",
                file=sys.stderr,
            )
        raise
    finally:
        shutil.rmtree(stage)
    print(f"Restore complete: SQL imported; data restored to {data}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore"):
        p = sub.add_parser(name, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        p.add_argument("--compose-file", default=str(DEFAULT_COMPOSE_FILE), help="explicit compose file")
        p.add_argument("--project-name", help="compose project (use an isolated compose file too)")
        p.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="actual backend data directory")
        if name == "backup":
            p.add_argument("--out", required=True, help="new bundle directory; parent must exist")
        else:
            p.add_argument("--from", dest="from_dir", required=True, help="trusted bundle directory")
            p.add_argument("--yes", action="store_true", help="confirm restore; NEVER bypasses emptiness checks")
    args = parser.parse_args(argv)
    print("REQUIRED: caller must keep ALL writers stopped; MySQL stays running. No services are stopped here.")
    try:
        if args.command == "backup":
            do_backup(args)
        else:
            do_restore(args)
    except (BackupError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR: interrupted; inspect any reported partial restore before retrying", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
