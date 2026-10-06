# Thin wrapper so the gate is invocable as scripts/typecheck.ps1 (see AGENTS.md / check.ps1).
# The implementation lives in typecheck.py: pyright's JSON output is UTF-8 with Chinese
# messages, which PowerShell 5.1 native decoding mangles.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    uv run python (Join-Path $PSScriptRoot "typecheck.py") @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
