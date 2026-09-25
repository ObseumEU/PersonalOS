# Start the agent workers directly on this PC (Windows), using the Claude CLI
# that is logged in here. Each worker runs in the background and logs to
# data\agent-logs\<name>.log. Stop them with ops\stop-agents.ps1.
#
# Needs: the stack running (docker compose up -d), keys in .env
# (DEV_AGENT_KEY, MAIL_AGENT_KEY, COMMUNITY_AGENT_KEY), backend\.venv with the
# worker installed (pip install -e worker).

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
$envFile = Join-Path $root ".env"
$logs = Join-Path $root "data\agent-logs"
New-Item -ItemType Directory -Force $logs | Out-Null

$vars = @{}
Get-Content $envFile | Where-Object { $_ -match '^\s*([A-Z0-9_]+)=(.*)$' } | ForEach-Object {
    $vars[$Matches[1]] = $Matches[2]
}
$port = if ($vars["POS_PORT"]) { $vars["POS_PORT"] } else { "8080" }

$agents = @(
    @{ Name = "dev-agent"; Key = "DEV_AGENT_KEY" },
    @{ Name = "mail-agent"; Key = "MAIL_AGENT_KEY" },
    @{ Name = "community-agent"; Key = "COMMUNITY_AGENT_KEY" }
)

foreach ($a in $agents) {
    $key = $vars[$a.Key]
    if (-not $key) { Write-Warning "$($a.Key) missing in .env, skipping $($a.Name)"; continue }
    $work = Join-Path $root "data\agent-work\$($a.Name)"
    New-Item -ItemType Directory -Force $work | Out-Null
    $env:POS_URL = "http://localhost:$port"
    $env:POS_AGENT_KEY = $key
    $env:WORKER_WORKDIR = $work
    $env:WORKER_POLL = "60"
    $log = Join-Path $logs "$($a.Name).log"
    $p = Start-Process -FilePath $python -ArgumentList "-m", "pos_worker" -WorkingDirectory $work `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $logs "$($a.Name).pid") -Value $p.Id
    Write-Output "$($a.Name) started (pid $($p.Id)), log $log"
}
Remove-Item Env:POS_AGENT_KEY -ErrorAction SilentlyContinue
