# Single-command local gate (G-0.4). Runs the cheap gates first, offline tests last.
# CI (ci.yml preflight job) runs the SAME steps in the SAME order: ruff check ->
# ruff format --check -> typecheck (pyright baseline ratchet) -> import-linter ->
# secret scan -> offline pytest.
# The last step compares regenerated contract artifacts byte-for-byte with the
# working tree, so it is meaningful mid-feature (it does not diff against HEAD).
# Usage (from anywhere): powershell -NoProfile -ExecutionPolicy Bypass -File scripts\check.ps1
# Opt out of the diff-cover step only when no baseline ref exists:
#   ... -File scripts\check.ps1 -AllowNoDiffCover
# NOTE: ASCII-only comments - Windows PowerShell 5.1 reads BOM-less files as ANSI.

param([switch]$AllowNoDiffCover)

$ErrorActionPreference = 'Stop'
$pkg = Split-Path -Parent $PSScriptRoot
Set-Location $pkg

function Step([string]$name) { Write-Host "==> $name" -ForegroundColor Cyan }

Step "ruff check"
uv run ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "ruff format --check"
uv run ruff format --check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "typecheck (pyright baseline ratchet)"
uv run python scripts/typecheck.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "import boundaries (import-linter, G-1.2)"
uv run lint-imports
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "code metrics (long functions / huge files ratchet)"
uv run python scripts/code_metrics.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "secret scan"
$root = Split-Path -Parent $pkg
$secrets = Join-Path $root "scripts\check-secrets.ps1"
if (Test-Path $secrets) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $secrets
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Step "offline tests (+ coverage)"
# Windows: pytest tmp_path factory scans legacy pytest-of-* dirs under the system
# TEMP and can hit permission errors (WinError 5). Redirect into the repo.
$tmp = Join-Path $pkg ".tmp-pytest"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$env:TEMP = $tmp
$env:TMP = $tmp
# tests/agent_eval 的度量层单测（PR-0 从 --ignore 解除）已进离线套件：mock 驱动、
# 零外网，与其余单测同一门禁。tests/api（活栈）与 tests/perf（压测）仍排除。
uv run pytest tests/ -q --ignore=tests/api --ignore=tests/perf --cov=app --cov-report=xml --cov-report=
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Step "coverage gate on changed lines (diff-cover, G-3.4)"
# Threshold only ever goes up (INV-1). Baseline is origin/master; when that ref is
# missing (shallow clone / offline repo) fall back to HEAD~1 instead of silently
# skipping -- a skipped gate must never be mistaken for a passed one (R0-6).
$baseline = "origin/master"
git rev-parse --verify --quiet $baseline > $null
if ($LASTEXITCODE -ne 0) {
    $baseline = "HEAD~1"
    git rev-parse --verify --quiet $baseline > $null
    if ($LASTEXITCODE -ne 0) {
        if ($AllowNoDiffCover) {
            Write-Host "  skipped via -AllowNoDiffCover (no baseline available)" -ForegroundColor Yellow
            $baseline = ""
        } else {
            Write-Host "  FAIL: neither origin/master nor HEAD~1 available; pass -AllowNoDiffCover to opt out" -ForegroundColor Red
            exit 1
        }
    } else {
        Write-Host "  fallback baseline: origin/master missing, comparing against $baseline" -ForegroundColor Yellow
    }
}
if ($baseline -ne "") {
    uv run diff-cover coverage.xml --compare-branch=$baseline --fail-under=70
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Step "contract export drift"
# R0-1/R0-2: re-export, byte-compare with the working tree, then require every
# artifact to be tracked. Extracted into scripts/check_contract_drift.ps1 so a
# deliberate violation can be proven red in seconds, not after the full gate.
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "check_contract_drift.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ""
Write-Host "ALL GREEN" -ForegroundColor Green
