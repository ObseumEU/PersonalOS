# Stop the agent workers and the deployer started by ops\start-agents.ps1 and
# ops\start-deployer.ps1. Each writes <name>.pid (the venv launcher) and
# <name>.child.pid (the real interpreter): both are stopped. Stop-Process is used
# because taskkill /T depends on WMI, which is broken on some PCs.
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "datagent-logs"
Get-ChildItem $logs -Filter *.pid -ErrorAction SilentlyContinue | Sort-Object Name -Descending | ForEach-Object {
    $procId = [int](Get-Content $_.FullName | Select-Object -First 1)
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        Write-Output "stopped $($_.BaseName) (pid $procId)"
    }
    Remove-Item $_.FullName
}
