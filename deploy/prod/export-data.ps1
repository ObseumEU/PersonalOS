# Export everything PersonalOS knows on this PC for the move to the server.
# Stops the agent workers, the deployer and the API first so nothing writes,
# then saves into data\migration-<timestamp>\:
#   pos-data.tgz     the whole data volume (SQLite database, files, agent instructions and memory)
#   agent-work.tgz   the agents' work folders (without the Dev agent's git worktree)
#   counts.json      row counts to compare after the import
#   env              the .env with all keys (never printed; the folder stays on this PC and the server)
#   codex-home.tgz   only with -IncludeCodexLogin: the Codex CLI login (~\.codex\auth.json, config.toml)
#   SHA256SUMS
# The API is started again at the end unless -KeepStopped.
param([switch]$IncludeCodexLogin, [switch]$KeepStopped)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$out = Join-Path $root "data\migration-$stamp"
New-Item -ItemType Directory -Force $out | Out-Null

& (Join-Path $root "ops\stop-agents.ps1") | Out-Null
docker compose stop api | Out-Null

$counts = @'
import json, sqlite3
c = sqlite3.connect("/data/personalos.db")
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
print(json.dumps({t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}))
'@
$counts | docker compose run --rm --no-deps -T api python - | Set-Content -Encoding utf8 (Join-Path $out "counts.json")
docker compose run --rm --no-deps -v "${out}:/out" api tar czf /out/pos-data.tgz -C /data .

$work = Join-Path $root "data\agent-work"
tar czf (Join-Path $out "agent-work.tgz") -C $work --exclude "dev-agent/PersonalOS" .
Copy-Item (Join-Path $root ".env") (Join-Path $out "env")
if ($IncludeCodexLogin) {
    tar czf (Join-Path $out "codex-home.tgz") -C (Join-Path $env:USERPROFILE ".codex") auth.json config.toml
}
Get-ChildItem $out -File | Where-Object Name -ne "SHA256SUMS" | ForEach-Object {
    "$((Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower())  $($_.Name)"
} | Set-Content -Encoding ascii (Join-Path $out "SHA256SUMS")

if (-not $KeepStopped) { docker compose start api | Out-Null }
Write-Output "exported to $out"
Get-Content (Join-Path $out "counts.json")
