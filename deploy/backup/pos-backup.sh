#!/usr/bin/env bash
# Run one host backup job (knowlage, personalos, nexus) with catch-up (T-379).
#
# Started by pos-backup@<name>.service; reads /etc/pos-backup/<name>.env:
#   BACKUP_CMD   the existing backup command (the old cron line's command)
#   BACKUP_DIR   the directory that command writes (what the sentinel watches)
#   MIN_AGE_H    skip when the newest backup is younger than this (default 12)
#
# The timer fires daily at 10:00, again after a missed run (Persistent=true)
# and 15 min after boot. The age check makes the extra starts harmless: a
# fresh backup means nothing to do. This script only starts the job; it never
# deletes or rotates backups (retention stays in BACKUP_CMD).
set -euo pipefail

name="${1:?usage: pos-backup.sh <name>}"
env_file="${POS_BACKUP_ENV_DIR:-/etc/pos-backup}/${name}.env"
[ -r "$env_file" ] || { echo "pos-backup: no $env_file" >&2; exit 2; }
# shellcheck disable=SC1090
. "$env_file"
: "${BACKUP_CMD:?BACKUP_CMD missing in $env_file}"
: "${BACKUP_DIR:?BACKUP_DIR missing in $env_file}"
min_age_h="${MIN_AGE_H:-12}"

# Newest file or dir mtime, 3 levels deep, as the sentinel measures it. stat -c %Y works with GNU
# and BusyBox (find -printf is GNU only: under set -e -o pipefail it ended the script silently).
newest="$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 3 -exec stat -c '%Y' {} + 2>/dev/null | sort -n | tail -1 || true)"
now="$(date +%s)"
if [ -n "$newest" ]; then
  age_s=$(( now - ${newest%.*} ))
  if [ "$age_s" -lt $(( min_age_h * 3600 )) ]; then
    echo "pos-backup $name: newest backup is $(( age_s / 60 )) min old (< ${min_age_h} h), skipping"
    exit 0
  fi
  echo "pos-backup $name: newest backup is $(( age_s / 3600 )) h old, running"
else
  echo "pos-backup $name: no backup in $BACKUP_DIR, running"
fi

lock="${POS_BACKUP_LOCK_DIR:-/run/lock}/pos-backup-${name}.lock"
exec 9>"$lock"
flock -n 9 || { echo "pos-backup $name: already running"; exit 0; }
bash -c "$BACKUP_CMD"
