"""One sentinel tick: measure, judge, remediate, tell PersonalOS."""

import json
import logging
import secrets
import threading
import time
from pathlib import Path

from . import checks, detect, packet, runbook
from .config import matches, service_of
from .docker import Docker
from .incidents import Incidents, Obs
from .pos import Pos
from .store import Store

log = logging.getLogger("sentinel")
VERSION = "1.0"
MAX_LINES = 50_000  # per container per tick; more is counted as a flood anyway


class Sentinel:
    def __init__(self, cfg: dict, store: Store | None = None, docker=None, pos=None, clock=time.time):
        self.cfg, self.t, self.clock = cfg, cfg["thresholds"], clock
        self.s = store or Store(Path(cfg["state_dir"]) / "sentinel.db")
        self.docker = docker or Docker(cfg["docker_host"])
        self.pos = pos or Pos(cfg["pos_url"], cfg["token"], self.s)
        self.inc = Incidents(self.s, self.t, clock)
        self.lock = threading.RLock()
        if not self.s.meta("instance"):
            self.s.set_meta("instance", secrets.token_hex(3))
        self.instance = self.s.meta("instance")
        self.started_at = clock()
        self.host: dict = {}
        self.apps: dict = {}
        self.backups: dict = {}
        self.last_tick: float | None = None
        self.tick_ms = 0
        self.pos_fail_streak = 0
        self.pos_down_since: float | None = None
        self.errors: list[str] = []

    # ------------------------------------------------------------- helpers
    def learning(self, now: float) -> bool:
        return now - (self.s.meta("created_at") or now) < 60 * self.t["warmup_min"]

    def service_of(self, name: str) -> str:
        return service_of(self.cfg, name)

    def watched(self, name: str, key: str) -> bool:
        return matches(name, self.cfg[key]) and not matches(name, self.cfg["ignore"])

    def incident_ref(self, iid: int) -> str:
        return f"{self.instance}-{iid}"

    # ------------------------------------------------------------- the tick
    def tick(self) -> dict:
        t0 = time.monotonic()
        now = self.clock()
        self.errors = []
        obs: list[Obs] = []
        with self.lock:
            obs += self._guard("containers", lambda: self._containers(now))
            obs += self._guard("logs", lambda: self._logs(now))
            obs += self._guard("http", lambda: self._http(now))
            obs += self._guard("host", lambda: self._host())
            if now - (self.s.meta("apps_at") or 0) >= 60 * self.cfg["apps_every_min"] - 5:
                self.s.set_meta("apps_at", now)
                obs += self._guard("apps", lambda: self._apps(now))
            if now - (self.s.meta("backups_at") or 0) >= 60 * self.cfg["backups_every_min"] - 5:
                self.s.set_meta("backups_at", now)
                obs += self._guard("backups", lambda: self._backups(now))
            out = self.process(obs, now)
            self.s.prune(now)
        self.last_tick = now
        self.tick_ms = int((time.monotonic() - t0) * 1000)
        self._heartbeat(now)
        return out

    def _guard(self, what: str, fn) -> list[Obs]:
        try:
            return fn() or []
        except Exception as e:  # noqa: BLE001 - one broken probe must not stop the others
            log.exception("%s failed", what)
            self.errors.append(f"{what}: {type(e).__name__}: {str(e)[:120]}")
            return []

    def process(self, obs: list[Obs], now: float) -> dict:
        opened, notified, resolved = [], [], []
        for o in obs:
            iid, what = self.inc.observe(o, now)
            if what == "opened":
                opened.append(iid)
                result = runbook.maybe_restart(self.s, self.inc, self.cfg, iid, self.docker, now)
                log.info("incident %s opened: %s (%s)", iid, o.title, result)
        for kind, row in self.inc.due(now):
            self._notify(kind, row, now)
            notified.append(row["id"])
        for row in self.inc.resolve_quiet(now):
            if row["notified_level"] >= 0:
                self._notify("incident_resolved", row, now)
            resolved.append(row["id"])
        self.pos.flush()
        return {"opened": opened, "notified": notified, "resolved": resolved}

    def _notify(self, kind: str, row: dict, now: float) -> None:
        p = packet.build(self.s, self.cfg, row, self.host)
        level = "resolved" if kind == "incident_resolved" else self.inc.mark_notified(row["id"], now)
        ref = self.incident_ref(row["id"])
        title = p["title"] if kind != "incident_resolved" else f"Resolved: {row['title']}"[:200]
        if kind == "incident_escalated":
            title = f"Escalated: {p['title']}"[:200]
        payload = {"source": "sentinel", "kind": kind, "ref": f"sentinel:{ref}#{level}", "title": title,
                   "body": p["markdown"], "author": "sentinel",
                   "labels": [f"service:{row['service']}", f"kind:{row['kind']}", f"severity:{row['severity']}"],
                   "data": {"incident": {**p["data"], "incident_id": ref, "level": level}}}
        res = self.pos.send("/api/events", payload)
        if res and res.get("task_ref"):
            self.inc.note(row["id"], f"PersonalOS task {res['task_ref']}", now)

    # ------------------------------------------------------------- probes
    def _containers(self, now: float) -> list[Obs]:
        items = []
        for c in self.docker.containers():
            if not self.watched(c["name"], "containers"):
                continue
            d = self.docker.inspect(c["id"])
            st = d.get("State") or {}
            items.append({"name": c["name"], "status": st.get("Status"), "health": (st.get("Health") or {}).get("Status"),
                          "restart_count": d.get("RestartCount"), "started_at": st.get("StartedAt"),
                          "oom": st.get("OOMKilled"), "exit_code": st.get("ExitCode"),
                          "image": (d.get("Config") or {}).get("Image"), "created": d.get("Created"),
                          "tty": bool((d.get("Config") or {}).get("Tty")), "id": c["id"]})
        self._tty = {i["name"]: i["tty"] for i in items}
        self._running = {i["name"] for i in items if i["status"] == "running"}
        return detect.containers(self.s, self.t, self.service_of, items, now)

    def _logs(self, now: float) -> list[Obs]:
        out = []
        learning = self.learning(now)
        for name in sorted(getattr(self, "_running", set())):
            if not self.watched(name, "logs"):
                continue
            cursor = self.s.meta(f"cursor:{name}") or (now - 60)
            lines = self.docker.logs(name, since=cursor, tty=self._tty.get(name, False))
            lines = [(ts, line) for ts, line in lines if ts > cursor][-MAX_LINES:]
            if not lines:
                continue
            self.s.set_meta(f"cursor:{name}", max(ts for ts, _ in lines))
            out += detect.ingest_logs(self.s, self.t, self.service_of(name), name, lines, now, learning)
        return out

    def _http(self, now: float) -> list[Obs]:
        out = []
        for c in self.cfg["http"]:
            c = {**c, "url": c["url"].replace("{pos_url}", self.cfg["pos_url"])}
            r = checks.http_check(c["url"], c.get("connect"), json_level=c.get("json_level"))
            out += detect.health(self.s, self.t, c, r, now)
        return out

    def _host(self) -> list[Obs]:
        self.host = checks.host(disks=[self.cfg["state_dir"]])
        return detect.host(self.t, self.host)

    def _apps(self, now: float) -> list[Obs]:
        out = []
        stats = {"personalos": checks.pos_runs(self.cfg["pos_url"], self.cfg["token"]),
                 "nexus": checks.nexus_runs(self.cfg["nexus_dsn"]),
                 "knowlage": checks.knowlage_runs(self.cfg["knowlage_url"], now - 3600)}
        self.apps = {k: v for k, v in stats.items() if v is not None}
        for app, st in stats.items():
            out += detect.runs(self.t, app, st)
        out += detect.push(self.t, stats["personalos"])
        b = checks.litellm_budgets(self.cfg["litellm_url"], self.cfg["litellm_key"])
        if b is not None:
            self.apps["litellm_budgets"] = len(b)
        out += detect.budgets(self.t, b)
        return out

    def _backups(self, now: float) -> list[Obs]:
        """Age of each backup: an incident over the thresholds, one JSON line each (Alloy labels it
        service="backup" in Loki) and backup_age_hours / probe_success on /metrics."""
        out = []
        for name, path in self.cfg["backups"].items():
            r = checks.backup_age(path, now)
            status = detect.backup_status(self.t, r["age_h"])
            self.backups[name] = {"age_h": r["age_h"], "status": status, "detail": r.get("detail") or ""}
            level = {"ok": "info", "warn": "warn", "fail": "error"}[status]
            print(json.dumps({"level": level, "check": "backup", "backup": name, "age_h": r["age_h"],
                              "status": status, "detail": r.get("detail") or "", "path": path,
                              "warn_h": self.t["backup_warn_h"], "fail_h": self.t["backup_fail_h"]}), flush=True)
            out += detect.backup(self.s, self.t, name, r, now)
        return out

    def metrics(self) -> str:
        """Prometheus text for Alloy: backup ages, and probe_success (0 over the fail threshold or
        missing) so metrics_snapshot lists a stale backup under failing_checks."""
        lines = ["# TYPE backup_age_hours gauge"]
        for name, b in sorted(self.backups.items()):
            if b["age_h"] is not None:
                lines.append(f'backup_age_hours{{backup="{name}"}} {b["age_h"]}')
        lines.append("# TYPE probe_success gauge")
        for name, b in sorted(self.backups.items()):
            lines.append(f'probe_success{{app="backup-{name}"}} {int(b["status"] != "fail")}')
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------- heartbeat and the fallback path
    def status(self, now: float | None = None) -> dict:
        now = now or self.clock()
        with self.lock:
            checks_ = [dict(r) for r in self.s.q("SELECT name, service, ok, fails, detail FROM checks ORDER BY name")]
            open_ = [{"id": self.incident_ref(r["id"]), "service": r["service"], "kind": r["kind"],
                      "severity": r["severity"], "title": r["title"], "count": r["count"],
                      "opened_at": packet.iso(r["opened_at"]), "notified": r["notified_level"] >= 0}
                     for r in self.s.q("SELECT * FROM incidents WHERE status = 'open' ORDER BY id DESC LIMIT 20")]
            day = now - 86400
            counters = {
                "incidents_24h": self.s.one("SELECT COUNT(*) AS n FROM incidents WHERE opened_at >= ?", day)["n"],
                "escalated_24h": self.s.one("SELECT COUNT(*) AS n FROM incidents WHERE escalated_at >= ?", day)["n"],
                "remediations_24h": self.s.one("SELECT COUNT(*) AS n FROM remediations WHERE at >= ?", day)["n"],
                "remediations_fixed_24h": self.s.one(
                    "SELECT COUNT(*) AS n FROM incidents WHERE remediated_at >= ? AND escalated_at IS NULL "
                    "AND status = 'resolved'", day)["n"],
                "fingerprints": self.s.one("SELECT COUNT(*) AS n FROM fp")["n"],
                "outbox": self.pos.pending(),
            }
        return {"instance": self.instance, "version": VERSION, "at": packet.iso(now),
                "started_at": packet.iso(self.started_at), "tick_ms": self.tick_ms, "learning": self.learning(now),
                "checks": [{"name": c["name"], "service": c["service"], "ok": bool(c["ok"]), "fails": c["fails"]}
                           for c in checks_],
                "open_incidents": open_, "counters": counters, "host": self.host, "apps": self.apps,
                "backups": self.backups, "errors": self.errors[:5]}

    def _heartbeat(self, now: float) -> None:
        ok = self.pos.heartbeat(self.status(now))
        self.pos_fail_streak = 0 if ok else self.pos_fail_streak + 1
        path = Path(self.cfg["state_dir"]) / "ALERT-personalos-down.md"
        if ok:
            if self.pos_down_since:
                log.warning("PersonalOS is back after %.0f min", (now - self.pos_down_since) / 60)
                self._alert_log(f"{packet.iso(now)} PersonalOS reachable again")
            self.pos_down_since = None
            if path.exists():
                path.unlink()
            return
        if self.pos_fail_streak >= 3:
            if self.pos_down_since is None:
                self.pos_down_since = now - 60 * (self.pos_fail_streak - 1)
                self._alert_log(f"{packet.iso(now)} PersonalOS unreachable: {self.pos.last_error}")
            try:
                path.write_text(self.fallback_text(now), encoding="utf-8")
            except OSError:
                pass

    def _alert_log(self, line: str) -> None:
        try:
            with open(Path(self.cfg["state_dir"]) / "alerts.log", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def fallback_text(self, now: float) -> str:
        st = self.status(now)
        lines = [f"# PersonalOS is down (sentinel {self.instance})", "",
                 f"- **Unreachable since:** {packet.iso(self.pos_down_since)} ({self.pos.last_error})",
                 f"- **Events waiting for PersonalOS:** {st['counters']['outbox']}", "", "## Open incidents"]
        lines += [f"- [{i['severity']}] {i['title']} (×{i['count']}, since {i['opened_at']})"
                  for i in st["open_incidents"]] or ["- none"]
        lines += ["", "Check: `docker compose ps api` in /opt/server/personalos/app, then its logs."]
        return "\n".join(lines) + "\n"

