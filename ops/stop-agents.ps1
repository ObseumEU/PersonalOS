# Stop the agent workers started by ops\start-agents.ps1 (and their Claude runs).
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "data\agent-logs"
Get-ChildItem $logs -Filter *.pid -ErrorAction SilentlyContinue | ForEach-Object {
    $procId = Get-Content $_.FullName
    try {
        & taskkill /PID $procId /T /F | Out-Null
        Write-Output "stopped $($_.BaseName) (pid $procId)"
    } catch { Write-Output "$($_.BaseName) was not running" }
    Remove-Item $_.FullName
}
