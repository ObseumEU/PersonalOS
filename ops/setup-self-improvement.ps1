# One-time setup for self-improvement on this PC (AGENTS-SPEC 6):
# - a git worktree for the Dev agent on branch agent/dev (it commits there, never pushes)
# - a deploy worktree where the deployer merges agent/dev into origin/main,
#   runs the tests and a staging stack (port 8091), and only then pushes to main
# Both get their own Python venv and web node_modules so tests run on their code.

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
$dev = Join-Path $root "data\agent-work\dev-agent\PersonalOS"
$deploy = Join-Path $root "data\deploy\PersonalOS"

git -C $root fetch origin main
if (-not (Test-Path $dev)) {
    git -C $root worktree add -B agent/dev $dev origin/main
}
if (-not (Test-Path $deploy)) {
    git -C $root worktree add --detach $deploy origin/main
}
foreach ($wt in @($dev, $deploy)) {
    $venv = Join-Path $wt "backend\.venv"
    if (-not (Test-Path $venv)) {
        & $python -m venv $venv
        & (Join-Path $venv "Scripts\python.exe") -m pip install -q -e (Join-Path $wt "backend[dev]") -e (Join-Path $wt "worker")
    }
    Push-Location (Join-Path $wt "web")
    if (-not (Test-Path "node_modules")) { npm install --silent }
    Pop-Location
}
Write-Output "Dev agent worktree: $dev (branch agent/dev)"
Write-Output "Deploy worktree:    $deploy"
