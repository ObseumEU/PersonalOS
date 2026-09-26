# Observability: Grafana, Loki, Prometheus (on .186), Alloy collectors, alerts into PersonalOS

```
svr03 (192.168.1.108)                        agent (192.168.1.186)
 obs-alloy ── logs ───────────────────────▶  loki (existing, tenant "Obseum")  ◀── grafana (existing, org "Obseum")
   docker logs, journal, node, cAdvisor,      obs-prometheus ◀── remote write        │  alert rules team=platform
   LiteLLM /metrics, Nexus run counts   ───▶  obs-blackbox (probes the apps)          ▼
                                              obs-alloy (this host)               obs-hook ──▶ personalos.obseum.cz
                                                                                      /api/hooks/grafana → Monitor (Hlídač)
```

- **Grafana**: https://grafana.obseum.cloud (LAN and VPN; 403 outside), org **Obseum**, folder
  **Obseum platform**: *Server overview*, *Apps*, *LLM usage*.
- The Grafana and Loki on .186 are **shared with the innogy project** (orgs Main, Obseum, Innogy).
  Everything here is additive: new provisioning files only (`00-obs-platform.yaml` dashboards
  provider, `obs-platform.yaml` datasource and alerting, all `orgId: 2`), a separate compose project
  `obs-platform` in `/opt/observability/platform`, our own Loki tenant `Obseum`. Innogy's tenant,
  org, dashboards, users and alerting are never touched. Back up `Data/grafana.db` and `loki-data`
  (`/opt/observability/backups/`) before changing anything there.

## What runs where

| Host | Path | Containers | Memory limit |
|---|---|---|---|
| svr03 | `/opt/server/observability` | `obs-alloy` (logs, host and container metrics, LiteLLM, Nexus runs) | 256 MB |
| .186 | `/opt/observability/platform` | `obs-prometheus` (14 d, ≤ 6 GB), `obs-blackbox`, `obs-alloy`, `obs-hook` | 768 + 64 + 256 + 48 MB |
| .186 | `/opt/observability/grafana` | `grafana`, `loki` (existing; retention 14 d for all tenants) | as before |

svr03's standalone Netdata (`/opt/server/monitoring`) is stopped (restart `no`, volumes kept): it
duplicated the metrics, used ~320 MB on a host deep in swap, and fed Nexus's *Netdata Alarm
Responder*; alerts now go to PersonalOS's Monitor agent.

**Logs** (Loki, tenant `Obseum`): labels `host`, `source` (docker|journal), `stack` (compose
project), `service`, `container`, `level` (error|warn|info|debug, from JSON `level`/pino numbers or
text prefixes) and `http="5xx"`. The rest is parsed at query time (`| json`). Each container is
rate-limited (50 lines/s) so a crash loop cannot flood.

**Metrics** (Prometheus): `node_*` per `host`, cAdvisor per `container_id`; the recording rules in
`agent186/rules.yml` add names: `container:memory_working_set_bytes`, `container:cpu_cores:rate5m`,
`container:restarts:15m|1h`, `container:oom_events:1h`, `host:*:ratio`. (Docker 29 on svr03 uses the
containerd image store, which cAdvisor cannot name; `up{job="docker_meta"}` carries the names.)

**Alerts** (`grafana/provisioning/alerting/obs-platform.yaml`, label `team=platform`, grouped by
alertname and host, repeat 12 h): swap > 90 % 10 min · disk > 85 % 15 min · root FS read-only ·
host metrics stopped (Docker or host down) · restart loop (≥ 3 starts in 15 min) · OOM kill ·
health check failing 5 min · app error spike (> 50 error lines / 10 min) · app 5xx (> 20 / 10 min)
· Nexus run failure ratio > 50 % (≥ 5 runs / h) · LiteLLM 5xx · LiteLLM budget/quota/rate-limit
errors · LiteLLM key budget < 10 %. Docker down on .186 stops Grafana itself: PersonalOS checks
Grafana every 5 minutes and asks the owner after three failures (`grafana_watch`).

## PersonalOS side

`POST /api/hooks/grafana` (bearer `POS_GRAFANA_TOKEN`; the same value is in
`/opt/observability/platform/.env`, where `obs-hook` adds it). Each alert → one event
`grafana:<fingerprint>:<start>:<state>` (repeats are duplicates) → the rule *Grafana alert →
Monitor* → an incident task for Hlídač, or the owner (ask) when the Monitor is missing or over its
caps. Resolved closes a task the Monitor has not started. The System page shows firing alerts from
these stored events. The Monitor's tools (permission `ops:observe`): `loki_query` (log query only,
≤ 60 min, ≤ 200 lines, redacted) and `metrics_snapshot` (fixed queries per host).

## Install / update

```bash
# svr03
cd /opt/server/observability          # files from deploy/observability/svr03
# secrets/ (mode 600): litellm_metrics_key (LiteLLM user obs-metrics, role proxy_admin_viewer,
#   budget 0), nexus_dsn (role obs_ro: SELECT on runs, workflow_runs; no trailing newline)
docker compose up -d

# .186
cd /opt/observability/platform        # files from deploy/observability/agent186
# .env (mode 600): POS_GRAFANA_TOKEN=<same as PersonalOS .env>
sudo docker compose up -d
# Grafana provisioning (additive): copy grafana/provisioning/*/obs-platform.yaml,
# 00-obs-platform.yaml and grafana/dashboards/*.json (into dashboards/files/platform/),
# owner 472, then `sudo docker restart grafana`.
python deploy/observability/grafana/build_dashboards.py   # after editing dashboards
```

The Caddy block for grafana.obseum.cloud is `caddy/grafana.Caddyfile` (in svr03's front proxy).
`caddy/logi.Caddyfile`: the older `logi.obseum.cloud` (Loki) now answers reads only on the LAN/VPN;
log pushes from outside (innogy) stay open. `log.obseum.cloud` still serves the Grafana login
publicly (unchanged).
