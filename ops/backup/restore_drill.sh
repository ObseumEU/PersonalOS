#!/usr/bin/env bash
# Restore drill: restores the newest backup into a temp dir and throwaway Postgres containers, then checks it.
#   - SHA256SUMS of the whole backup
#   - every SQLite copy: decompress, integrity_check (quick_check for >300 MB), row counts == the manifest
#   - every pg_dump: pg_restore into a fresh container of the same image, row counts vs the counts taken at
#     backup time (≤ 1 % or 5 rows apart: they are read right after the dump, while the app keeps writing)
#   - the file tarballs list cleanly; secrets.tar.zst.gpg is encrypted to the backup key (not decryptable here)
# Result: $STORE/drill/<stamp>.json, metric pos_backup_drill_last_success_timestamp_seconds. Monthly by cron.
set -Eeuo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd)"
STORE="${POS_BACKUP_STORE:-/opt/server/backups/personalos}"
SRC="${1:-$(ls -1d "$STORE"/daily/*/ | grep -v partial | sort | tail -1)}"
SRC="${SRC%/}"
TMP="$(mktemp -d "$STORE/drill-tmp.XXXXXX")"
PGS=()
cleanup() { for c in "${PGS[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done; rm -rf "$TMP"; }
trap cleanup EXIT
mkdir -p "$STORE/drill"
REPORT="$STORE/drill/$(date -u +%Y%m%dT%H%M%SZ).json"
log() { echo "$(date -u +%H:%M:%S) $*"; logger -t pos-backup-drill "$*"; }
problems=()

log "drill on $SRC"
cp -a "$SRC/." "$TMP/"
( cd "$TMP" && sha256sum --quiet -c SHA256SUMS ) || problems+=("checksums")

# SQLite
find "$TMP/sqlite" -name '*.db.zst' -print0 | xargs -0 -r -n1 zstd -q -d --rm
mapfile -t DBS < <(cd "$TMP/sqlite" && find . -name '*.db' | sed 's|^\./||' | sort)
HERE_DIR="$HERE" python3 - "$TMP/sqlite" "${DBS[@]}" > "$TMP/sqlite-drill.json" <<'PY' || problems+=("sqlite")
import json, os, sys, importlib.util
here = os.environ.get("HERE_DIR")
root, dbs = sys.argv[1], sys.argv[2:]
spec = importlib.util.spec_from_file_location("sb", os.path.join(here, "sqlite_backup.py"))
sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
manifest = json.load(open(os.path.join(root, "sqlite-manifest.json")))
out, bad = {}, []
for rel in dbs:
    label = rel[:-3]
    got = sb.check(os.path.join(root, rel))
    want = manifest.get(label, {}).get("tables")
    diff = {t: [want.get(t) if want else None, n] for t, n in got["tables"].items() if not want or want.get(t) != n}
    out[label] = {"ok": got["ok"] and not diff, "check": got["check"], "rows": sum(v for v in got["tables"].values() if isinstance(v, int)), "diff": diff}
    if not out[label]["ok"]:
        bad.append(label)
missing = sorted(set(manifest) - {d[:-3] for d in dbs})
print(json.dumps({"databases": out, "missing": missing}, indent=1))
sys.exit(1 if bad or missing else 0)
PY
log "sqlite: $(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(', '.join(f'{k} {v[\"rows\"]} rows {\"ok\" if v[\"ok\"] else \"BAD\"}' for k,v in d['databases'].items()))" "$TMP/sqlite-drill.json")"

# Postgres
echo "{}" > "$TMP/pg-drill.json"
for dump in "$TMP"/pg/*.dump; do
  name="$(basename "$dump" .dump)"
  image="$(cat "$TMP/pg/$name.image")"
  c="pos-drill-$name-$$"
  PGS+=("$c")
  docker run -d --rm --name "$c" --memory 768m --cpus 1 -e POSTGRES_PASSWORD="$(head -c 18 /dev/urandom | base64)" \
    -e POSTGRES_USER=drill -e POSTGRES_DB=drill "$image" >/dev/null
  for _ in $(seq 60); do docker exec "$c" pg_isready -U drill -d drill >/dev/null 2>&1 && break; sleep 2; done
  sleep 3
  docker exec -i "$c" pg_restore -U drill -d drill --no-owner --no-privileges --exit-on-error < "$dump" \
    > "$TMP/pg/$name.restore.log" 2>&1 || { problems+=("pg_restore $name"); log "pg $name: restore FAILED"; docker rm -f "$c" >/dev/null; continue; }
  COUNTS_SQL="select table_schema||'.'||table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text from information_schema.tables where table_type='BASE TABLE' and table_schema not in ('pg_catalog','information_schema') order by 1"
  docker exec "$c" psql -U drill -d drill -AtF '	' -c "$COUNTS_SQL" > "$TMP/pg/$name.restored.tsv"
  docker rm -f "$c" >/dev/null
  python3 - "$TMP/pg/$name.counts.tsv" "$TMP/pg/$name.restored.tsv" "$name" "$TMP/pg-drill.json" <<'PY' || problems+=("pg counts $name")
import json, sys
read = lambda p: {l.split("\t")[0]: int(l.split("\t")[1]) for l in open(p) if "\t" in l}
want, got, name, out = read(sys.argv[1]), read(sys.argv[2]), sys.argv[3], sys.argv[4]
diff = {t: [w, got.get(t)] for t, w in want.items() if got.get(t) is None or abs(got[t] - w) > max(5, w * 0.01)}
report = json.load(open(out))
report[name] = {"tables": len(got), "rows": sum(got.values()), "ok": not diff, "diff": diff}
json.dump(report, open(out, "w"), indent=1)
print(f"{name}: {len(got)} tables, {sum(got.values())} rows, {'ok' if not diff else 'DIFF ' + json.dumps(diff)[:300]}")
sys.exit(1 if diff else 0)
PY
  log "pg $name: $(python3 -c "import json,sys;v=json.load(open(sys.argv[1]))[sys.argv[2]];print(v['tables'],'tables',v['rows'],'rows','ok' if v['ok'] else 'DIFF')" "$TMP/pg-drill.json" "$name")"
done

# Files and secrets
for t in "$TMP"/files/*.tar.zst "$TMP"/config/config.tar.zst; do
  zstd -q -dc "$t" | tar -tf - > /dev/null || problems+=("tar $(basename "$t")")
done
packets="$(gpg --batch --list-packets "$TMP/config/secrets.tar.zst.gpg" 2>/dev/null || true)"  # fails: no secret key here
grep -qiE "keyid (1554BEF8B0696F2E|A3590CF5A4A4BC2F)" <<<"$packets" \
  || problems+=("secrets not encrypted to the backup key")

python3 - "$REPORT" "$SRC" "$TMP/sqlite-drill.json" "$TMP/pg-drill.json" "${problems[*]:-}" <<'PY'
import json, sys, time
report, src, sq, pg, problems = sys.argv[1:6]
json.dump({"backup": src, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": not problems.strip(),
           "problems": problems.split() if problems.strip() else [], "sqlite": json.load(open(sq)),
           "postgres": json.load(open(pg))}, open(report, "w"), indent=1)
PY
f="$STORE/metrics/pos_backup.prom"
if [ ${#problems[@]} = 0 ]; then
  { grep -v '^pos_backup_drill_last_success_timestamp_seconds ' "$f" || true; echo "pos_backup_drill_last_success_timestamp_seconds $(date +%s)"; } | sort > "$f.tmp" && mv "$f.tmp" "$f"
  log "drill OK ($REPORT)"
else
  log "drill FAILED: ${problems[*]} ($REPORT)"; exit 1
fi
