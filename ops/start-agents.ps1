# Start the agent workers directly on this PC (Windows), using the Claude CLI
# that is logged in here. Each worker runs in the background and logs to
# data\agent-logs\<name>.log. Stop them with ops\stop-agents.ps1.
#
# Needs: the stack running (docker compose up -d), keys in .env
# (ASSISTANT_AGENT_KEY, DEV_AGENT_KEY, MAIL_AGENT_KEY, COMMUNITY_AGENT_KEY), backend\.venv with the
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

# The Dev agent works in its own git worktree (branch agent/dev, see
# ops/setup-self-improvement.ps1) with git (no push), tests and the web build.
# The deployer promotes its commits to main after the checks.
$devRepo = Join-Path $root "data\agent-work\dev-agent\PersonalOS"
# Token budget (agents/dev-agent/INSTRUCTIONS.md): the agent sees only the six
# pos tools it uses (every tool definition costs tokens on every turn), and a
# run that goes far past its size is stopped and handed back.
$devPos = "get_task", "report_progress", "complete_task", "ack_message", "request_approval", "create_task"
$devHidden = "ask_agent assign_task capture check_inbox claim_task create_agent emit_event freeze get_agent_status " +
    "heartbeat hr_admit_agent hr_overview hr_review hr_weekly_report list_active_runs list_routes list_tasks " +
    "request_outbound schedule_create schedule_delete schedule_list schedule_pause schedule_resume " +
    "schedule_run_now schedule_update send_message update_task"
$devTools = (@($devPos | ForEach-Object { "mcp__pos__$_" }) + @(
    "Read", "Glob", "Grep", "Write", "Edit",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)", "Bash(git add:*)",
    "Bash(git commit:*)", "Bash(git fetch origin:*)", "Bash(git merge origin/main:*)",
    "Bash(backend/.venv/Scripts/python -m pytest:*)", "Bash(npm run build:*)"
)) -join "|"

$agents = @(
    @{ Name = "assistant"; Key = "ASSISTANT_AGENT_KEY" },
    @{ Name = "dev-agent"; Key = "DEV_AGENT_KEY"; Work = $devRepo; Tools = $devTools; Builtin = "Bash,Read,Edit,Write,Glob,Grep"
       Env = @{
           WORKER_CODEX_CONFIG = 'mcp_servers.pos.enabled_tools=["' + ($devPos -join '","') + '"]||model_reasoning_effort="medium"'
           WORKER_CLAUDE_DISALLOWED = (($devHidden -split " ") | ForEach-Object { "mcp__pos__$_" }) -join " "
           WORKER_CLAUDE_EFFORT = "medium"
           WORKER_CLAUDE_MAX_USD = "6"
           WORKER_MAX_STEPS = "90"
       } },
    @{ Name = "mail-agent"; Key = "MAIL_AGENT_KEY" },
    @{ Name = "community-agent"; Key = "COMMUNITY_AGENT_KEY" }
)

foreach ($a in $agents) {
    $pidFile = Join-Path $logs "$($a.Name).pid"
    if (Test-Path $pidFile) {
        $old = [int](Get-Content $pidFile | Select-Object -First 1)
        if (Get-Process -Id $old -ErrorAction SilentlyContinue) {
            Write-Output "$($a.Name) already running (pid $old); run ops\stop-agents.ps1 first to restart"
            continue
        }
    }
    $key = $vars[$a.Key]
    if (-not $key) { Write-Warning "$($a.Key) missing in .env, skipping $($a.Name)"; continue }
    $work = if ($a.Work) { $a.Work } else { Join-Path $root "data\agent-work\$($a.Name)" }
    New-Item -ItemType Directory -Force $work | Out-Null
    $env:POS_URL = "http://localhost:$port"
    $env:POS_AGENT_KEY = $key
    $env:WORKER_WORKDIR = $work
    $env:WORKER_POLL = "60"
    $env:WORKER_CLAUDE_TOOLS = if ($a.Tools) { $a.Tools } else { "" }
    $env:WORKER_CLAUDE_BUILTIN = if ($a.Builtin) { $a.Builtin } else { "" }
    if (-not $a.Tools) { Remove-Item Env:WORKER_CLAUDE_TOOLS -ErrorAction SilentlyContinue }
    # Per-agent settings; cleared for agents that do not set them.
    foreach ($n in "WORKER_CODEX_CONFIG", "WORKER_CLAUDE_DISALLOWED", "WORKER_CLAUDE_EFFORT", "WORKER_CLAUDE_MAX_USD", "WORKER_MAX_STEPS") {
        Remove-Item "Env:$n" -ErrorAction SilentlyContinue
    }
    if ($a.Env) { foreach ($k in $a.Env.Keys) { Set-Item "Env:$k" $a.Env[$k] } }
    $env:POS_CHILD_PIDFILE = Join-Path $logs "$($a.Name).child.pid"
    $log = Join-Path $logs "$($a.Name).log"
    $p = Start-Process -FilePath $python -ArgumentList "-m", "pos_worker" -WorkingDirectory $work `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $logs "$($a.Name).pid") -Value $p.Id
    Write-Output "$($a.Name) started (pid $($p.Id)), log $log"
}
Remove-Item Env:POS_AGENT_KEY -ErrorAction SilentlyContinue

# The API container cannot reach the LAN on Docker Desktop; this forwards
# 127.0.0.1:8097 to knowlage (POS_KNOWLAGE_URL=http://host.docker.internal:8097).
$bridgePid = Join-Path $logs "knowlage-bridge.pid"
if (-not ((Test-Path $bridgePid) -and (Get-Process -Id ([int](Get-Content $bridgePid)) -ErrorAction SilentlyContinue))) {
    $b = Start-Process -FilePath $python -ArgumentList (Join-Path $root "ops\knowlage-bridge.py") -WindowStyle Hidden -PassThru
    Set-Content -Path $bridgePid -Value $b.Id
    Write-Output "knowlage bridge started (pid $($b.Id)) on 127.0.0.1:8097"
}
