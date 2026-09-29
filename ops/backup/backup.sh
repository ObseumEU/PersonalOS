#!/usr/bin/env bash
# Nightly backup of PersonalOS and its neighbours on svr03 (cron as drosko, see ops/backup/README.md).
#
#   ops/backup/backup.sh            # full run: dump, verify, retention, ad-hoc cleanup, .186, Google Drive
#   SKIP_GDRIVE=1 ops/backup/backup.sh
#
# Store: $STORE/daily/<stamp>/ (14 kept), $STORE/weekly/<stamp>/ (hard links of Sunday's daily, 8 kept),
# $STORE/archive/ (ad-hoc copies moved off the data volume), $STORE/metrics/pos_backup.prom (read by obs-alloy).
set -Eeuo pipefail
umask 077

HERE="$(cd "$(dirname "$0")" && pwd)"
STORE="${POS_BACKUP_STORE:-/opt/server/backups/personalos}"
KEEP_DAILY="${KEEP_DAILY:-14}"
KEEP_WEEKLY="${KEEP_WEEKLY:-8}"
REMOTE="${POS_BACKUP_REMOTE:-agent@192.168.1.186:}"   # rrsync-restricted to /srv/backups/svr03 on .186
SSH_KEY="${POS_BACKUP_SSH_KEY:-$HOME/.ssh/pos_backup_ed25519}"
PY_IMAGE="${PY_IMAGE:-python:3.12-slim}"
GPG_RECIPIENT="B553517B011A040CA0DBD068D168BA163D99F61A"
ENV_FILE="/opt/server/personalos/app/.env"

# what is backed up (name=volume or path)
SQLITE_VOLUMES=(personalos_pos-data personalos_sentinel-state kb_kb_data)
SQLITE_SPECS=(personalos=/v/personalos_pos-data/personalos.db
              sentinel=/v/personalos_sentinel-state/sentinel.db
              knowlage=/v/kb_kb_data/kb.sqlite
              knowlage-gdrive=/v/kb_kb_data/gdrive/*.sqlite)
PG_CONTAINERS=(nexus-process-pilot-postgres-1 litellm-postgres langfuse-postgres)
FILE_VOLUMES=(personalos_pos-data nexus-process-pilot_knowledge-objects nexus-process-pilot_team-files)
CONFIG_FILES=(/opt/server/personalos/app/docker-compose.yml
              /opt/server/personalos/app/deploy/prod/docker-compose.prod.yml
              /opt/server/knowlage/docker-compose.yml /opt/server/knowlage/deploy/prod/docker-compose.prod.yml
              /opt/server/knowlage/deploy/Caddyfile
              /opt/server/nexus-process-pilot/app/docker-compose.yml
              /opt/server/nexus-process-pilot/app/docker-compose.prod.yml
              /opt/server/litellm/docker-compose.yaml /opt/server/litellm/config.yaml
              /opt/server/langfuse/docker-compose.yaml
              /opt/server/docker-migration/caddy/Caddyfile /opt/server/docker-migration/caddy/docker-compose.yml
              /opt/server/observability/docker-compose.yml /opt/server/observability/config.alloy)
SECRET_FILES=(/opt/server/personalos/app/.env /opt/server/knowlage/.env /opt/server/nexus-process-pilot/app/.env
              /opt/server/litellm/.env /opt/server/langfuse/.env /opt/server/observability/secrets)

mkdir -p "$STORE"/{daily,weekly,archive,metrics,logs}
exec 9>"$STORE/.lock"
flock -n 9 || { echo "another backup is running"; exit 0; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DIR="$STORE/daily/$STAMP"
WORK="$DIR.partial"
LOG="$STORE/logs/$STAMP.log"
exec > >(tee -a "$LOG" | logger -t pos-backup) 2>&1
log() { echo "$(date -u +%H:%M:%S) $*"; }
NICE=(nice -n 19 ionice -c3)

metric() {  # metric NAME VALUE [LABELS] — rewrites one line of the textfile atomically
  local f="$STORE/metrics/pos_backup.prom" key="$1"
  [ -n "${3:-}" ] && key="$1{$3}"
  touch "$f"
  { grep -vF "$key " "$f" | grep -v '^#' || true; echo "$key $2"; } | sort > "$f.tmp"
  mv "$f.tmp" "$f"
}
fail() { log "FAILED: $*"; metric pos_backup_last_run_ok 0; metric pos_backup_last_run_timestamp_seconds "$(date +%s)"; exit 1; }
trap 'fail "line $LINENO: $BASH_COMMAND"' ERR

log "backup $STAMP into $DIR"
mkdir -p "$WORK"/{sqlite,pg,files,config}

# 1. SQLite: online backup API from a throwaway container (the volumes' files belong to root / other uids)
vols=(); for v in "${SQLITE_VOLUMES[@]}"; do vols+=(-v "$v:/v/$v"); done
"${NICE[@]}" docker run --rm --cpus 1 --memory 768m "${vols[@]}" -v "$WORK/sqlite:/out" \
  -v "$HERE/sqlite_backup.py:/b.py:ro" "$PY_IMAGE" \
  sh -c "python /b.py backup /out ${SQLITE_SPECS[*]} && chown -R $(id -u):$(id -g) /out"
find "$WORK/sqlite" -name '*.db' -print0 | xargs -0 -r -n1 "${NICE[@]}" zstd -q -T2 -10 --rm
log "sqlite done"

# 2. Postgres: pg_dump custom format (compressed) + exact row counts for the drill
COUNTS_SQL="select table_schema||'.'||table_name, (xpath('/row/c/text()', query_to_xml(format('select count(*) as c from %I.%I', table_schema, table_name), false, true, '')))[1]::text from information_schema.tables where table_type='BASE TABLE' and table_schema not in ('pg_catalog','information_schema') order by 1"
for c in "${PG_CONTAINERS[@]}"; do
  "${NICE[@]}" docker exec "$c" sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -Z 6' > "$WORK/pg/$c.dump"
  docker exec -i "$c" pg_restore --list < "$WORK/pg/$c.dump" > /dev/null
  docker exec "$c" sh -c "psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -AtF '	' -c \"$COUNTS_SQL\"" > "$WORK/pg/$c.counts.tsv"
  docker inspect -f '{{.Config.Image}}' "$c" > "$WORK/pg/$c.image"
  docker exec "$c" sh -c 'pg_dumpall -U "$POSTGRES_USER" --globals-only' > "$WORK/pg/$c.globals.sql"
  log "pg $c: $(du -h "$WORK/pg/$c.dump" | cut -f1)"
done

# 3. Files: the data volumes without the SQLite files (those are in sqlite/), tar + zstd
for v in "${FILE_VOLUMES[@]}"; do
  "${NICE[@]}" docker run --rm --cpus 1 -v "$v:/v:ro" "$PY_IMAGE" \
    tar -C /v --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.db.*' -cf - . \
    | "${NICE[@]}" zstd -q -T2 -10 > "$WORK/files/$v.tar.zst"
done
log "files done"

# 4. Config (plain) and secrets (.env, pg roles; encrypted to the owner's backup key, private key not on svr03)
existing() { for f in "$@"; do [ -e "$f" ] && echo "$f"; done; return 0; }
existing "${CONFIG_FILES[@]}" | tar -cf - -P -T - | zstd -q > "$WORK/config/config.tar.zst"
GNUPGHOME="$(mktemp -d)"; export GNUPGHOME
gpg -q --batch --import "$HERE/backup-pubkey.asc"
{ existing "${SECRET_FILES[@]}"; ls "$WORK"/pg/*.globals.sql; } | tar -cf - -P -T - 2>/dev/null | zstd -q \
  | gpg -q --batch --trust-model always --encrypt -r "$GPG_RECIPIENT" -o "$WORK/config/secrets.tar.zst.gpg"
rm -f "$WORK"/pg/*.globals.sql
log "config done"

# 5. Manifest, checksums, publish
( cd "$WORK" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
echo "{\"stamp\": \"$STAMP\", \"host\": \"$(hostname)\", \"bytes\": $(du -sb "$WORK" | cut -f1)}" > "$WORK/MANIFEST.json"
mv "$WORK" "$DIR"
# the sentinel (uid 10001, docs/SENTINEL.md "Backups") stats the newest file: directories readable, files stay 600
chmod 755 "$STORE/daily"; find "$DIR" -type d -exec chmod 755 {} +
SIZE="$(du -sb "$DIR" | cut -f1)"
KIND=daily
if [ "$(date -u +%u)" = 7 ] || [ -z "$(find "$STORE/weekly" -mindepth 1 -maxdepth 1 -type d -mtime -6 | head -1)" ]; then
  cp -al "$DIR" "$STORE/weekly/$STAMP"; KIND=weekly
fi
log "local backup ok: $(du -sh "$DIR" | cut -f1) ($KIND)"
metric pos_backup_last_success_timestamp_seconds "$(date +%s)" 'target="local"'
metric pos_backup_size_bytes "$SIZE"

# 6. Retention (newest first; partial leftovers of a crashed run go too)
{ ls -1d "$STORE"/daily/*/ 2>/dev/null || true; } | sort -r | tail -n +$((KEEP_DAILY + 1)) | xargs -r rm -rf
{ ls -1d "$STORE"/weekly/*/ 2>/dev/null || true; } | sort -r | tail -n +$((KEEP_WEEKLY + 1)) | xargs -r rm -rf
find "$STORE/daily" -maxdepth 1 -name '*.partial' -mmin +360 -exec rm -rf {} +
find "$STORE/logs" -type f -mtime +60 -delete

# 7. Ad-hoc copies on the PersonalOS volume: keep the newest 3 and the last 48 h, archive the rest
docker run --rm --cpus 0.5 -v personalos_pos-data:/data -v "$STORE/archive:/archive" \
  -v "$HERE/sqlite_backup.py:/b.py:ro" "$PY_IMAGE" \
  sh -c "python /b.py adhoc /data /archive/pos-data-adhoc && chown -R $(id -u):$(id -g) /archive"
find "$STORE/archive" -name '*.db' -print0 | xargs -0 -r -n1 zstd -q -T1 -10 --rm

# 8. Off-host: .186 (mirror of daily/weekly/archive; the key is restricted with rrsync on .186)
trap - ERR
rc=0
"${NICE[@]}" rsync -aH --delete --partial -e "ssh -i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=20" \
  "$STORE/daily" "$STORE/weekly" "$STORE/archive" "$REMOTE" || rc=$?
if [ $rc = 0 ]; then
  log "copied to .186"; metric pos_backup_last_success_timestamp_seconds "$(date +%s)" 'target="agent186"'
else
  log "copy to .186 FAILED (rsync $rc)"
fi

# 9. Off-host: Google Drive, the whole backup as one file encrypted to the owner's key
grc=0
if [ -z "${SKIP_GDRIVE:-}" ]; then
  BUNDLE="$STORE/pos-backup-$STAMP-$KIND.tar.gpg"
  ( tar -C "$STORE/daily" -cf - "$STAMP" | gpg -q --batch --trust-model always --compress-algo none \
      --encrypt -r "$GPG_RECIPIENT" -o "$BUNDLE" \
    && python3 "$HERE/gdrive.py" --env-file "$ENV_FILE" upload "$BUNDLE" \
    && python3 "$HERE/gdrive.py" --env-file "$ENV_FILE" prune --keep-daily "$KEEP_DAILY" --keep-weekly "$KEEP_WEEKLY" ) || grc=$?
  rm -f "$BUNDLE"
  if [ $grc = 0 ]; then
    log "copied to Google Drive"; metric pos_backup_last_success_timestamp_seconds "$(date +%s)" 'target="gdrive"'
  else
    log "copy to Google Drive FAILED ($grc)"
  fi
fi
rm -rf "$GNUPGHOME"

metric pos_backup_last_run_timestamp_seconds "$(date +%s)"
metric pos_backup_last_run_ok "$([ $rc = 0 ] && [ $grc = 0 ] && echo 1 || echo 0)"
log "done"
