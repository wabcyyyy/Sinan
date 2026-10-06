#!/usr/bin/env powershell
# Pre-commit / pre-push secret scan for Travel-Assistant.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check-secrets.ps1
# Exit 1 if suspicious secrets found in project sources, or if a real .env got tracked.
#
# Note: local .env contents are intentionally not scanned (live keys live only in
# gitignored local files). The dangerous case is a .env file being tracked by git,
# which the git ls-files check below catches. Keep this file ASCII-only: Windows
# PowerShell 5.1 reads UTF-8 without BOM as ANSI and can mangle non-ASCII comments.

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

# Patterns match real key shapes. Placeholders like "your-llm-api-key" are not matched.
$patterns = @(
    'sk-[A-Za-z0-9._-]{20,}'
    'sk-ws-[A-Za-z0-9._-]{10,}'
    'eyJhbGciOi[A-Za-z0-9_-]{20,}'
    '-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----'
    'AKIA[0-9A-Z]{16}'
    'AIza[0-9A-Za-z_\-]{35}'
    'AMAP_WEB_KEY=[a-f0-9]{25,}'
    'UNSPLASH_ACCESS_KEY=[A-Za-z0-9_-]{25,}'
    'PEXELS_ACCESS_KEY=[A-Za-z0-9]{25,}'
)

# Generic high-entropy assignment: key names containing PASSWORD/SECRET/TOKEN/
# API_KEY/ACCESS_KEY/APP_KEY followed by a 32+ char token. Placeholders are
# excluded afterwards via $placeholderMarkers.
$entropyPattern = '(?i)(?:PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY|APP_KEY)[A-Z_]*\s*[=:]\s*[''"]?([A-Za-z0-9+/_\-]{32,})'
$placeholderMarkers = @('replace', 'your-', 'change-me', 'changeme', 'example', 'placeholder', 'xxxx')

# Low-entropy credential assignments. The 32+ char gate above cannot see a short
# password, and one sat in a tracked debug script undetected. Quoted values only,
# so env indirection (password=os.environ["DB_PASSWORD"]) and yml placeholders
# (${MYSQL_PASSWORD:}) stay clean; placeholders are still excluded below.
$assignPattern = '(?i)(?:password|passwd|db_?pwd)\s*[=:]\s*[''"]([A-Za-z0-9+/_\-]{4,})[''"]'

# Separator-agnostic (Windows \ vs Linux /): must skip the same dirs on both.
# .dsh_tmp: session artifact dump (live-verify scripts + cookie jars); stays
# untracked by design, same category as logs/.tmp-*.
$skip = '[/\\](\.git|node_modules|\.venv[^\\/]*|\.uv-cache|\.uv-python|\.pnpm-store|models|dist|target|\.idea|\.vscode|data|logs|\.test-report|site-packages|\.tmp-[^\\/]*|\.dsh_tmp[^\\/]*)[/\\]'
$found = New-Object System.Collections.Generic.List[string]

function Add-Hit([string]$rel, [string]$line, [int]$lineNumber) {
    Write-Host "HIT $rel"
    if ($line.Length -gt 140) { $line = $line.Substring(0, 140) }
    # Never echo the matched literal back into CI logs.
    $line = $line -replace '([=:]\s*)["\x27][^"\x27]{3,}["\x27]', '$1"<redacted>"'
    Write-Host ("  L{0}: {1}" -f $lineNumber, $line)
    if (-not $found.Contains($rel)) { $found.Add($rel) | Out-Null }
}

# ---- 1. Tracked .env files (except *.example templates) fail immediately ----
# Once a .env is committed the keys are in git history: rotation alone is not
# enough, history must be rewritten. Catching it before the commit is cheaper.
$trackedEnv = git -C $root ls-files -- '.env' '.env.*' 2>$null |
    Where-Object { $_ -and $_ -notmatch '\.example$' }
foreach ($rel in $trackedEnv) {
    Add-Hit $rel "tracked .env file (real env files must stay gitignored)" 0
}

# ---- 2. Real key shapes in project sources ----
$files = Get-ChildItem -Path $root -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch $skip -and $_.Length -lt 2MB } |
    Where-Object {
        $_.Extension -match '^\.(md|java|py|ts|vue|js|yml|yaml|xml|json|ps1|cmd|sql|html|toml|txt)$' -or
        $_.Name -eq '.env.example' -or $_.Name -like '.env.*'
    }

foreach ($file in $files) {
    # Never scan real local .env (contains live keys by design; not tracked).
    if ($file.Name -eq '.env') { continue }
    # This scanner itself embeds pattern literals.
    if ($file.Name -eq 'check-secrets.ps1') { continue }
    if ($file.FullName -match 'package-lock|pnpm-lock|\.min\.(js|css)') { continue }

    $rel = $file.FullName.Substring($root.Length).TrimStart('\', '/')

    $hits = Select-String -Path $file.FullName -Pattern $patterns -ErrorAction SilentlyContinue
    foreach ($h in ($hits | Select-Object -First 5)) {
        Add-Hit $rel $h.Line $h.LineNumber
    }

    $entropyHits = Select-String -Path $file.FullName -Pattern $entropyPattern -ErrorAction SilentlyContinue
    foreach ($h in ($entropyHits | Select-Object -First 5)) {
        $value = [string]$h.Matches[0].Groups[1].Value
        $isPlaceholder = $false
        foreach ($marker in $placeholderMarkers) {
            if ($value.ToLower().Contains($marker)) { $isPlaceholder = $true; break }
        }
        if (-not $isPlaceholder) { Add-Hit $rel $h.Line $h.LineNumber }
    }

    $assignHits = Select-String -Path $file.FullName -Pattern $assignPattern -ErrorAction SilentlyContinue
    foreach ($h in ($assignHits | Select-Object -First 5)) {
        $value = [string]$h.Matches[0].Groups[1].Value
        $isPlaceholder = $false
        foreach ($marker in $placeholderMarkers) {
            if ($value.ToLower().Contains($marker)) { $isPlaceholder = $true; break }
        }
        if (-not $isPlaceholder) { Add-Hit $rel $h.Line $h.LineNumber }
    }
}

if ($found.Count -gt 0) {
    Write-Host ""
    Write-Host ("Found {0} file(s) with suspicious secrets. Rotate real keys and remove them from trackable files." -f $found.Count) -ForegroundColor Red
    exit 1
}

Write-Host "OK: no suspicious secrets in scannable project sources." -ForegroundColor Green
exit 0
