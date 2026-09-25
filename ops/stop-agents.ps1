# Stop the agent workers started by ops\start-agents.ps1 (and their Claude runs).
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "data\agent-logs"
Get-ChildItem $logs -Filter *.pid -ErrorAction SilentlyContinue | ForEach-Object {
    $procId = [int](Get-Content $_.FullName | Select-Object -First 1)
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
        & taskkill /PID $procId /T /F 2>$null | Out-Null
        Write-Output "stopped $($_.BaseName) (pid $procId)"
    } else { Write-Output "$($_.BaseName) was not running" }
    Remove-Item $_.FullName
}
