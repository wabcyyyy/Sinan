# Contract drift gate (G-1.1 / R0-1, R0-2). Called by scripts/check.ps1 and by CI.
#
# Semantics: re-export contracts from app/schemas/** and byte-compare with the files
# on disk *right now*. The previous version compared with HEAD, which made the only
# gate protecting the wire models un-passable in the middle of a feature (uncommitted
# artifacts always look like drift) -- so it could only go green after the commit it
# was supposed to guard.
#
# Also fails when an artifact exists on disk but is not tracked: contracts/openapi.json
# lived in .gitignore for weeks that way, invisible to every diff-based gate.
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check_contract_drift.ps1
# NOTE: ASCII-only comments - Windows PowerShell 5.1 reads BOM-less files as ANSI.

$ErrorActionPreference = 'Stop'
$pkg = Split-Path -Parent $PSScriptRoot
Set-Location $pkg

$rootDir = Split-Path -Parent $pkg
$artifactDirs = @(
    (Join-Path $rootDir "contracts"),
    (Join-Path $rootDir "travel-frontend-react\src\types\generated")
)
foreach ($d in $artifactDirs) {
    if (-not (Test-Path $d)) { Write-Host "FAIL: artifact dir missing: $d" -ForegroundColor Red; exit 1 }
}

$snap = Join-Path $pkg ".tmp-contract-pre"
if (Test-Path $snap) { Remove-Item -Recurse -Force $snap }
New-Item -ItemType Directory -Force -Path $snap | Out-Null
foreach ($d in $artifactDirs) {
    $dst = Join-Path $snap (Split-Path -Leaf $d)
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Copy-Item -Path (Join-Path $d "*") -Destination $dst -Force -ErrorAction SilentlyContinue
}

uv run python scripts/export_contracts.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$drift = @()
foreach ($d in $artifactDirs) {
    $dst = Join-Path $snap (Split-Path -Leaf $d)
    foreach ($f in @(Get-ChildItem -Path $d -File)) {
        $pre = Join-Path $dst $f.Name
        if (-not (Test-Path $pre)) { $drift += "NEW   $($f.Name)"; continue }
        if ((Get-FileHash -Algorithm SHA256 $f.FullName).Hash -ne (Get-FileHash -Algorithm SHA256 $pre).Hash) {
            $drift += "MOD   $($f.Name)"
        }
    }
    foreach ($p in @(Get-ChildItem -Path $dst -File)) {
        if (-not (Test-Path (Join-Path $d $p.Name))) { $drift += "GONE  $($p.Name)" }
    }
}
Remove-Item -Recurse -Force $snap
if ($drift.Count -gt 0) {
    Write-Host "FAIL: exported contracts disagree with app/schemas/**:" -ForegroundColor Red
    $drift | ForEach-Object { Write-Host "  $_" }
    Write-Host "fix: uv run python scripts/export_contracts.py, review, commit artifacts with the code"
    exit 1
}

Push-Location $rootDir
$untracked = @(git ls-files --others --exclude-standard -- contracts)
Pop-Location
if ($untracked.Count -gt 0) {
    Write-Host "FAIL: untracked contract artifacts (no diff-based gate can see them):" -ForegroundColor Red
    $untracked | ForEach-Object { Write-Host "  $_" }
    exit 1
}

Write-Host "OK: contracts match app/schemas/** byte-for-byte and every artifact is tracked"
exit 0
