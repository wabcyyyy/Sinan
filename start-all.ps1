# Sinan one-click launcher
# Starts redis(6380), backend-python(8000, serves /api + /api/agent), frontend-react(5173).
# The Spring module (travel-backend-java/) was deleted after the migration; see ARCHIVED.md to recover it from git history.
# Services already running are skipped. Background services run without extra console windows.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less files as ANSI and
# non-ASCII comments break parsing (start-all.cmd calls powershell.exe, i.e. 5.1).

$root = Split-Path -Parent $MyInvocation.MyCommand.Path

# Use a project-local uv cache; a broken global cache path would block the Agent from starting.
$env:UV_CACHE_DIR = Join-Path $root ".uv-cache"

# Load repo-root .env into this process; child services inherit it.
# (module-local .env files / real environment variables can still override.)
$rootEnv = Join-Path $root ".env"
if (Test-Path $rootEnv) {
    Get-Content $rootEnv | ForEach-Object {
        if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)\s*$') {
            Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
        }
    }
    Write-Host "[env   ] loaded $rootEnv"
}

# Safety check: the FastAPI backend fails fast when JWT_SECRET is missing/short (it signs sessions).
if (-not $env:JWT_SECRET -or $env:JWT_SECRET.Length -lt 32) {
    Write-Warning "JWT_SECRET is missing or too short (need >= 32 chars). Put it in repo-root .env; see travel-agent-python/.env.example"
}
if (-not $env:AGENT_INTERNAL_TOKEN) {
    Write-Warning "AGENT_INTERNAL_TOKEN is not set: HTTP calls to /api/agent/* will be unauthenticated (acceptable only for local demos on loopback)"
}

function Test-Port([int]$p) {
    return [bool](Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue)
}

if (-not (Test-Path (Join-Path $root "logs"))) { New-Item -ItemType Directory -Path (Join-Path $root "logs") | Out-Null }

function Start-ServiceWindow([string]$title, [string]$workdir, [string]$cmdline) {
    # Output goes to logs\<title>.log; watch it with: Get-Content logs\<title>.log -Wait -Tail 100
    $logFile = Join-Path $root "logs\$title.log"
    $args_ = "/k title $title && cd /d `"$workdir`" && $cmdline > `"$logFile`" 2>&1"
    Start-Process -FilePath "cmd.exe" -ArgumentList $args_ -WindowStyle Hidden
}

Write-Host "=== Sinan launcher ==="

# --- redis (cache, optional but recommended) ---
if (-not (Test-Port 6380)) {
    # Resolve redis-server.exe without any hardcoded path: REDIS_SERVER_EXE
    # (repo-root .env works, it is loaded above) wins, then PATH.
    $redisExe = $env:REDIS_SERVER_EXE
    if (-not $redisExe) {
        $redisCmd = Get-Command redis-server -ErrorAction SilentlyContinue
        if ($redisCmd) { $redisExe = $redisCmd.Source }
    }
    if ($redisExe -and (Test-Path $redisExe)) {
        Start-Process -FilePath $redisExe -ArgumentList "--bind 127.0.0.1 --protected-mode yes --port 6380" -WindowStyle Minimized
        Write-Host "[start] redis      :6380"
    } else {
        Write-Warning "redis-server.exe not found (checked REDIS_SERVER_EXE and PATH). Set REDIS_SERVER_EXE in repo-root .env or put redis-server on PATH; cache-dependent APIs will fail without redis on :6380"
    }
} else {
    Write-Host "[skip ] redis      :6380 already running"
}

# --- backend (FastAPI on :8000) ---
if (-not (Test-Port 8000)) {
    # --no-sync: when the local venv drifts from uv.lock (e.g. torch/embedding deps bumped
    # without uv sync yet), bare "uv run" does an implicit sync that can stall on the pytorch
    # CPU index. Start from the installed environment here; explicit sync is done by
    # "uv sync" (local) and CI ("uv sync --frozen").
    Start-ServiceWindow "agent-python" "$root\travel-agent-python" `
        "uv run --no-sync python main.py"
    Write-Host "[start] agent-python :8000"
} else {
    Write-Host "[skip ] agent-python :8000 already running"
}

# --- frontend react :5173 ---
if (-not (Test-Port 5173)) {
    Start-ServiceWindow "frontend-react" "$root\travel-frontend-react" `
        "npm run dev"
    Write-Host "[start] frontend-react :5173"
} else {
    Write-Host "[skip ] frontend-react :5173 already running"
}

# --- health check ---
Write-Host ""
Write-Host "Waiting for services to become healthy..."
$targets = @(
    # FastAPI serves both surfaces on :8000
    @{ Name = "backend-python :8000"; Url = "http://127.0.0.1:8000/api/test/hello" },
    @{ Name = "agent-python :8000"; Url = "http://127.0.0.1:8000/api/agent/hello" },
    @{ Name = "frontend-react :5173"; Url = "http://localhost:5173/" }
)
$deadline = (Get-Date).AddSeconds(150)
$pending = @($targets)
while ($pending.Count -gt 0 -and (Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    foreach ($t in @($pending)) {
        try {
            Invoke-WebRequest -Uri $t.Url -UseBasicParsing -TimeoutSec 2 | Out-Null
            Write-Host "[ ok   ] $($t.Name)"
            $pending = @($pending | Where-Object { $_ -ne $t })
        } catch { }
    }
}
foreach ($t in $pending) { Write-Host "[TIMEOUT] $($t.Name) not responding yet - check its window for errors" }

Write-Host ""
Write-Host "Done. Web UI: http://localhost:5173  (register an account on first use)"


