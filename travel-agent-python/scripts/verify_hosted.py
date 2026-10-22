"""Local-only hosted acceptance: real MySQL auth, credit, stopped-writer backup and isolated restore.

Run after compose.hosted-test.yml is healthy. Never invokes a model or touches the
normal development stack. Failed backups/restores remain in a named workspace temp directory.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "compose.hosted-test.yml"
PROJECT = "sinan-hosted-test"
BASE = "http://127.0.0.1:18000"


def run(args: list[str]) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr[-1500:]}")
    return result.stdout.strip()


def compose(project: str, *args: str, override: Path | None = None) -> str:
    command = ["docker", "compose", "-p", project, "-f", str(COMPOSE)]
    if override is not None:
        command += ["-f", str(override)]
    return run(command + list(args))


def login(client: httpx.Client, username: str) -> dict[str, str]:
    result = client.post("/api/auth/login", json={"username": username, "password": "example123"})
    assert result.status_code == 200, result.text
    cookie = SimpleCookie(result.headers["set-cookie"])
    assert cookie["TA_AUTH"]["secure"] and cookie["TA_AUTH"]["httponly"]
    # Local acceptance intentionally uses loopback HTTP; send this test cookie explicitly.
    return {"Cookie": "TA_AUTH=" + cookie["TA_AUTH"].value}


def exercise(client: httpx.Client, suffix: str) -> tuple[str, int]:
    codes = compose(
        PROJECT, "exec", "-T", "app", "python", "scripts/trial_admin.py", "invites", "--count", "2"
    ).splitlines()
    assert len(codes) == 2
    admin, tester = "example_admin_" + suffix, "example_tester_" + suffix
    for name, code in zip((admin, tester), codes, strict=True):
        response = client.post(
            "/api/auth/register", json={"username": name, "password": "example123", "inviteCode": code}
        )
        assert response.status_code == 200, response.text
    response = client.post(
        "/api/auth/register",
        json={"username": "example_reuse_" + suffix, "password": "example123", "inviteCode": codes[0]},
    )
    assert response.status_code == 400
    admin_headers = login(client, admin)
    tester_headers = login(client, tester)
    assert client.get("/api/admin/trial/users", headers=tester_headers).status_code == 403
    wallet = client.get("/api/user/trial-wallet", headers=tester_headers).json()["data"]
    assert wallet["balanceCny"] == 6
    user_id = wallet["userId"]
    compose(PROJECT, "exec", "-T", "app", "python", "scripts/trial_admin.py", "promote", admin)
    grant = {"amountCny": 0.1, "reason": "isolated acceptance", "requestId": str(uuid4())}
    for _ in range(2):
        response = client.post(f"/api/admin/trial/users/{user_id}/grants", headers=admin_headers, json=grant)
        assert response.status_code == 200 and response.json()["data"]["balanceCny"] == 6.1, response.text
    for status in (0, 1):
        assert client.put(f"/api/admin/users/{user_id}/status/{status}", headers=admin_headers).status_code == 200
        assert client.get("/api/user/info", headers=tester_headers).status_code == (401 if status == 0 else 200)
    for path in ("/api/agent/health", "/mcp", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert client.get("/demo").status_code == 200 and client.get("/healthz").status_code == 200
    print("PASS: invitation, one-time credit, grant replay, admin isolation, disable, public surface", flush=True)
    return tester, user_id


def backup_and_restore(client: httpx.Client, username: str, user_id: int, suffix: str) -> None:
    stage = Path(tempfile.mkdtemp(prefix=".tmp-hosted-", dir=ROOT)).resolve()
    assert stage.is_relative_to(ROOT.resolve())
    restored_project = "sinan-restore-" + suffix
    backup_tool = str(ROOT / "travel-agent-python/scripts/backup_restore.py")
    compose(
        PROJECT,
        "exec",
        "-T",
        "--user",
        "1000",
        "app",
        "python",
        "-c",
        "from pathlib import Path; Path('/app/data/acceptance-marker').write_text('persistent')",
    )
    compose(PROJECT, "stop", "app", "planning-worker")
    succeeded = False
    try:
        source = stage / "source"
        source.mkdir()
        compose(PROJECT, "cp", "app:/app/data/.", str(source))
        run(
            [
                sys.executable,
                backup_tool,
                "backup",
                "--compose-file",
                str(COMPOSE),
                "--project-name",
                PROJECT,
                "--data-dir",
                str(source),
                "--out",
                str(stage / "bundle"),
            ]
        )
        compose(restored_project, "up", "-d", "--wait", "--wait-timeout", "180", "mysql", "redis")
        restored = stage / "restored"
        run(
            [
                sys.executable,
                backup_tool,
                "restore",
                "--compose-file",
                str(COMPOSE),
                "--project-name",
                restored_project,
                "--data-dir",
                str(restored),
                "--from",
                str(stage / "bundle"),
                "--yes",
            ]
        )
        assert (restored / "acceptance-marker").read_text() == "persistent"
        override = stage / "restore.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "app": {
                            "image": PROJECT + "-app",
                            "volumes": [{"type": "bind", "source": str(restored), "target": "/app/data"}],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        compose(restored_project, "up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "app", override=override)
        headers = login(client, username)
        wallet = client.get("/api/user/trial-wallet", headers=headers).json()["data"]
        assert wallet["userId"] == user_id and wallet["balanceCny"] == 6.1
        assert any(entry["detail"].get("reason") == "isolated acceptance" for entry in wallet["entries"])
        print(
            "PASS: stopped-writer MySQL + data backup, empty-target restore, restored application login and ledger",
            flush=True,
        )
        succeeded = True
    finally:
        compose(restored_project, "down")  # Isolated test containers only; retain named DB volumes.
        compose(PROJECT, "start", "--wait", "--wait-timeout", "180", "app")
        if succeeded:
            shutil.rmtree(stage)  # Verified workspace child, newly created by this script.
        else:
            print(f"Failed restore evidence preserved at {stage}", file=sys.stderr)


def main() -> None:
    if not COMPOSE.is_file():
        raise RuntimeError("Run from the local checkout, not inside the hosted container")
    with httpx.Client(base_url=BASE, timeout=20) as client:
        suffix = uuid4().hex[:8]
        username, user_id = exercise(client, suffix)
        backup_and_restore(client, username, user_id, suffix)


if __name__ == "__main__":
    main()
