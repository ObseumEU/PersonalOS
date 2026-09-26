"""The 2026-09 reorganisation into a company (docs/REORG.md), as the owner.

    python -m pos.reorg                 # dry run: what would change (nothing is written)
    python -m pos.reorg --apply         # do it
    python -m pos.reorg --apply --announce   # and the CEO's short announcement in #team

Idempotent: a second run finds nothing left to do. Nothing is deleted
(constitution rule 3): the old agents are archived with their history, their
open tasks go to their successors, the old schedules and duplicate routing
rules are archived, and every change is versioned and audit-logged as the
owner ("via reorg"), who asked for it on 2026-09-26.
"""

import argparse
import json
import sqlite3
from pathlib import Path

from . import actors, agents, agents_code, audit, roles, routing, schedules, tasks, versioning
from .core import Ctx

# Who takes over which old agent's work (the Assistant is renamed in place by pos.actors).
SUCCESSORS = {old: new for old, new in roles.LEGACY.items() if old != "Assistant"}
# Members kept as they are, with a new lead (the HA specialist comes from its own agent.json).
KEEP_LEADS = {roles.ACCESS_MANAGER: roles.CEO, "Home Assistant Specialist": roles.CTO}
SERVICES_LEAD = roles.CTO  # the Deployer, knowlage and Nexus are services; engineering speaks for them
HR_LIMIT = 26  # active non-system agents during the switch (old + new) with room for a few hires
# Grants only the owner gives, part of the new roles (docs/REORG.md "Grants").
OWNER_GRANTS = [
    (roles.CFO, "tool:access_usage", "CFO: čtení útrat agentů pro report nákladů (jen čtení)"),
    ("Security Engineer", "tool:access_audit", "Security Engineer: měsíční revize přístupů (jen čtení)"),
]
OWNER_REVOKES = [
    ("Executive Assistant", "agents:create", "Nábor patří HR (Head of People), ne asistentovi"),
]
JOB_NAMES = {
    "feedback_digest": f"Repeated critique of an agent → the {roles.COACH}",
    "access_weekly": f"Access: weekly budget review ({roles.ACCESS_MANAGER})",
    "weekly_report": f"Weekly company report and board meeting ({roles.CHIEF_OF_STAFF})",
}
ANNOUNCEMENT = (
    "Ahoj všichni, jsem **CEO** a od dneška fungujeme jako firma. Nová struktura:\n"
    "- **Vedení:** CEO, Chief of Staff (týdenní report, souhrn pro Davida), Executive Assistant, "
    "COO (projekty a standup), Legal & Compliance (na vyžádání)\n"
    "- **Technika (CTO):** Software Engineer, QA Reviewer, SRE s Hlídačem, Security Engineer a specialisté "
    "na Nexus, knowlage a Home Assistant\n"
    "- **Finance a správa:** CFO, Správce přístupů\n"
    "- **Lidé:** Head of People a Performance Coach\n"
    "- **Obchod a marketing (Head of Growth):** Content & Brand, Community Manager\n"
    "- **Zákazníci:** Head of Customer Success\n\n"
    "Pravidlo: hlaste se svému vedoucímu (`org_chart`), Davida kontaktuju jen já; souhrn mu posílá Chief of "
    "Staff dvakrát denně. Staří agenti jsou archivovaní i s historií, jejich práci převzali nástupci."
)


def _owner(conn: sqlite3.Connection) -> Ctx:
    return Ctx(actors.owner_id(conn), via="reorg")


def _active(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return actors.find_by_name(conn, name)


def step_hr_limit(conn, ctx, apply: bool) -> list[str]:
    from .hr.policy import SETTING_MAX_ACTIVE, current
    from .settings_store import put

    have = current(conn).max_active_agents
    if have >= HR_LIMIT:
        return []
    if apply:
        put(conn, ctx, SETTING_MAX_ACTIVE, HR_LIMIT, action="reorg")
    return [f"hr.max_active_agents {have} -> {HR_LIMIT}"]


def step_create(conn, ctx, apply: bool, data_dir: Path) -> list[str]:
    """The same as the API's start-up (pos.main): the platform's own members under their new names
    (the COO, the Head of People), then every role from agents/*/agent.json, their grants, the
    Hlídač's rules and keys for the pool."""
    import os

    missing = [s["name"] for s in agents_code.specs()
               if s.get("enabled") is not False and not conn.execute(
                   "SELECT 1 FROM actors WHERE name = ?", (s["name"],)).fetchone()]
    if apply:
        from . import chat, integrations, monitor, observability, workers
        from .access import service as access

        integrations.register_builtin_agents(conn)
        workers.mark_services(conn)
        agents_code.ensure_from_repo(conn, data_dir)
        access.seed(conn)
        monitor.ensure(conn)
        observability.ensure(conn)
        chat.ensure_team_channel(conn)
        if os.environ.get("POS_WORKER_KEYS_DIR"):
            agents_code.write_worker_keys(conn, Path(os.environ["POS_WORKER_KEYS_DIR"]))
    return [f"create {n}" for n in missing]


def step_org(conn, ctx, apply: bool) -> list[str]:
    """Every role sits where its agent.json says (the file wins once, now), the kept members get
    their new lead, the services hang under engineering."""
    from .org import set_org

    out = []
    want: dict[str, dict] = {}
    for s in agents_code.specs():
        if s.get("enabled") is False:
            continue
        want[s["name"]] = {k: s[k] for k in ("role", "team", "reports_to") if s.get(k)}
    for name, lead in KEEP_LEADS.items():
        want.setdefault(name, {})["reports_to"] = lead
    for name in ("Deployer", "Knowledge agent", "Nexus"):
        want.setdefault(name, {})["reports_to"] = SERVICES_LEAD
    for name, fields in want.items():
        row = _active(conn, name)
        if row is None or row["is_owner"]:
            continue
        changes = {}
        for key in ("role", "team"):
            if fields.get(key) and row[key] != fields[key]:
                changes[key] = fields[key]
        if fields.get("reports_to"):
            lead = _active(conn, fields["reports_to"])
            if lead is not None and lead["id"] != row["id"] and row["reports_to"] != lead["id"]:
                changes["reports_to"] = lead["id"]
        if changes:
            out.append(f"org {name}: " + ", ".join(f"{k}={v if k != 'reports_to' else fields['reports_to']}"
                                                   for k, v in changes.items()))
            if apply:
                if row["runtime"] == "service":  # not in the chart: a plain versioned change
                    versioning.update(conn, ctx, "actor", row["id"], changes, action="reorg")
                else:
                    set_org(conn, ctx, row["id"], changes)
    return out


def _new_rule_name(name: str) -> str:
    name = name.replace("Mail agent triage", "Customer Success triage")
    for old, new in sorted(roles.LEGACY.items(), key=lambda kv: -len(kv[0])):
        if old in name:
            name = name.replace(old, new)
    return name


def step_routing(conn, ctx, apply: bool) -> list[str]:
    out = []
    by_name = {r["name"]: r for r in routing.list_rules(conn)}
    for rule in routing.list_rules(conn):
        name, assignee = rule["name"], rule["assignee"]
        if rule["name"] == routing.NEXUS_RULE:  # invoices: recorded by the CFO (Nexus is a service)
            target = {"name": routing.INVOICE_RULE, "assignee": roles.CFO, "priority": 2, "enabled": True}
        elif assignee in SUCCESSORS:
            new_name = _new_rule_name(name)
            if new_name == name and "Customer Success" not in name:
                new_name = f"{name} ({SUCCESSORS[assignee]})"
            target = {"name": new_name, "assignee": SUCCESSORS[assignee]}
        else:
            continue
        twin = by_name.get(target["name"])
        if twin is not None and twin["id"] != rule["id"]:  # created at start-up under the new name already
            out.append(f"rule archived (duplicate of #{twin['id']}): {name}")
            if apply:
                routing.archive_rule(conn, ctx, rule["id"])
            continue
        out.append(f"rule {name} -> {target['name']} ({target['assignee']})")
        if apply:
            routing.update_rule(conn, ctx, rule["id"], target)
    return out


def step_schedules(conn, ctx, apply: bool) -> list[str]:
    """The old agents' routines end; the new roles bring their own (agent.json, the COO's standup)."""
    out = []
    for old in SUCCESSORS:
        row = conn.execute("SELECT id FROM actors WHERE name = ?", (old,)).fetchone()
        if row is None:
            continue
        for s in conn.execute("SELECT id, name FROM schedules WHERE assignee_id = ? AND archived_at IS NULL",
                              (row["id"],)).fetchall():
            out.append(f"schedule archived: {s['name']} ({old})")
            if apply:
                schedules.archive(conn, ctx, s["id"])
    return out


def step_tasks(conn, ctx, apply: bool) -> list[str]:
    from . import reassign

    out = []
    for old, new in SUCCESSORS.items():
        o = conn.execute("SELECT id FROM actors WHERE name = ? AND archived_at IS NULL", (old,)).fetchone()
        n = _active(conn, new)
        if o is None or n is None:
            continue
        open_ids = [r["id"] for r in conn.execute(
            """SELECT id FROM tasks WHERE assignee_id = ? AND archived_at IS NULL
               AND status NOT IN ('done', 'someday')""", (o["id"],))]
        for tid in open_ids:
            out.append(f"task {tasks.display_id(tid)}: {old} -> {new}")
            if not apply:
                continue
            status = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"]
            try:
                reassign.reassign(conn, ctx, tid, n["id"], f"Reorganizace: {old} je archivovaný, práci "
                                                           f"převzal {new} (docs/REORG.md).", force=True)
            except tasks.Invalid:
                tasks.assign(conn, ctx, tid, {"type": "agent", "id": n["id"]})
            # Only queued work stays queued: a result waiting for review (or a task waiting for someone)
            # keeps its state; the successor must not redo it (2026-09-26, the first run re-queued them).
            if status not in ("next", "working") and conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()["status"] != status:
                versioning.update(conn, ctx, "task", tid, {"status": status}, action="reorg")
        rev = conn.execute("SELECT COUNT(*) FROM tasks WHERE reviewer_id = ? AND archived_at IS NULL "
                           "AND status NOT IN ('done', 'someday')", (o["id"],)).fetchone()[0]
        if rev:
            out.append(f"{rev} open task(s) reviewed by {old} -> {new}")
            if apply:
                conn.execute("UPDATE tasks SET reviewer_id = ? WHERE reviewer_id = ? AND archived_at IS NULL "
                             "AND status NOT IN ('done', 'someday')", (n["id"], o["id"]))
    return out


def _holds(conn, agent_id: int, cap: str) -> bool:
    from .access import store as access_store
    from .core import now_iso

    return conn.execute(f"SELECT 1 FROM access_grants WHERE agent_id = ? AND capability = ? AND {access_store.ACTIVE}",
                        (agent_id, cap, now_iso())).fetchone() is not None


def step_grants(conn, ctx, apply: bool) -> list[str]:
    from .access import service as access
    from .access import store as access_store

    if not access_store.ready(conn):
        return []
    out = []
    for name, cap, why in OWNER_GRANTS:
        row = _active(conn, name)
        if row is None or _holds(conn, row["id"], cap):
            continue
        out.append(f"grant {name}: {cap}")
        if apply:
            access.grant(conn, ctx, row["id"], cap, why)
    for name, cap, why in OWNER_REVOKES:
        row = _active(conn, name)
        if row is None or not _holds(conn, row["id"], cap):
            continue
        out.append(f"revoke {name}: {cap}")
        if apply:
            access.revoke(conn, ctx, row["id"], cap, why)
    return out


def step_jobs(conn, ctx, apply: bool) -> list[str]:
    out = []
    for action, name in JOB_NAMES.items():
        row = conn.execute("SELECT id, name FROM jobs WHERE action = ?", (action,)).fetchone()
        if row and row["name"] != name:
            out.append(f"job {row['name']} -> {name}")
            if apply:
                conn.execute("UPDATE jobs SET name = ? WHERE id = ?", (name, row["id"]))
    return out


def step_archive(conn, ctx, apply: bool) -> list[str]:
    out = []
    for old, new in SUCCESSORS.items():
        o = conn.execute("SELECT id FROM actors WHERE name = ? AND archived_at IS NULL", (old,)).fetchone()
        if o is None:
            continue
        if _active(conn, new) is None:
            out.append(f"NOT archived yet: {old} (its successor {new} does not exist)")
            continue
        out.append(f"archive {old} (successor {new})")
        if apply:
            agents.archive_no_commit(conn, ctx, o["id"], f"Reorganizace 2026-09: nahrazen rolí {new}")
    return out


def step_channels(conn, ctx, apply: bool) -> list[str]:
    from . import chat, weekly

    if apply:
        from . import org

        chat.ensure_team_channel(conn)
        weekly.channel_id(conn)
        org.ensure_standup(conn)  # the COO's standup (the old PM's is archived)
        conn.commit()
    return []


STEPS = (("hr_limit", step_hr_limit), ("org", step_org), ("routing", step_routing),
         ("schedules", step_schedules), ("tasks", step_tasks), ("grants", step_grants), ("jobs", step_jobs),
         ("archive", step_archive), ("channels", step_channels))


def run(conn: sqlite3.Connection, data_dir: Path, apply: bool = False) -> dict:
    ctx = _owner(conn)
    if apply:
        actors.ensure_builtin(conn)  # renames the Assistant in place (once)
    report: dict[str, list[str]] = {"hr_limit": step_hr_limit(conn, ctx, apply)}
    report["create"] = step_create(conn, ctx, apply, data_dir)
    for key, fn in STEPS[1:]:
        report[key] = fn(conn, ctx, apply)
    if apply:
        audit.log(conn, ctx, "reorg", None, None, **{k: len(v) for k, v in report.items()})
        conn.commit()
    else:
        conn.rollback()
    return report


def announce(conn: sqlite3.Connection) -> dict:
    from . import chat

    ceo = _active(conn, roles.CEO)
    if ceo is None:
        raise SystemExit("no CEO yet")
    out = chat.post_to_team(conn, ceo["id"], ANNOUNCEMENT)
    conn.commit()
    return out


def main() -> None:
    from .config import get_settings
    from .db import connect

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: a dry run)")
    ap.add_argument("--announce", action="store_true", help="post the CEO's announcement in #team (with --apply)")
    a = ap.parse_args()
    settings = get_settings()
    conn = connect(settings.db_path)
    report = run(conn, settings.data_dir, apply=a.apply)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if a.apply and a.announce:
        print(json.dumps({"announced": bool(announce(conn))}, ensure_ascii=False))


if __name__ == "__main__":
    main()
