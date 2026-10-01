"""Runbook remediation without a model: restart an allowlisted stateless
container when its health check fails, at most once per cooldown per
container. Databases, queues and volumes are never touched: only the
containers named in runbook.restart_allowlist, only `docker restart`.
A container stopped from outside (exit 143/137, e.g. the deployer stopped by its
own deploy, T-462) is started again if it is on runbook.start_allowlist."""

import json

from .incidents import Incidents
from .store import Store

REMEDIABLE = ("health", "unhealthy")
STOPPED_EXIT = (143, 137)  # SIGTERM / SIGKILL: stopped by docker stop or a recreate, not a crash


def maybe_restart(s: Store, inc: Incidents, cfg: dict, incident_id: int, docker, now: float | None = None) -> str:
    """Try the runbook for a newly opened incident. Returns what happened:
    'restarted', 'cooldown', 'not_allowed', 'not_remediable' or 'failed'."""
    now = now or inc.clock()
    rb = cfg["runbook"]
    row = inc.get(incident_id)
    container = row["container"]
    if not container:
        return "not_remediable"
    if row["kind"] == "container_down":
        exit_code = (json.loads(row["detail"] or "{}") or {}).get("exit_code")
        if exit_code not in STOPPED_EXIT or container not in rb.get("start_allowlist", []):
            return "not_remediable"
    elif row["kind"] not in REMEDIABLE:
        return "not_remediable"
    elif container not in rb["restart_allowlist"]:
        inc.note(incident_id, f"runbook: {container} is not on the restart allowlist", now)
        return "not_allowed"
    last = s.one("SELECT MAX(at) AS at FROM remediations WHERE container = ?", container)["at"]
    if last and now - last < 60 * rb["cooldown_min"]:
        inc.note(incident_id, f"runbook: {container} was restarted {int((now - last) / 60)} min ago; "
                              f"no second restart within {rb['cooldown_min']} min", now)
        return "cooldown"
    try:
        docker.restart(container)
        ok = True
    except Exception as e:  # noqa: BLE001 - recorded, and the incident escalates as usual
        ok = False
        inc.note(incident_id, f"runbook: restart of {container} failed: {str(e)[:160]}", now)
    s.x("INSERT INTO remediations (container, at, reason, incident_id, ok) VALUES (?, ?, ?, ?, ?)",
        container, now, f"{row['kind']}: {row['title']}"[:300], incident_id, int(ok))
    if not ok:
        return "failed"
    inc.note(incident_id, f"runbook: restarted {container}", now)
    inc.hold(incident_id, now + rb["grace_s"])
    return "restarted"
