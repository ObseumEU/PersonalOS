# Nightly backups (svr03)

`backup.sh` runs nightly from drosko's crontab on svr03 (no sudo there), from the deployed checkout:

```
30 2 * * * /opt/server/personalos/app/ops/backup/backup.sh >/dev/null 2>&1   # pos-backup
30 4 1 * * /opt/server/personalos/app/ops/backup/restore_drill.sh >/dev/null 2>&1   # pos-backup-drill
```

## What

| Part | How | File in `daily/<stamp>/` |
|---|---|---|
| PersonalOS `personalos.db`, sentinel `sentinel.db` | SQLite online backup API + integrity_check + row counts | `sqlite/*.db.zst`, `sqlite/sqlite-manifest.json` |
| knowlage `kb.sqlite` and the Drive catalogs `gdrive/*.sqlite` | same (quick_check for the 1 GB kb.sqlite) | `sqlite/knowlage*.db.zst` |
| Nexus, LiteLLM, Langfuse Postgres | `pg_dump -Fc`, `pg_restore --list`, exact row counts | `pg/<container>.dump`, `.counts.tsv`, `.image` |
| Volumes `personalos_pos-data` (agents, files), Nexus `knowledge-objects`, `team-files` | tar without SQLite files | `files/*.tar.zst` |
| Compose files, Caddyfiles, LiteLLM and Alloy config | tar | `config/config.tar.zst` |
| `.env` of PersonalOS, knowlage, Nexus, LiteLLM, Langfuse; Alloy secrets; Postgres roles | tar, **gpg-encrypted** | `config/secrets.tar.zst.gpg` |

Not backed up: Qdrant (derived: re-index from `kb.sqlite`, costs embedding calls), knowlage's
`workspace/` export and `cache/` (derived), the Codex/Claude logins (sign in again).

Store `/opt/server/backups/personalos/`: `daily/` (14 kept), `weekly/` (hard links of Sunday's daily,
8 kept), `archive/pos-data-adhoc/` (ad-hoc `personalos.*.db` copies moved off the data volume: each run
keeps the newest 3 and anything under 48 h in `/data`, the rest is copied here, SHA-256 checked, then
removed), `logs/`, `drill/`, `metrics/pos_backup.prom`.

## Off-host

- **.186** (`agent@192.168.1.186:/srv/backups/svr03/`): `rsync -aH --delete` mirror of daily, weekly
  and archive, with svr03's key `~/.ssh/pos_backup_ed25519`, restricted on .186 to
  `restrict,from="192.168.1.108",command="/usr/bin/rrsync /srv/backups/svr03"`. .186's SSD is worn,
  so it is not the only copy.
- **Google Drive** of david.rosko@obseum.cz, folder **PersonalOS zálohy** (created by the
  `drive.file` token `POS_GDRIVE_FILE_TOKEN` of the api's `.env`, which sees only what it created):
  each night one file `pos-backup-<stamp>-daily|weekly.tar.gpg`, the whole backup encrypted; 14 daily
  + 8 weekly kept, older ones go to the Drive trash. `python3 gdrive.py quota|list`.

## The encryption key (not on svr03, .186 or Drive)

gpg key `B553517B011A040CA0DBD068D168BA163D99F61A` ("PersonalOS backups (svr03)"). svr03 only has
the public key (`backup-pubkey.asc`). The private key is on the owner's PC:
`C:\Users\rosko\.personalos-backup-key\personalos-backup-SECRET.asc` (and the keyring `gnupg\` next
to it, with the revocation certificate). **Put a copy in the password manager**: without it the
Drive copies and `secrets.tar.zst.gpg` cannot be opened. No passphrase (the file is the secret).

```bash
gpg --import personalos-backup-SECRET.asc
gpg -d pos-backup-<stamp>-daily.tar.gpg | tar -xf -          # a Drive copy
gpg -d config/secrets.tar.zst.gpg | zstd -d | tar -tvf -     # the .env files
```

## Restore

- SQLite: `zstd -d personalos.db.zst`, stop the app, replace `/data/personalos.db` (and remove
  `-wal`/`-shm`) in the volume, start. Same for `kb.sqlite` (`kb_kb_data`), then reindex Qdrant if lost.
- Postgres: `docker exec -i <pg> pg_restore -U "$POSTGRES_USER" -d <db> --clean --if-exists < pg/<c>.dump`.
- `restore_drill.sh [dir]` does all of this into a temp dir and throwaway containers of the same
  images and compares the row counts; report in `drill/<stamp>.json`.

## Alerts

`metrics/pos_backup.prom` is read by obs-alloy's textfile collector (`deploy/observability/svr03/config.alloy`):
`pos_backup_last_success_timestamp_seconds{target="local|agent186|gdrive"}`,
`pos_backup_drill_last_success_timestamp_seconds`, `pos_backup_last_run_ok`, `pos_backup_size_bytes`.
Grafana (`obs-platform.yaml`): *PersonalOS backup older than 26 h* (any target, or no metric) and
*restore drill older than 35 days* → PersonalOS Monitor. The sentinel also watches the age of
`daily/` (`POS_BACKUP_DIR`, `KNOWLAGE_BACKUP_DIR`, `NEXUS_BACKUP_DIR`).
