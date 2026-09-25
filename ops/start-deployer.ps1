# Start the deployer in promote mode on this PC: it watches agent/dev, and for
# new commits merges them into origin/main in data\deploy\PersonalOS, runs the
# constitution check, backend tests and web build, a staging stack on port 8091
# with a health check, and only then pushes to main. Logs: data\agent-logs\deployer.log

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
$deploy = Join-Path $root "data\deploy\PersonalOS"
$logs = Join-Path $root "data\agent-logs"
New-Item -ItemType Directory -Force $logs | Out-Null

$vars = @{}
Get-Content (Join-Path $root ".env") | Where-Object { $_ -match '^\s*([A-Z0-9_]+)=(.*)$' } | ForEach-Object { $vars[$Matches[1]] = $Matches[2] }
$port = if ($vars["POS_PORT"]) { $vars["POS_PORT"] } else { "8080" }

$env:POS_URL = "http://localhost:$port"
$env:POS_DEPLOYER_KEY = $vars["DEPLOYER_KEY"]
$env:DEPLOY_TEST_CMD = "cd backend && .venv\Scripts\python -m pytest -q && cd ..\web && npm run build"
$env:DEPLOY_UP_CMD = "set POS_PORT=8091&& docker compose -p pos-staging up -d --build api web"
$env:DEPLOY_HEALTH_URL = "http://localhost:8091/api/health"
$pidFile = Join-Path $logs "deployer.pid"
if ((Test-Path $pidFile) -and (Get-Process -Id ([int](Get-Content $pidFile | Select-Object -First 1)) -ErrorAction SilentlyContinue)) {
    Write-Output "deployer already running (pid $(Get-Content $pidFile)); stop it first to restart"
    exit 0
}
$env:POS_CHILD_PIDFILE = Join-Path $logs "deployer.child.pid"
$log = Join-Path $logs "deployer.log"
$p = Start-Process -FilePath $python -ArgumentList "-m", "pos.selfdeploy", "--repo", $deploy, "--promote-from", "agent/dev", "--watch", "60" `
    -WorkingDirectory $deploy -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $logs "deployer.pid") -Value $p.Id
Remove-Item Env:POS_DEPLOYER_KEY -ErrorAction SilentlyContinue
Write-Output "deployer started (pid $($p.Id)), log $log"
