# The SRE's runbook on svr03 (`ops_runbook`)

No agent has a shell on svr03. The SRE runs a **fixed catalogue** of commands instead, through
the MCP tool `ops_runbook(action, params, reason, task_id?)` (`ops_runbook_list` shows the
catalogue). The catalogue, its allowlists and the pinned GitHub host keys are in
`backend/src/pos/ops_runbook.py`; this folder is the host side.

| Action | Runs (argv, never a shell) |
|---|---|
| `docker_ps`, `docker_stats` | `docker ps -a`, `docker stats --no-stream` |
| `docker_logs {container, since<=24h, tail<=500}` | `docker logs --since … --tail … <container>` |
| `df`, `free`, `uptime` | `df -h`, `free -m`, `uptime` |
| `systemctl_user_status {unit}`, `journalctl_tail {unit, lines<=300}` | `systemctl [--user] status`, `journalctl [--user] -u … -n … --no-pager` |
| `compose_up {stack, service}` | `docker compose -p <stack> --project-directory … -f … up -d --no-deps --no-build <service>` (app services of personalos, kb, nexus-process-pilot, observability, litellm, langfuse; never a database) |
| `restart {container}` | `docker restart <container>` (allowlist; no databases) |
| `backup_run` | `systemd-run --user --unit=pos-runbook-backup --collect --no-block ops/backup/backup.sh` (the nightly job; it has a lock) |
| `towerdog_stop`, `towerdog_start` | `docker stop/start nexus-process-pilot-towerdog-1` |
| `refresh_known_hosts {deployer}` | writes GitHub's pinned host keys into `personalos` (`data/deployer-ssh/known_hosts`) or `kniha` (`/opt/server/kniha-deployer/known_hosts`); old file kept as `.bak`; never `ssh-keyscan` |

Every call needs a reason and leaves one audit row `ops_runbook:<action>` (args, reason, outcome,
exit code, output length, run id). Output is redacted and cut to 8k characters. An action not in
the catalogue runs nothing: the CTO gets one task with the command and the reason (never the
owner). Permission `ops:runbook`: the SRE only (granted once at start, `pos.ops_runbook.ensure`;
not an autonomy default, and `request_access` for it needs the owner).

## Design: a user service of drosko on a unix socket

- `executor.py` (stdlib Python) runs as **drosko's `systemd --user` service** `pos-ops-runbook`
  (linger is on). drosko has no sudo but is in the `docker` and `adm` groups, which is exactly
  what the catalogue needs: docker and compose, `systemctl --user` (the backup unit), the system
  journal and unit status (read-only), `df`/`free`/`uptime` of the **host**. A container with a
  docker-socket-proxy could not see the host's systemd, journal or filesystems without being
  privileged, and a root system service would need sudo nobody has.
- It listens on `ops/runbook/run/runbook.sock` in the checkout (the directory is tracked, so git
  creates it as drosko and the deployer's `git clean` keeps the ignored socket). That directory is
  bind-mounted into the API container at `/run/pos-ops-runbook`; the API finds it with
  `POS_OPS_RUNBOOK_SOCKET` (unset or no socket: the tool says the executor is not installed).
- **It never trusts the API.** A request carries only an action and parameters, never a command.
  The executor validates them again with its **own frozen copy** of the catalogue
  (`~/.local/lib/pos-ops-runbook/catalogue.py`, copied by `install.sh`: a later commit changes
  nothing until the owner reinstalls), builds the argv itself, allows only `docker`, `df`, `free`,
  `uptime`, `systemctl`, `journalctl`, `systemd-run`, runs without a shell with a timeout and a
  minimal environment, accepts only peers with uid 0 (the API container) or drosko
  (`SO_PEERCRED`), serialises write actions and logs every request to its journal.

## Deploy on svr03 (once, as drosko; after this branch is on main and deployed)

```sh
cd /opt/server/personalos/app && git log -1 --oneline   # the checkout has ops/runbook
sh ops/runbook/install.sh                                 # copies executor + catalogue, enables and starts the unit
ls -l ops/runbook/run/runbook.sock                        # the socket exists
journalctl --user -u pos-ops-runbook -n 5 --no-pager      # {"event": "start", ...}

# the API container: the socket mount and POS_OPS_RUNBOOK_SOCKET (deploy/prod/docker-compose.prod.yml);
# the deployer's own "up -d --build api web" applies it too
docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml up -d api
docker exec personalos-api-1 ls -l /run/pos-ops-runbook/  # runbook.sock visible inside
```

Then, as the SRE: `ops_runbook(action="uptime", reason="runbook smoke test")` answers with the
host's uptime, and the audit log has the row `ops_runbook:uptime`.

**Update** the catalogue (a new action, a new container): change `backend/src/pos/ops_runbook.py`
(and its tests), deploy, then run `sh ops/runbook/install.sh` again. Until then the executor
refuses the new values: both sides must agree.

**Stop / remove**: `systemctl --user disable --now pos-ops-runbook` (the tool then answers that
the executor is not installed); `rm -r ~/.local/lib/pos-ops-runbook ~/.config/systemd/user/pos-ops-runbook.service`.
