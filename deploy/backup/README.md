# Host backups on svr03: systemd timers with catch-up (T-379)

The daily backups that the sentinel watches (`/backups/{personalos,knowlage,nexus}`,
see `docs/SENTINEL.md`) ran from cron (journal: `session-NNNNN.scope`). Cron does
not run a job missed while the host was down: after the freeze of 29. 9. 22:23
(boot 30. 9. 14:41) the knowlage backup waited for the next 10:00.

This folder moves only the **start** of each job to systemd:

| File | Goes to | What |
|---|---|---|
| `pos-backup@.timer` | `/etc/systemd/system/` | daily 10:00 (±5 min), `Persistent=true`, and 15 min after boot |
| `pos-backup@.service` | `/etc/systemd/system/` | oneshot, runs the wrapper for instance `%i` |
| `pos-backup.sh` | `/usr/local/lib/pos-backup/` | skips when the newest backup is < `MIN_AGE_H` (12 h) old, else runs `BACKUP_CMD` under a lock |
| `<name>.env` | `/etc/pos-backup/` | `BACKUP_CMD` (the old cron command, unchanged), `BACKUP_DIR` |

The backup command, its target and its retention stay exactly as they are; nothing
is deleted or rewritten. Instances: `knowlage`, `personalos`, `nexus` (only those
that have a cron line today).

## Install (per backup, as root; `$REPO` is the PersonalOS checkout)

```sh
crontab -l; ls /etc/cron.d; systemctl list-timers --all   # 1. find the current job
cp /etc/crontab /root/crontab.bak.$(date +%F); crontab -l > /root/crontab-root.bak.$(date +%F)

install -D -m 0755 $REPO/deploy/backup/pos-backup.sh /usr/local/lib/pos-backup/pos-backup.sh
install -m 0644 $REPO/deploy/backup/pos-backup@.service $REPO/deploy/backup/pos-backup@.timer /etc/systemd/system/
install -d -m 0750 /etc/pos-backup
install -m 0640 $REPO/deploy/backup/knowlage.env.example /etc/pos-backup/knowlage.env
$EDITOR /etc/pos-backup/knowlage.env        # BACKUP_CMD = the cron line's command, BACKUP_DIR = its target

/usr/local/lib/pos-backup/pos-backup.sh knowlage   # dry check: backup fresh -> "skipping"
crontab -e                                  # comment out the old line: "# T-379 moved to pos-backup@knowlage.timer: ..."
systemctl daemon-reload
systemctl enable --now pos-backup@knowlage.timer
systemctl list-timers 'pos-backup@*'
```

Do the cron edit and `enable --now` together, so the job is never scheduled twice
or not at all. (A double start is harmless anyway: the lock and the age check stop it.)

## Verify a missed run is caught up

On svr03 after install, or in a VM/container with systemd:

```sh
systemctl stop pos-backup@knowlage.timer
touch -d '2 days ago' /var/lib/systemd/timers/stamp-pos-backup@knowlage.timer
touch -d '2 days ago' /opt/server/kb/backups/<newest>    # only in a test VM, never on svr03
systemctl daemon-reload; systemctl start pos-backup@knowlage.timer
systemctl list-timers 'pos-backup@*'        # LAST = now, the missed 10:00 fired at start
journalctl -u pos-backup@knowlage.service -n 20   # "newest backup is 48 h old, running"
```

On svr03 itself, skip the second `touch`: the job then logs "skipping" (the backup
is fresh), which shows the timer fired without making an extra backup.

## Rollback

```sh
systemctl disable --now pos-backup@knowlage.timer
crontab -e                                  # uncomment the old line (or restore /root/crontab-root.bak.<date>)
systemctl daemon-reload
```

The unit files can stay in `/etc/systemd/system/`: a disabled timer starts nothing.

The backups themselves are not touched by install or rollback.
