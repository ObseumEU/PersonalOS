"""Incidents: open, absorb, escalate, resolve.

An incident is keyed by (service, kind, key); the key is the error
fingerprint or the check's name. While it is open every further observation
is absorbed (count, last seen, the higher severity). PersonalOS hears about
it once (level 0) and again only on escalation: a higher severity or ten
times the count it was last told about. After a quiet period without
observations it resolves itself; PersonalOS hears that too if it was told
about the incident.

A quota incident whose lines name the reset ("quota exhausted until X",
detail.quota_until) is one ongoing incident: it does not resolve while the
quota is exhausted (a caller retrying once an hour would otherwise reopen it
every hour), it does not escalate on its count, and it resolves by itself
once the reset has passed without a new error after it.
"""

import json
import time
from dataclasses import dataclass, field

from .store import Store, sev_rank

HEALTH_KINDS = ("health", "unhealthy", "container_down")
RESET_GRACE_S = 300  # after a quota's reset, this long without a new error resolves it


def quota_until(row) -> float | None:
    """The reset time (epoch) of an ongoing quota incident, or None."""
    if row["kind"] != "quota":
        return None
    raw = (json.loads(row["detail"] or "{}") or {}).get("quota_until")
    if not raw:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass
class Obs:
    service: str
    kind: str
    key: str
    severity: str
    title: str
    count: int = 1
    detail: dict = field(default_factory=dict)
    container: str | None = None


class Incidents:
    def __init__(self, store: Store, thresholds: dict, clock=time.time):
        self.s, self.t, self.clock = store, thresholds, clock

    def get(self, incident_id: int):
        return self.s.one("SELECT * FROM incidents WHERE id = ?", incident_id)

    def open_for(self, service: str, kind: str, key: str):
        return self.s.one("SELECT * FROM incidents WHERE status = 'open' AND service = ? AND kind = ? AND key = ?",
                          service, kind, key)

    def observe(self, o: Obs, now: float | None = None) -> tuple[int, str]:
        """Returns (incident id, 'opened' | 'absorbed')."""
        now = now or self.clock()
        row = self.open_for(o.service, o.kind, o.key)
        if row is None:
            before = self.s.one("SELECT COUNT(*) AS n FROM incidents WHERE service = ? AND kind = ? AND key = ? "
                                "AND opened_at >= ?", o.service, o.kind, o.key, now - 86400)["n"]
            if before:
                o.detail = {**o.detail, "recurrences_24h": before}
            iid = self.s.x(
                """INSERT INTO incidents (service, kind, key, severity, title, count, opened_at, last_seen, escalate_after,
                       container, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                o.service, o.kind, o.key, o.severity, o.title[:200], o.count, now, now, now, o.container,
                json.dumps(o.detail, default=str)[:4000])
            return iid, "opened"
        sev = o.severity if sev_rank(o.severity) > sev_rank(row["severity"]) else row["severity"]
        detail = {**json.loads(row["detail"] or "{}"), **o.detail}
        self.s.x("UPDATE incidents SET count = count + ?, last_seen = ?, severity = ?, title = ?, detail = ?, "
                 "container = COALESCE(?, container) WHERE id = ?",
                 o.count, now, sev, (o.title if sev != row["severity"] else row["title"])[:200],
                 json.dumps(detail, default=str)[:4000], o.container, row["id"])
        return row["id"], "absorbed"

    def note(self, incident_id: int, text: str, now: float | None = None) -> None:
        row = self.get(incident_id)
        notes = json.loads(row["notes"] or "[]")
        notes.append({"at": now or self.clock(), "text": text[:300]})
        self.s.x("UPDATE incidents SET notes = ? WHERE id = ?", json.dumps(notes[-20:]), incident_id)

    def hold(self, incident_id: int, until: float) -> None:
        """Do not escalate before `until` (a remediation gets its chance)."""
        self.s.x("UPDATE incidents SET escalate_after = ?, remediated_at = ? WHERE id = ?", until, self.clock(),
                 incident_id)

    def due(self, now: float | None = None) -> list[tuple[str, dict]]:
        """What PersonalOS should hear now: ('incident' | 'incident_escalated', row).
        An incident held for a remediation escalates only if it was seen again
        after the hold ended."""
        now = now or self.clock()
        out = []
        for r in self.s.q("SELECT * FROM incidents WHERE status = 'open'"):
            if r["notified_level"] < 0:
                if now < (r["escalate_after"] or 0):
                    continue
                if r["remediated_at"] and r["last_seen"] < (r["escalate_after"] or 0):
                    continue  # the restart fixed it (so far)
                out.append(("incident", dict(r)))
            elif sev_rank(r["severity"]) > sev_rank(r["notified_severity"]) or (
                    r["notified_count"] and r["count"] >= 10 * r["notified_count"] and quota_until(r) is None):
                out.append(("incident_escalated", dict(r)))
        return out

    def mark_notified(self, incident_id: int, now: float | None = None) -> int:
        r = self.get(incident_id)
        level = r["notified_level"] + 1
        self.s.x("UPDATE incidents SET notified_level = ?, notified_count = ?, notified_severity = ?, "
                 "escalated_at = COALESCE(escalated_at, ?) WHERE id = ?",
                 level, max(r["count"], 1), r["severity"], now or self.clock(), incident_id)
        return level

    def quiet_s(self, kind: str) -> float:
        m = self.t["quiet_min_health"] if kind in HEALTH_KINDS else self.t["quiet_min"]
        return 60 * m

    def resolve_quiet(self, now: float | None = None) -> list[dict]:
        """Resolve the open incidents nobody observed for their quiet period."""
        now = now or self.clock()
        done = []
        for r in self.s.q("SELECT * FROM incidents WHERE status = 'open'"):
            until = quota_until(r)
            if until is not None:
                if now < until + RESET_GRACE_S:
                    continue  # still exhausted: the same ongoing incident
                if r["last_seen"] <= until + RESET_GRACE_S:
                    self.resolve(r["id"], "the quota reset has passed without a new error", now)
                    done.append(dict(self.get(r["id"])))
                    continue
            if now - r["last_seen"] >= self.quiet_s(r["kind"]):
                self.resolve(r["id"], "quiet: no new observation", now)
                done.append(dict(self.get(r["id"])))
        return done

    def resolve(self, incident_id: int, why: str, now: float | None = None, classification: str | None = None) -> None:
        now = now or self.clock()
        self.s.x("UPDATE incidents SET status = 'resolved', resolved_at = ?, classification = COALESCE(?, classification) "
                 "WHERE id = ? AND status = 'open'", now, classification, incident_id)
        self.note(incident_id, f"resolved: {why}", now)
