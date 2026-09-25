#!/usr/bin/env bash
# Import a migration folder made by export-data.ps1 into this checkout's stack.
#   deploy/prod/import-data.sh ../migration-<timestamp>
# Refuses to overwrite a data volume that already has a database, unless FORCE=1
# (then the old volume content is kept as a .tgz next to the migration folder).
set -euo pipefail
src="$(cd "$1" && pwd)"
cd "$(dirname "$0")/../.."
compose=(docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml)

# --ignore-missing: codex-home.tgz is optional (the Codex login can be done on the server instead).
(cd "$src" && sha256sum -c --ignore-missing SHA256SUMS)
[ -f .env ] || { install -m 600 "$src/env" .env; echo "installed .env from the migration"; }

project="$(grep -E '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2 || true)"
volume="${project:-$(basename "$PWD")}_pos-data"
docker volume create "$volume" >/dev/null
if docker run --rm -v "$volume:/data" alpine test -f /data/personalos.db; then
  [ "${FORCE:-0}" = 1 ] || { echo "$volume already has a database; FORCE=1 to replace it"; exit 1; }
  docker run --rm -v "$volume:/data" -v "$src/..:/out" alpine tar czf "/out/pos-data-before-import-$(date +%s).tgz" -C /data .
fi
"${compose[@]}" stop api 2>/dev/null || true
docker run --rm -v "$volume:/data" -v "$src:/in:ro" alpine sh -c 'rm -rf /data/* && tar xzf /in/pos-data.tgz -C /data'

# Agent work folders go into the agents' volumes.
for agent in assistant mail-agent community-agent; do
  vol="${project:-$(basename "$PWD")}_${agent}-work"
  docker volume create "$vol" >/dev/null
  docker run --rm -v "$vol:/work" -v "$src:/in:ro" alpine sh -c \
    "mkdir -p /tmp/w && tar xzf /in/agent-work.tgz -C /tmp/w && [ -d /tmp/w/$agent ] && cp -a /tmp/w/$agent/. /work/ || true"
done

"${compose[@]}" up -d --build api web
sleep 5
"${compose[@]}" exec -T api python - <<'PY' > "$src/counts-after.json"
import json, sqlite3
c = sqlite3.connect("/data/personalos.db")
tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
print(json.dumps({t: c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}))
PY
python3 - "$src/counts.json" "$src/counts-after.json" <<'PY'
import json, sys
before = json.load(open(sys.argv[1], encoding="utf-8-sig"))
after = json.load(open(sys.argv[2]))
# The app may add rows on start (audit, jobs); nothing may be missing.
missing = {t: (n, after.get(t, 0)) for t, n in before.items() if after.get(t, 0) < n}
print("counts OK" if not missing else f"MISSING ROWS: {missing}")
sys.exit(1 if missing else 0)
PY
