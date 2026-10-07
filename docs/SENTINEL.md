# Sentinel and the Hlídač (on-call)

Code watches everything; a model wakes only for a real incident, and it gets
a compact packet, never raw logs.

```
 svr03 containers, logs, HTTP, host      ops/sentinel (every 60 s, no model)
 ───────────────────────────────────▶    measure → fingerprint → rules → incident
                                           │ runbook: 1 restart / 30 min (allowlist)
                                           │ still open → POST /api/events (source sentinel)
                                           ▼
 PersonalOS  routing rule "Sentinel incident → Hlídač"   (pos.monitor: caps first)
                                           ▼
 Hlídač (Haiku, low effort)  classify → fix task (engineer/specialist) | SRE task | close
```

## The sentinel (`ops/sentinel`, compose profile `sentinel`)

A small Python service (standard library, plus psycopg for Nexus and git for
recent commits) with its own SQLite in the `sentinel-state` volume. Docker is
reached through `docker-socket-proxy`: container reads, logs and restarts
only. Every minute:

| Signal | Source | Incident when (defaults in `sentinel/config.py`) |
|---|---|---|
| HTTP health | public URLs through `caddy-proxy:443` (SNI) and internal health endpoints | 3 failures in a row |
| TLS | the certificate of each public check | < 14 days (high < 5) |
| Containers | state, `RestartCount`, `OOMKilled`, health | down 3 ticks (not a clean exit), ≥3 restarts in 15 min, OOM, unhealthy 3 ticks |
| Host | `/proc` and the state volume's disk | disk ≥ 90 %, memory available < 5 %, **swap ≥ 90 %**, load5 ≥ 3 per CPU |
| Runs | PersonalOS `/api/sentinel/stats`, Nexus `runs` + `workflow_runs` (read-only role), knowlage sync runs | > 50 % failed over ≥ 5 finished runs in the last hour |
| Push | PersonalOS `/api/sentinel/stats` → `push` (pos.push.health) | ≥ 3 failed sends in 24 h and no device accepted one (high); a browser failed to subscribe while the owner has no subscribed device (medium) |
| HTTP codes | access lines in the logs | 5xx ≥ 20 and ≥ 20 % (5 min), 429 ≥ 20, 401 ≥ 100, "usage limit"/quota lines ≥ 3 |
| LiteLLM spend | `/key/list`, `/team/list` with a viewer key | spend ≥ 90 % of `max_budget` |
| Log errors | docker logs since the last tick, fingerprinted | a new fingerprint ≥ 5 in 5 min; a known one > 10× its 24 h baseline and ≥ 5/min |
| Backups (every 15 min) | mtime of the newest file or snapshot dir (3 levels deep) in each backup directory | older than 26 h (medium), older than 48 h, missing or empty (high) |

**Backups.** Three read-only mounts in `deploy/prod/docker-compose.prod.yml`
(no new container, no extra memory; the sentinel only stats files):

| Backup | Host path (env, default) | In the container |
|---|---|---|
| PersonalOS `data/` | `POS_BACKUP_DIR`, `/opt/server/backups/personalos/daily` | `/backups/personalos` |
| knowlage `kb_data` | `KNOWLAGE_BACKUP_DIR`, `/opt/server/backups/personalos/daily` | `/backups/knowlage` |
| Nexus Postgres | `NEXUS_BACKUP_DIR`, `/opt/server/backups/personalos/daily` | `/backups/nexus` |

All three are written by the nightly `ops/backup/backup.sh` (one directory per night). Point the env at the directory the backup job actually writes (not the live
data: the live SQLite changes every minute and would always look fresh). The
sentinel runs as uid 10001 and needs read and execute on the directory. Thresholds:
`backup_warn_h` 26, `backup_fail_h` 48 in `thresholds`; paths in `backups`
(both overridable in `sentinel.json`). Each run gives:
- a check row `backup-<name>` (service `backup`) in `/api/status` and the heartbeat, plus `backups: {name: {age_h, status}}`;
- one JSON line per backup (`{"level": "info|warn|error", "check": "backup", "backup", "age_h", "status", …}`),
  which Alloy on svr03 labels `service="backup"`: `{host="svr03", service="backup"}` in Loki;
- `GET /metrics` (no token, scraped by Alloy as `job="sentinel"`): `backup_age_hours{backup}` and
  `probe_success{app="backup-<name>"}` (0 above 48 h or missing). `metrics_snapshot("svr03")` shows them as
  `backup_age_h` and, when stale, under `failing_checks`.

**Fingerprints.** An error line is normalized (timestamps, UUIDs, e-mails,
keys, IPs, URLs, paths, hex and long ids, quoted values, numbers →
placeholders) and hashed per service; access lines count by route and status.
10 000 identical errors are one fingerprint with a count and at most 20
deduplicated, redacted samples. The first 30 minutes after a fresh state are
a warm-up: fingerprints are learned, nothing opens.

**Incidents** are deduplicated by (service, kind, fingerprint or check). An
open one absorbs further observations and re-notifies only on escalation
(severity up, or ten times the count PersonalOS last heard). It resolves
itself after 30 quiet minutes (health: 10).

**Runbook.** A failing health check of an allowlisted stateless container
(`personalos-web-1`, `personalos-api-1`, Nexus web and API, `kb-web-1`,
`litellm`, `langfuse-web`) gets one `docker restart` per 30 minutes. If the
check recovers within the grace (150 s), the incident closes with a note and
PersonalOS never hears about it. Databases, queues and volumes are never
touched.

**Packet** (≤ 6 kB): service, kind, severity, fingerprint, count, first and
last seen, the service's containers (status, restarts, OOM), containers
recreated and commits in the last 24 h, the runbook's notes, host numbers and
≤ 20 redacted sample lines. Redaction removes bearer tokens, API keys, JWTs,
passwords in URLs and key=value pairs, long opaque strings and e-mail
addresses.

**API** (`:8097`, token = `POS_SENTINEL_TOKEN`): `GET /api/logs` (one watched
container, ≤ 30 min, ≤ 100 lines, errors only unless a fingerprint or grep,
deduplicated and redacted), `POST /api/incidents/<id>/ack`,
`GET /api/status`; without a token `GET /healthz` and `GET /fallback`.
`POST /api/test/inject` feeds synthetic lines through the real pipeline
(service `sentinel-test`) only with `SENTINEL_TEST_HOOK=1`.

## PersonalOS (`pos.monitor`)

- **Events**: `POST /api/events` with the sentinel's token, `source:
  sentinel`, `ref: sentinel:<instance>-<id>#<level>` (idempotent), `data.incident`.
  New incidents go by the rule to the **Monitor** agent (priority 1, topic
  `provoz`); it closes its own tasks. Escalations become comments; a task the
  Monitor called transient reopens. A resolution closes a task not yet
  started, without a model run.
- **Caps (code, before any run)**: ≤ 12 incident tasks a day
  (`POS_MONITOR_MAX_INCIDENTS_DAY`), ≤ 2 Monitor runs per incident
  (`POS_MONITOR_MAX_RUNS_INCIDENT`), and the Monitor's access budget
  (USD 1/day, 15/month, 0.30/run, 25 runs/day). Above them, paused, or with
  the kill switch on, the incident goes to the owner through `ask_owner` as
  code-built text with a recommendation.
- **Monitor tools**: `incident_logs` (≤ 6 reads per incident, wrapped as
  untrusted) and `incident_close` (classification + summary; tells the
  sentinel). Permission `ops:monitor`.
- **Heartbeat**: `POST /api/sentinel/heartbeat` every minute; the System page
  shows it (checks, open incidents, host). The job "Sentinel: alert when its
  heartbeat stops" asks the owner once when it is older than 5 minutes.
- **Digest** (daily 08:00): code-built numbers in #team, plus one short
  Monitor paragraph (a cheap run) only when there were incidents. Quiet days:
  nothing, except "7 dní bez incidentu" on Mondays.

## The SRE's runbook on svr03 (`ops_runbook`)

No agent has a shell on svr03. The SRE (permission `ops:runbook`, not an autonomy default)
runs a fixed catalogue instead: `ops_runbook(action, params, reason, task_id?)` and
`ops_runbook_list()`. Read-only `docker_ps`, `docker_stats`, `docker_logs` (≤ 24 h, ≤ 500
lines), `df`, `free`, `uptime`, `systemctl_user_status`, `journalctl_tail` (≤ 300 lines);
actions `compose_up` (our stacks' app services, never a database), `restart` (allowlist),
`backup_run`, `towerdog_stop`/`towerdog_start`, `refresh_known_hosts` (GitHub's pinned keys).
Every call needs a reason and leaves an audit row `ops_runbook:<action>` with the run id;
output is redacted and cut to 8k. Anything else: one task for the CTO, never the owner. The
host side (a user service of drosko on a unix socket, re-validating every request) and its
install: `ops/runbook/README.md`.

## When PersonalOS itself is down

The sentinel keeps events in its outbox (retried every tick, in order) and,
after 3 failed heartbeats, writes `ALERT-personalos-down.md` in its state
volume and answers `GET /fallback` with 503 and the open incidents. The Nexus
service monitor on the host (`/opt/server/nexus-service-monitor`, already
alerting through Nexus) checks `http://127.0.0.1:8097/fallback`, so the
outage shows in Nexus. No new external service.

## Deploy (svr03)

```bash
cd /opt/server/personalos/app
# .env: POS_SENTINEL_TOKEN (openssl rand -hex 24), SENTINEL_NEXUS_DSN, SENTINEL_LITELLM_KEY
docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml up -d --build api web
docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml --profile agents up -d --build monitor
docker compose -f docker-compose.yml -f deploy/prod/docker-compose.prod.yml --profile sentinel up -d --build
```

Nexus read-only role (once): `CREATE ROLE sentinel_ro LOGIN PASSWORD '…';
GRANT CONNECT ON DATABASE nexus TO sentinel_ro; GRANT USAGE ON SCHEMA public TO
sentinel_ro; GRANT SELECT ON runs, workflow_runs, run_events TO sentinel_ro;`

## Cost

A quiet day: no model tokens (the digest is code-only and silent). An
incident: one Haiku run at low effort; measured on svr03 at rollout 7–21 k
input and 1.5–3.6 k output tokens, 4–9 tool calls, USD 0.03–0.07 per incident;
at most 2 runs per incident and 12 incidents a day (≤ USD 1/day by budget). A
day with incidents adds one digest run (a few thousand tokens). A code bug
adds the Dev agent's own run (Opus) under its own budget.
