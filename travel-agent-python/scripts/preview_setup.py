"""Docker-only preview initialization. Standard library, no service imports or network."""

import argparse
import os
import re
import secrets
import stat
from pathlib import Path


def read_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if match:
            values[match[1]] = match[2].strip("\"'")
    return values


def render(template: Path, replacements: dict[str, str]) -> str:
    lines = template.read_text(encoding="utf-8-sig").splitlines()
    for index, line in enumerate(lines):
        key = line.lstrip("# ").partition("=")[0].strip()
        if key in replacements:
            lines[index] = f"{key}={replacements.pop(key)}"
    if replacements:
        raise ValueError("Configuration template lacks required keys")
    return "\n".join(lines) + "\n"


def create_env(path: Path, template: Path, values: dict[str, str]) -> None:
    text = render(template, values.copy())
    # O_EXCL prevents replacing a file created by a concurrent initializer.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    if os.name != "nt" and os.geteuid() == 0:
        owner = template.stat()
        os.fchown(descriptor, owner.st_uid, owner.st_gid)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"CREATED {path.name} (values withheld)")


def check_data(path: Path) -> None:
    if not path.is_dir():
        raise ValueError("data directory missing; run initialization")
    if os.name != "nt":
        info = path.stat()
        required = stat.S_IWOTH | stat.S_IXOTH
        if info.st_uid == 1000:
            required = stat.S_IWUSR | stat.S_IXUSR
        elif info.st_gid == 1000:
            required = stat.S_IWGRP | stat.S_IXGRP
        if info.st_mode & required != required:
            raise ValueError("data is not writable by UID 1000; inspect ownership before changing existing data")


def initialize(root: Path) -> None:
    compose_env = root / ".env"
    backend_env = root / "travel-agent-python/.env"
    # Read every needed template and existing file before any writes.
    compose_template = root / ".env.compose.example"
    backend_template = root / "travel-agent-python/.env.example"
    compose_template.read_text(encoding="utf-8-sig")
    backend_template.read_text(encoding="utf-8-sig")
    current = read_values(compose_env) if compose_env.exists() else {}
    if backend_env.exists():
        read_values(backend_env)
    data = root / "travel-agent-python/data"
    if data.exists():
        check_data(data)
    if not compose_env.exists():
        create_env(
            compose_env,
            compose_template,
            {
                "MYSQL_ROOT_PASSWORD": secrets.token_hex(32),
                "REDIS_PASSWORD": secrets.token_hex(32),
            },
        )
        current = read_values(compose_env)
    if any(not current.get(key) for key in ("MYSQL_ROOT_PASSWORD", "REDIS_PASSWORD")):
        raise ValueError("root environment lacks MySQL/Redis credentials; existing file preserved")
    if not backend_env.exists():
        create_env(
            backend_env,
            backend_template,
            {
                "JWT_SECRET": secrets.token_hex(32),
                "AGENT_INTERNAL_TOKEN": secrets.token_hex(32),
                "LLM_API_KEY": "",
                "DB_PASSWORD": current.get("MYSQL_ROOT_PASSWORD", ""),
                "DB_PORT": current.get("MYSQL_PORT", "3306"),
                "REDIS_PORT": current.get("REDIS_PORT", "6380"),
                "REDIS_PASSWORD": current.get("REDIS_PASSWORD", ""),
            },
        )
    effective = {**read_values(backend_env), **current}
    if any(not effective.get(key) for key in ("JWT_SECRET", "AGENT_INTERNAL_TOKEN")):
        raise ValueError("application credentials missing; existing file preserved")
    if not data.exists():
        data.mkdir(mode=0o750)
        if os.name != "nt" and os.geteuid() == 0:
            os.chown(data, 1000, 1000)
    check_data(data)
    print("OK: configuration present; existing files preserved. LLM is optional for /demo.")


def check(root: Path) -> None:
    for path in (root / ".env", root / "travel-agent-python/.env"):
        if not path.is_file():
            raise ValueError("Environment file missing; run initialization")
        read_values(path)
    check_data(root / "travel-agent-python/data")
    print("OK: files and data permissions. Run preview_doctor.py in the running backend for dependency checks.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        (initialize if args.command == "init" else check)(args.root.resolve())
    except (OSError, ValueError):
        print(
            "FAIL: missing/invalid template, unreadable environment file or data permissions. "
            "Check docs/public/troubleshooting.md; existing values were not overwritten."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
