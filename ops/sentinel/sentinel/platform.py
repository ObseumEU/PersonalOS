"""PersonalOS's own health, every cfg.platform.every_s (15 s): alert the owner and the SRE, restart the API.

prod 2026-10-13 11:10-11:31: the owner's app would not load (31x "database is locked", /api/auth/me 500 after
10 s), /api/health said 200 the whole time, nothing alerted and the owner found it himself; a manual restart of
personalos-api-1 fixed it. This watch probes GET /api/health/deep (a real read and the write lock taken and
released, plus the API's last minute) and decides in code, no model:

- **problem**: the deep health failed `alert_after_fails` probes in a row, or the last minute had
  >= fivexx_min 5xx at >= fivexx_ratio of the requests, or >= locked_per_min "database is locked" (the deep
  health's count, or the API's log lines counted by detect.ingest_logs).
- **one incident** (kind `platform`, never the generic /api/events path: incidents.SELF_MANAGED) and one
  alert to PersonalOS's POST /api/sentinel/alert: the owner at once (push, else e-mail) and the SRE's task.
- **auto-heal**: the deep health failing continuously for heal_after_s (90 s) restarts the container, at most
  once per heal_cooldown_min (15 min; any restart of it counts, the runbook's too, table `remediations`). The
  logs around it go into the incident log (`<state>/incident-log/`, the incident's notes, alerts.log) and to
  PersonalOS (`healed`: the SRE's task and the audit row).
- **escalation** (once): still failing heal_grace_s after the restart, the restart failed, or a restart is due
  but the cooldown forbids it: `escalated` to the owner and the SRE.
- **recovery**: no problem for recover_ok_s: the incident resolves and both hear `recovered`.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import checks
from .incidents import Obs
from .redact import redact

log = logging.getLogger("sentinel.platform")

KIND = "platform"
SERVICE = "personalos"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PlatformWatch:
    def __init__(self, sen, probe=None):
        self.sen = sen
        self.p = sen.cfg["platform"]
        self.probe = probe or checks.http_check
        self.last_probe_at: float | None = None
        self.last: dict = {}

    # ------------------------------------------------------------- state (survives a sentinel restart)
    def state(self) -> dict:
        return self.sen.s.meta("platform") or {}

    def _save(self, st: dict) -> None:
        self.sen.s.set_meta("platform", st)

    def due(self, now: float) -> bool:
        return self.last_probe_at is None or now - self.last_probe_at >= self.p["every_s"] - 0.5

    # ------------------------------------------------------------- the probe and the signals
    def measure(self, now: float) -> dict:
        url = self.p["url"].replace("{pos_url}", self.sen.cfg["pos_url"])
        r = self.probe(url, timeout=self.p["timeout_s"], want_json=True)
        body = r.get("json") or {}
        lm = body.get("last_minute") or {}
        container = self.p["container"]
        m = int(now // 60)
        log_locked = max(self.sen.s.stat_sum(container, "locked", m - 1, m), self.sen.s.stat_sum(container, "locked", m, m + 1))
        return {"deep_ok": bool(r.get("ok")), "status": r.get("status"), "ms": r.get("ms"),
                "reason": body.get("reason") or r.get("detail") or (f"HTTP {r.get('status')}" if r.get("status") else ""),
                "requests": int(lm.get("requests") or 0), "errors_5xx": int(lm.get("errors_5xx") or 0),
                "rate_5xx": float(lm.get("rate_5xx") or 0.0), "p95_ms": lm.get("p95_ms"),
                "locked": max(int(lm.get("db_locked") or 0), log_locked), "long_tx": int(lm.get("long_tx") or 0)}

    def signals(self, st: dict, m: dict) -> list[str]:
        p, out = self.p, []
        if st.get("fails", 0) >= p["alert_after_fails"]:
            out.append(f"deep health selhal {st['fails']}x po sobě ({m['reason'] or 'bez odpovědi'})")
        if m["errors_5xx"] >= p["fivexx_min"] and m["rate_5xx"] >= p["fivexx_ratio"]:
            out.append(f"{m['errors_5xx']} z {m['requests']} požadavků API skončilo 5xx za poslední minutu")
        if m["locked"] >= p["locked_per_min"]:
            out.append(f"{m['locked']}x 'database is locked' za minutu")
        return out

    # ------------------------------------------------------------- one probe
    def tick(self, now: float | None = None) -> dict:
        now = now or self.sen.clock()
        self.last_probe_at = now
        m = self.measure(now)
        st = self.state()
        if m["deep_ok"]:
            st["fails"], st["fail_since"] = 0, None
        else:
            st["fails"] = st.get("fails", 0) + 1
            st["fail_since"] = st.get("fail_since") or now
        reasons = self.signals(st, m)
        self.last = {**m, "at": now, "reasons": reasons, "fails": st["fails"]}
        out: dict = {"reasons": reasons, "actions": []}
        iid = st.get("incident")
        if iid is not None and self.sen.inc.get(iid) is None:
            iid = st["incident"] = None
        if reasons:
            st["ok_since"] = None
            sev = "critical" if not m["deep_ok"] else "high"
            title = "PersonalOS je degradovaný: " + "; ".join(reasons)
            iid, what = self.sen.inc.observe(Obs(SERVICE, KIND, self.p["container"], sev, title[:200], 1,
                                                 {"last": {k: m[k] for k in ("status", "reason", "errors_5xx",
                                                                             "requests", "locked", "p95_ms")}},
                                                 self.p["container"]), now)
            if what == "opened" or iid != st.get("incident"):
                st.update(incident=iid, opened_at=now, title=title[:200], alerted=False, escalated=False,
                          healed_at=None)
                self.sen.inc.note(iid, f"platform: {'; '.join(reasons)}"[:300], now)
            if not st.get("alerted"):
                self._alert(st, "alert", title, self._body(m, reasons), now)
                st["alerted"] = True
                out["actions"].append("alert")
        elif iid is not None:
            st["ok_since"] = st.get("ok_since") or now
            if now - st["ok_since"] >= self.p["recover_ok_s"]:
                took = int((now - (st.get("opened_at") or now)) / 60)
                self.sen.inc.resolve(iid, f"platform healthy for {int(now - st['ok_since'])} s", now)
                self._alert(st, "recovered", f"PersonalOS znovu odpovídá (výpadek trval ~{took} min)",
                            self._body(m, []), now)
                out["actions"].append("recovered")
                st = {"fails": 0}
        if st.get("incident") is not None and not m["deep_ok"]:
            out["actions"] += self._heal(st, m, now)
        self._save(st)
        return out

    # ------------------------------------------------------------- auto-heal
    def _heal(self, st: dict, m: dict, now: float) -> list[str]:
        p, container, iid = self.p, self.p["container"], st["incident"]
        failing_s = now - (st.get("fail_since") or now)
        if st.get("healed_at"):
            if not st.get("escalated") and now - st["healed_at"] >= p["heal_grace_s"]:
                self._escalate(st, f"restart před {int(now - st['healed_at'])} s nepomohl, deep health dál padá", m,
                               now)
                return ["escalated"]
            return []
        if failing_s < p["heal_after_s"]:
            return []
        last = self.sen.s.one("SELECT MAX(at) AS at FROM remediations WHERE container = ?", container)["at"]
        if last and now - last < 60 * p["heal_cooldown_min"]:
            if not st.get("escalated"):
                self.sen.inc.note(iid, f"platform: {container} was restarted {int((now - last) / 60)} min ago; no "
                                       f"second restart within {p['heal_cooldown_min']} min", now)
                self._escalate(st, f"{container} byl restartován před {int((now - last) / 60)} min a znovu nesmí "
                                   f"(nejvýš jednou za {p['heal_cooldown_min']} min)", m, now)
                return ["escalated"]
            return []
        logs = self._logs_around(container, now)
        try:
            self.sen.docker.restart(container)
            ok, err = True, ""
        except Exception as e:  # noqa: BLE001 - recorded and escalated
            ok, err = False, f"{type(e).__name__}: {str(e)[:200]}"
        reason = f"platform: deep health failing {int(failing_s)} s ({m['reason']})"[:300]
        self.sen.s.x("INSERT INTO remediations (container, at, reason, incident_id, ok) VALUES (?, ?, ?, ?, ?)",
                     container, now, reason, iid, int(ok))
        path = self._incident_log(container, now, ok, err, failing_s, m, logs)
        self.sen.inc.note(iid, (f"platform: restarted {container} after {int(failing_s)} s of failing deep health"
                                if ok else f"platform: restart of {container} failed: {err}") + f" (log {path.name})",
                          now)
        self.sen._alert_log(f"{_iso(now)} platform: {'restarted' if ok else 'restart failed:'} {container} {err}".rstrip())
        log.warning("platform: %s %s after %.0f s of failing deep health %s", "restarted" if ok else "could not restart",
                    container, failing_s, err)
        st["healed_at"] = now
        self._alert(st, "healed", f"sentinel restartoval {container} po {int(failing_s)} s nefunkčního deep health"
                    if ok else f"restart {container} selhal: {err}", self._body(m, [reason]), now,
                    logs="\n".join(logs)[-4000:])
        if not ok:
            self._escalate(st, f"automatický restart {container} selhal: {err}", m, now)
            return ["restart_failed", "escalated"]
        return ["restarted"]

    def _escalate(self, st: dict, why: str, m: dict, now: float) -> None:
        st["escalated"] = True
        self.sen.inc.note(st["incident"], f"platform: escalated: {why}"[:300], now)
        self._alert(st, "escalated", f"PersonalOS stále nefunguje: {why}", self._body(m, [why]), now)

    def _logs_around(self, container: str, now: float) -> list[str]:
        """The container's last minutes before the restart, redacted: every error/lock line and the tail."""
        try:
            lines = self.sen.docker.logs(container, since=now - 180, tail=2000,
                                         tty=getattr(self.sen, "_tty", {}).get(container, False))
        except Exception as e:  # noqa: BLE001 - the restart does not wait for the logs
            return [f"(logs unavailable: {type(e).__name__}: {str(e)[:120]})"]
        n = int(self.p["log_lines"])
        keep = [f"{_iso(t)} {redact(line)[:400]}" for t, line in lines
                if any(k in line for k in ("Error", "error", "locked", "Traceback", "long transaction", " 500 "))]
        tail = [f"{_iso(t)} {redact(line)[:400]}" for t, line in lines[-40:]]
        out = list(dict.fromkeys(keep[-(n - len(tail)):] + tail)) if n > len(tail) else tail
        return out[-n:]

    def _incident_log(self, container: str, now: float, ok: bool, err: str, failing_s: float, m: dict,
                      logs: list[str]) -> Path:
        d = Path(self.sen.cfg["state_dir"]) / "incident-log"
        path = d / f"{datetime.fromtimestamp(now, timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{container}.log"
        try:
            d.mkdir(parents=True, exist_ok=True)
            head = {"at": _iso(now), "container": container, "restart_ok": ok, "error": err,
                    "failing_s": int(failing_s), "deep": m}
            path.write_text(json.dumps(head, ensure_ascii=False) + "\n" + "\n".join(logs) + "\n", encoding="utf-8")
        except OSError as e:
            log.warning("platform: incident log not written: %s", e)
        return path

    # ------------------------------------------------------------- PersonalOS
    def _body(self, m: dict, reasons: list[str]) -> str:
        lines = [f"- {r}" for r in reasons]
        lines.append(f"- deep health: {'ok' if m['deep_ok'] else 'FAIL'}"
                     + (f" ({m['reason']})" if m.get("reason") and not m["deep_ok"] else "")
                     + (f", {m['ms']} ms" if m.get("ms") is not None else ""))
        lines.append(f"- poslední minuta: {m['requests']} požadavků, {m['errors_5xx']}x 5xx, p95 "
                     f"{m['p95_ms'] if m['p95_ms'] is not None else '-'} ms, {m['locked']}x 'database is locked'")
        return "\n".join(lines)

    def _alert(self, st: dict, level: str, title: str, body: str, now: float, logs: str = "") -> None:
        ref = f"sentinel:{self.sen.incident_ref(st['incident'])}"
        payload = {"ref": ref, "level": level, "title": title[:200], "body": body[:4000],
                   "incident_title": (st.get("title") or title)[:150], "container": self.p["container"],
                   "severity": "critical" if level in ("alert", "escalated") else "info",
                   "at": _iso(now), **({"logs": logs} if logs else {})}
        res = self.sen.pos.alert(payload)
        self.sen.inc.note(st["incident"], f"platform: {level} → PersonalOS {json.dumps(res, default=str)[:160]}", now)
        self.sen._alert_log(f"{_iso(now)} platform {level}: {title}"[:300])

    def status(self) -> dict:
        st = self.state()
        return {"last": {k: v for k, v in self.last.items() if k != "at"},
                "last_probe": _iso(self.last_probe_at) if self.last_probe_at else None,
                "incident": self.sen.incident_ref(st["incident"]) if st.get("incident") else None,
                "fails": st.get("fails", 0), "healed_at": _iso(st["healed_at"]) if st.get("healed_at") else None}
