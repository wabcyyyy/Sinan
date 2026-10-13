# Personal iteration entry. Full verification is explicit; CI retains all gates.
param(
    [switch]$Full,
    [switch]$Plan,
    [string[]]$Files,
    [string[]]$Tests,
    [string]$Base,
    [switch]$NoCache,
    [string]$TempRoot
)
$ErrorActionPreference = 'Stop'
$rules = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'validation-rules.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$python = Join-Path (Join-Path $PSScriptRoot $rules.python_cwd) '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw "Project environment missing: $python" }
$cliArgs = @((Join-Path $PSScriptRoot 'scripts\ai_validate.py'))
if (-not $Plan) { $cliArgs += '--run' }
if ($Full) { $cliArgs += @('--level', '3') }
if ($Files) { $cliArgs += '--files'; $cliArgs += $Files }
if ($Tests) { $cliArgs += '--tests'; $cliArgs += $Tests }
if ($Base) { $cliArgs += @('--base', $Base) }
if ($NoCache) { $cliArgs += '--no-cache' }
if ($TempRoot) { $cliArgs += @('--temp-root', $TempRoot) }
& $python @cliArgs
exit $LASTEXITCODE
