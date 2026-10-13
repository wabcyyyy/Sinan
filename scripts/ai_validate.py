"""Plan-first local validation. Standard library only; CI remains authoritative."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ai_validate_graph import affected as analyze_imports
from ai_validate_graph import consumer_tests
from ai_validate_quick import select_quick
from ai_validate_run import run_validation

ROOT = Path(__file__).resolve().parents[1]
CFG = json.loads((ROOT / "validation-rules.json").read_text(encoding="utf-8"))


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace")


def paths() -> list[str]:
    return sorted(
        set(git("ls-files", "-z").split("\0"))
        | set(git("ls-files", "--others", "--exclude-standard", "-z").split("\0")) - {""}
    )


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def relevant(path: str) -> bool:
    return not matches(path, CFG["exclude"]) and (
        matches(path, CFG["inputs"]) or path in {"AGENTS.md", "VALIDATION.md", "validation-rules.json"}
    )


def fingerprint(files: list[str]) -> str:
    digest = hashlib.sha256()
    for name in files:
        if relevant(name):
            file = ROOT / name
            digest.update(name.encode())
            digest.update(hashlib.sha256(file.read_bytes()).digest() if file.is_file() else b"DELETED")
    return digest.hexdigest()


def affected(files: list[str], changed: list[str]) -> tuple[set[str], dict[str, set[str]]]:
    return analyze_imports(ROOT, CFG, files, changed)


def plan(args: argparse.Namespace, files: list[str], changed: list[str]) -> dict[str, Any]:
    reasons, minimum = [], 0
    quick = args.level in {"auto", "0", "1"}
    missing = []
    py = [name for name in changed if name.endswith(".py") and (ROOT / name).is_file()]
    fe = [name for name in changed if name.startswith(CFG.get("frontend", "<none>"))]
    docs_only = all(matches(name, CFG["docs"]) for name in changed) and not any(
        matches(name, CFG["high_risk"]) for name in changed
    )
    selected, graph = affected(files, changed) if not docs_only and py else (set(), {})
    domains = set()
    for rule in CFG["groups"]:
        if any(matches(name, rule["files"]) for name in changed):
            selected |= {name for name in files if matches(name, rule["tests"])}
            if rule["name"] != "docs":
                domains.add(rule["name"])
    selected |= set(args.tests)
    if not docs_only:
        minimum = 1
        if any(matches(name, CFG["high_risk"]) for name in changed):
            minimum, reasons = (
                2,
                ["shared configuration / security / execution boundary"],
            )
        elif len(domains) > 1:
            minimum, reasons = (
                2,
                ["multiple semantic modules changed; full regression stays in CI"],
            )
        for name in changed:
            if name.endswith(".py") and not matches(name, CFG["tests"]) and name not in graph:
                minimum, reasons = 2, ["deleted / unmapped Python input"]
                mapped = any(
                    rule["name"] != "docs"
                    and matches(name, rule["files"])
                    and any(matches(test, rule["tests"]) for test in files)
                    for rule in CFG["groups"]
                )
                if not args.tests and not mapped:
                    missing.append(name)
            if not matches(name, CFG["docs"]) and not name.endswith(".py") and name not in fe:
                minimum, reasons = (
                    2,
                    ["runtime/configuration input; validate its contract in CI"],
                )
        for name in py:
            if matches(name, CFG["tests"]) or CFG.get("manual") or args.tests:
                continue
            covered = consumer_tests(graph, [name], CFG["tests"]) or any(
                rule["name"] != "docs"
                and matches(name, rule["files"])
                and any(matches(test, rule["tests"]) for test in files)
                for rule in CFG["groups"]
            )
            if not covered:
                minimum, reasons = (
                    2,
                    ["a production file has no consumer tests / semantic mapping"],
                )
                missing.append(name)
        if py and not selected and not CFG.get("manual"):
            minimum, reasons = (
                2,
                ["no affected tests found; cannot report an empty selection as passed"],
            )
    requested = minimum if args.level == "auto" else int(args.level)
    level = requested if requested == 0 else max(requested, minimum)
    commands = [
        {
            "cwd": ".",
            "argv": [
                "git",
                "-c",
                "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol",
                "diff",
                getattr(args, "diff_base", "HEAD"),
                "--check",
                "--",
                *changed,
            ],
        }
    ]
    if py:
        commands.append(
            {
                "cwd": ".",
                "argv": [
                    "$PY",
                    "-c",
                    (
                        "import ast,pathlib,sys; "
                        "[ast.parse(pathlib.Path(p).read_text(encoding='utf-8-sig'),filename=p) for p in sys.argv[1:]]"
                    ),
                    *py,
                ],
            }
        )
    if py and CFG["ruff"]:
        commands += [
            {
                "cwd": CFG["python_cwd"],
                "argv": [
                    "$PY",
                    "-m",
                    "ruff",
                    *CFG.get("ruff_flags", []),
                    *mode,
                    *[os.path.relpath(ROOT / name, ROOT / CFG["python_cwd"]) for name in py],
                ],
            }
            for mode in (["check"], ["format", "--check"])
        ]
    if level >= 2 and level < 3 and not quick:
        expanded = [
            name
            for name in files
            if any(matches(name, rule["files"]) for rule in CFG["groups"] if rule["name"] in domains)
            and name.endswith(".py")
        ]
        if expanded:
            selected |= affected(files, expanded)[0]
    if level == 2 and py and not quick:
        commands += CFG["module_static"]
    deferred = []
    if quick and selected:
        selected, deferred = select_quick(CFG, graph, selected, changed, args.tests, CFG.get("quick_test_files", 6))
    if level == 3:
        commands += CFG["full"]
    else:
        if selected and (level > 0 or docs_only):
            commands.append(
                {
                    "cwd": CFG["python_cwd"],
                    "argv": [
                        "$PY",
                        "-m",
                        "pytest",
                        "-q",
                        "--durations=10",
                        *CFG["pytest_args"],
                        *[
                            os.path.relpath(ROOT / name.split("::")[0], ROOT / CFG["python_cwd"])
                            + ("::" + name.split("::", 1)[1] if "::" in name else "")
                            for name in sorted(selected)
                        ],
                        "--basetemp=$TEMP/pytest",
                    ],
                }
            )
        if fe and level > 0:
            commands += CFG["frontend_full"] if not quick or not CFG.get("frontend_related") else CFG["frontend_static"]
            if quick and CFG.get("frontend_related"):
                inputs = [
                    os.path.relpath(ROOT / name, ROOT / CFG["frontend"])
                    for name in fe
                    if (ROOT / name).is_file() and name.endswith((".ts", ".tsx", ".js", ".mjs"))
                ]
                if inputs:
                    commands.append(
                        {
                            "cwd": CFG["frontend"],
                            "argv": [
                                "node",
                                "node_modules/vitest/vitest.mjs",
                                "related",
                                "--run",
                                *inputs,
                            ],
                        }
                    )
                else:
                    commands += CFG["frontend_tests"]
                commands += CFG["frontend_node_tests"]
    return {
        "level": level,
        "minimum_level": minimum,
        "reasons": reasons,
        "changed": changed,
        "tests": sorted(selected),
        "commands": commands,
        "manual_required": (
            [
                "No regression mapped for: "
                + ", ".join(sorted(set(missing)))
                + "; add --tests or record a relevant manual smoke"
            ]
            if missing and level > 0 and level < 3
            else []
        ),
        "deferred_tests": deferred,
        "quick_mode": quick,
        "scope": "Selected local checks only; full regression is explicit or runs in CI",
    }


def execute(result: dict[str, Any], args: argparse.Namespace, files: list[str]) -> int:
    return run_validation(ROOT, CFG, result, args, files, git, paths, fingerprint)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--level", choices=["auto", "0", "1", "2", "3"], default="auto")
    parser.add_argument("--base", help="Git ref; includes merge-base..HEAD and all local changes")
    parser.add_argument(
        "--files",
        nargs="+",
        help="Task-scoped repository-relative paths; report unrelated dirty files separately",
    )
    parser.add_argument(
        "--tests",
        nargs="*",
        default=[],
        help="Additional repository-relative pytest nodes",
    )
    parser.add_argument("--run", action="store_true", help="Without this flag, only print a plan")
    parser.add_argument("--output", default=".validation")
    parser.add_argument(
        "--temp-root",
        default=str(Path(tempfile.gettempdir()) / "ai-validation"),
    )
    parser.add_argument("--timeout", type=int, default=3600, help="Per-command seconds")
    parser.add_argument("--budget", type=int, help="Total quick-run seconds; default from rules")
    parser.add_argument("--no-cache", action="store_true", help="Force fresh checks")
    args = parser.parse_args()
    if os.environ.get("CI", "").lower() in {"true", "1"}:
        args.level = "3"
        args.no_cache = True
    if args.budget is not None and args.budget <= 0:
        parser.error("--budget must be positive")
    files = paths()
    local = set(git("diff", "HEAD", "--name-only", "--no-renames", "-z").split("\0"))
    local |= set(git("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    if args.base:
        base = git("merge-base", "HEAD", args.base).strip()
        args.diff_base = base
        local |= set(git("diff", base, "HEAD", "--name-only", "--no-renames", "-z").split("\0"))
    changed = sorted({name.replace("\\", "/") for name in args.files} if args.files else local - {""})
    if any(
        Path(name.split("::")[0]).is_absolute() or ".." in Path(name.split("::")[0]).parts or name.startswith("-")
        for name in [*changed, *args.tests]
    ):
        parser.error("Use repository-relative paths inside this repository")
    if any(name not in files and not (ROOT / name).is_file() for name in changed):
        parser.error("Unknown changed file (check spelling); deleted tracked paths are allowed")
    if any(not (ROOT / name.split("::")[0]).is_file() for name in args.tests):
        parser.error("Additional pytest files must exist")
    if not changed and args.level != "3":
        print("No changed files. No checks run; this is not a validation pass.")
        return 0
    result = plan(args, files, changed)
    result["unrelated_dirty"] = sorted(local - set(changed) - {""}) if args.files else []
    if args.run:
        print(
            f"L{result['level']}: {len(changed)} changed files, {len(result['tests'])} selected test files; "
            f"{len(result['deferred_tests'])} deferred to CI",
            flush=True,
        )
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return execute(result, args, files) if args.run else 0


if __name__ == "__main__":
    raise SystemExit(main())
