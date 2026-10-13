"""The 2026-10 lean org (docs/REORG.md "2026-10: lean org"), as the owner.

    python -m pos.reorg_lean                 # dry run: what would change (nothing is written)
    python -m pos.reorg_lean --apply         # do it
    python -m pos.reorg_lean --apply --announce   # and the CEO's short announcement in #team

The owner asked (2026-10-13) to cut the roles that only coordinate, review and report, so that the
spend goes to work that reaches customers. Idempotent: a second run finds nothing left to do. Nothing
is deleted (constitution rule 3): the cut agents are archived with their history, their open work
goes to their successors (busywork such as review, chat-inbox, coaching and routine tasks is archived),
their routines and every routine but the two kept ones are archived, routing rules and project leads
move to the successors, and the kept agents get their budgets from agents/*/agent.json. Every change
is versioned and audit-logged as the owner ("via reorg_lean").
"""

import argparse
import json
import sqlite3
from pathlib import Path

from . import actors, agents, agents_code, audit, roles, routing, schedules, tasks, versioning
from .core import Ctx, now_iso

# Who takes over the work of each archived agent.
SUCCESSORS = getattr(roles, "LEAN_SUCCESSORS", None) or {
    "COO": "CEO", "Chief of Staff": "CEO", "Legal & Compliance": "CEO", "Performance Coach": "CEO",
    "CTO": "Software Engineer", "QA Reviewer": "Software Engineer", "Nexus Specialist": "Software Engineer",
    "Knowlage Specialist": "Software Engineer", "Nabu Specialist": "Software Engineer",
    "Hlídač": "SRE", "Security Engineer": "SRE",
    "Head of Growth": "Head of Customer Success", "Content & Brand": "Head of Customer Success",
    "Community Manager": "Head of Customer Success",
    "Kniha Lead": "Kniha Developer",
    "Kniha Marketing Lead": "Kniha Growth & Sales", "Kniha Content Creator": "Kniha Growth & Sales",
}
SERVICES = ("Deployer", "Knowledge agent", "Nexus", "PersonalOS")  # no worker; the CEO speaks for them
KEEP_SCHEDULES = ("CEO: ranní souhrn", "Obchod: pipeline a follow-upy")
HR_MAX_ACTIVE = 16  # the kept agents and the services, with room for one hire
BUDGET_METRICS = ("usd_day", "usd_month", "usd_run", "runs_day")
# Open work of an archived agent that only existed because of the old org: archived, not handed on.
BUSYWORK_TITLES = ("Review:", "Chat: nepřečtené", "Chat loop", "Zpětná vazba", "Upravit instrukce",
                   "Daily standup", "Souhrn pro majitele", "CEO: podklady", "CEO: pondělní", "CEO denní")
BUSYWORK_SOURCES = ("schedule:", "meeting:", "scheduler")
EXTRA_PERMISSIONS = {"Software Engineer": ["ops:observe"]}
# Queued work of the kept agents that the lean org makes moot (by ref and title, checked both).
OBSOLETE = {1271: "Kniha Developer: $", 1275: "Platformní náklady W41", 1276: "Rozpočty agentů podle reportu"}
ANNOUNCEMENT = (
    "Ahoj všichni, od dneška je firma **štíhlá**: méně rolí, žádné porady, standupy ani vzájemné recenze.\n"
    "- **CEO**: jediný kontakt Davida, všichni se hlásí přímo jemu\n"
    "- **Kniha Produkt** a **Kniha Obchod**: celý projekt Kniha (produkt je teď pozastavený, aplikaci "
    "dokončuje Davidova session)\n"
    "- **Obchod a zákazníci**: zákazníci Obsea, podpora, pipeline, LinkedIn\n"
    "- **Software Engineer**: kód PersonalOS, Nexus a knowlage\n"
    "- **SRE**: servery, incidenty, bezpečnost\n"
    "- **Home Assistant**, **CFO** (faktury, smlouvy)\n\n"
    "Měří se výsledky venku (živá funkce, odeslaný e-mail, objednávka), ne uzavřené úkoly. Výsledky se "
    "přijímají automaticky; recenzi dostane jen peníze, právo, bezpečnost a odpovědi zákazníkům."
)


def _owner(conn: sqlite3.Connection) -> Ctx:
    return Ctx(actors.owner_id(conn), via="reorg_lean")


def _active(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    row = actors.find_by_name(conn, name)
    return row if row is not None and not row["archived_at"] else None


def _cut(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return [r for r in (_active(conn, n) for n in SUCCESSORS) if r is not None]


def step_hr_limit(conn, ctx, apply: bool) -> list[str]:
    from .hr.policy import SETTING_MAX_ACTIVE, current
    from .settings_store import put

    have = current(conn).max_active_agents
    if have == HR_MAX_ACTIVE:
        return []
    if apply:
        put(conn, ctx, SETTING_MAX_ACTIVE, HR_MAX_ACTIVE, action="reorg_lean")
    return [f"hr.max_active_agents {have} -> {HR_MAX_ACTIVE}"]


def step_org(conn, ctx, apply: bool) -> list[str]:
    """Everyone kept reports to the CEO (the CEO to the owner), as its agent.json says; the services too."""
    from .org import set_org

    ceo = _active(conn, roles.CEO)
    if ceo is None:
        return ["NO active CEO: nothing moved"]
    out = []
    want = {s["name"]: s.get("reports_to") for s in agents_code.specs() if s.get("enabled") is not False}
    want.update({name: roles.CEO for name in SERVICES})
    for name, lead_name in want.items():
        row, lead = _active(conn, name), _active(conn, lead_name or "")
        if row is None or row["is_owner"] or lead is None or lead["id"] == row["id"] or row["reports_to"] == lead["id"]:
            continue
        out.append(f"org {name}: reports_to={lead_name}")
        if apply:
            if row["runtime"] == "service" or row["name"] in SERVICES:
                versioning.update(conn, ctx, "actor", row["id"], {"reports_to": lead["id"]}, action="reorg_lean")
            else:
                set_org(conn, ctx, row["id"], {"reports_to": lead["id"]})
    for row in _cut(conn):  # their open work flows to the successor when they are archived
        succ = _active(conn, SUCCESSORS[row["name"]])
        if succ is not None and row["reports_to"] != succ["id"]:
            out.append(f"org {row['name']}: reports_to={succ['name']} (before archiving)")
            if apply:
                versioning.update(conn, ctx, "actor", row["id"], {"reports_to": succ["id"]}, action="reorg_lean")
    return out


def step_permissions(conn, ctx, apply: bool) -> list[str]:
    out = []
    for name, extra in EXTRA_PERMISSIONS.items():
        row = _active(conn, name)
        if row is None:
            continue
        have = json.loads(row["permissions"] or "[]")
        missing = [p for p in extra if p not in have]
        if missing:
            out.append(f"permissions {name}: + {', '.join(missing)}")
            if apply:
                agents.set_permissions(conn, ctx, row["id"], have + missing)
    return out


def _rule_name(name: str) -> str:
    for old, new in sorted(SUCCESSORS.items(), key=lambda kv: -len(kv[0])):
        name = name.replace(old, new)
    return name


def step_routing(conn, ctx, apply: bool) -> list[str]:
    out = []
    names = {r["name"]: r["id"] for r in routing.list_rules(conn)}
    for rule in routing.list_rules(conn):
        new = SUCCESSORS.get(rule["assignee"])
        if not new:
            continue
        target = {"name": _rule_name(rule["name"]), "assignee": new}
        if names.get(target["name"], rule["id"]) != rule["id"]:
            out.append(f"rule archived (duplicate): {rule['name']}")
            if apply:
                routing.archive_rule(conn, ctx, rule["id"])
            continue
        out.append(f"rule {rule['name']} -> {target['name']}")
        if apply:
            routing.update_rule(conn, ctx, rule["id"], target)
    return out


def step_projects(conn, ctx, apply: bool) -> list[str]:
    from . import projects

    out = []
    for p in conn.execute("""SELECT p.id, p.slug, a.name AS lead FROM projects p JOIN actors a ON a.id = p.lead_id
                             WHERE p.archived_at IS NULL AND p.status != 'done'""").fetchall():
        new = SUCCESSORS.get(p["lead"])
        succ = _active(conn, new) if new else None
        if succ is None:
            continue
        out.append(f"project {p['slug']}: lead {p['lead']} -> {new}")
        if apply:
            projects.update(conn, ctx, p["id"], {"lead": succ["id"]})
    return out


def _busywork(row) -> bool:
    return (row["title"] or "").startswith(BUSYWORK_TITLES) or (row["source"] or "").startswith(BUSYWORK_SOURCES)


def step_tasks(conn, ctx, apply: bool) -> list[str]:
    from . import reassign

    out = []
    for o in _cut(conn):
        new = SUCCESSORS[o["name"]]
        n = _active(conn, new)
        if n is None:
            continue
        rows = conn.execute("""SELECT id, title, source, status FROM tasks WHERE assignee_id = ?
                               AND archived_at IS NULL AND status != 'done'""", (o["id"],)).fetchall()
        for t in rows:
            if _busywork(t):
                out.append(f"task {tasks.display_id(t['id'])} archived (busywork of {o['name']}): {t['title'][:60]}")
                if apply:
                    tasks.archive(conn, ctx, t["id"])
                continue
            out.append(f"task {tasks.display_id(t['id'])}: {o['name']} -> {new}")
            if not apply:
                continue
            try:
                reassign.reassign(conn, ctx, t["id"], n["id"], f"Štíhlá firma (2026-10): {o['name']} je archivovaný, "
                                                              f"práci převzal {new} (docs/REORG.md).", force=True)
            except tasks.Invalid:
                tasks.assign(conn, ctx, t["id"], {"type": "agent", "id": n["id"]})
            if t["status"] not in ("next", "working") and conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (t["id"],)).fetchone()["status"] != t["status"]:
                versioning.update(conn, ctx, "task", t["id"], {"status": t["status"]}, action="reorg_lean")
        rev = [r["id"] for r in conn.execute("""SELECT id FROM tasks WHERE reviewer_id = ? AND archived_at IS NULL
                                                AND status != 'done'""", (o["id"],))]
        if rev:
            out.append(f"{len(rev)} open task(s) reviewed by {o['name']} -> {new}")
            if apply:
                for tid in rev:
                    tasks.hand_review(conn, ctx, tid, n["id"], f"převzato po {o['name']} (štíhlá firma, docs/REORG.md)")
    # Chat-inbox wake-ups of anyone, queued under the old rules: the messages stay unread for the next run.
    for t in conn.execute("""SELECT id, title FROM tasks WHERE archived_at IS NULL AND status IN ('inbox', 'next')
                             AND title LIKE 'Chat: nepřečtené%'""").fetchall():
        out.append(f"task {tasks.display_id(t['id'])} archived (chat inbox): {t['title'][:60]}")
        if apply:
            tasks.archive(conn, ctx, t["id"])
    return out


def step_schedules(conn, ctx, apply: bool) -> list[str]:
    """Only two routines stay (KEEP_SCHEDULES); people's own schedules are not touched."""
    out = []
    for s in conn.execute("""SELECT s.id, s.name, a.name AS who FROM schedules s JOIN actors a ON a.id = s.assignee_id
                             WHERE s.archived_at IS NULL AND a.kind != 'human'""").fetchall():
        if s["name"] in KEEP_SCHEDULES:
            continue
        out.append(f"schedule archived: {s['name']} ({s['who']})")
        if apply:
            schedules.archive(conn, ctx, s["id"])
    return out


def step_cleanup(conn, ctx, apply: bool) -> list[str]:
    """The kept agents' queue without the old org's leftovers: tasks of archived routines, self-improvement
    rounds, command approvals for archived agents, older daily health digests and OBSOLETE; the owner's chat
    messages waiting for a paused agent go to the CEO."""
    from . import reassign

    out = []
    today = now_iso()[:10]
    rows = conn.execute("""SELECT t.id, t.title, t.source, t.created_at, a.paused_at FROM tasks t
                           JOIN actors a ON a.id = t.assignee_id
                           WHERE t.archived_at IS NULL AND t.status NOT IN ('done', 'review') AND a.kind != 'human'
                             AND a.archived_at IS NULL""").fetchall()
    archived_names = [n for n in SUCCESSORS]
    ceo = _active(conn, roles.CEO)
    for t in rows:
        title, source = t["title"] or "", t["source"] or ""
        why = ""
        if source.startswith("schedule:") and conn.execute(
                "SELECT 1 FROM schedules WHERE id = ? AND archived_at IS NOT NULL",
                (source.split(":", 1)[1],)).fetchone():
            why = "its routine is archived"
        elif source.startswith("improve"):
            why = "self-improvement round"
        elif title.startswith("Approve a command for ") and any(n in title for n in archived_names):
            why = "a command approval for an archived agent"
        elif title.startswith("Denní přehled zdraví") and t["created_at"][:10] < today:
            why = "an older daily health digest"
        elif t["id"] in OBSOLETE and title.startswith(OBSOLETE[t["id"]]):
            why = "made moot by the lean org"
        if why:
            out.append(f"task {tasks.display_id(t['id'])} archived ({why}): {title[:60]}")
            if apply:
                tasks.archive(conn, ctx, t["id"])
        elif t["paused_at"] and title.startswith("Chat: answer Owner") and ceo is not None:
            out.append(f"task {tasks.display_id(t['id'])} -> CEO (its agent is paused): {title[:60]}")
            if apply:
                reassign.reassign(conn, ctx, t["id"], ceo["id"], "Agent je pozastavený: odpovídá CEO.", force=True)
    return out


def step_budgets(conn, ctx, apply: bool) -> list[str]:
    """Each kept agent's own limits from its agent.json "budget" (the company cap stays the owner's)."""
    from .access import service as access
    from .access import store as access_store

    if not access_store.ready(conn):
        return []
    out = []
    for spec in agents_code.specs():
        row = _active(conn, spec["name"])
        budget = spec.get("budget") or {}
        if row is None or not budget:
            continue
        for metric in BUDGET_METRICS:
            want = budget.get(metric)
            if want is None or access.limit(conn, row["id"], metric) == float(want):
                continue
            out.append(f"budget {spec['name']}: {metric} {access.limit(conn, row['id'], metric)} -> {want}")
            if apply:  # quiet: one #team line for the whole reorg (the announcement), not one per limit
                now = now_iso()
                old = [r["id"] for r in conn.execute(
                    f"""SELECT id FROM access_budgets WHERE agent_id = ? AND metric = ? AND expires_at IS NULL
                        AND {access_store.ACTIVE}""", (row["id"], metric, now))]
                access._end(conn, "access_budgets", old, ctx.actor_id, "replaced", "replaced: štíhlá firma 2026-10")
                bid = access._insert_budget(conn, row["id"], metric, float(want), ctx.actor_id, "owner",
                                            "Štíhlá firma 2026-10 (docs/REORG.md): rozpočet z agent.json")
                audit.log(conn, ctx, "access_budget", "actor", row["id"], budget=bid, metric=metric, amount=want,
                          reason="reorg_lean")
    return out


def step_archive(conn, ctx, apply: bool) -> list[str]:
    out = []
    for o in _cut(conn):
        new = SUCCESSORS[o["name"]]
        if _active(conn, new) is None:
            out.append(f"NOT archived: {o['name']} (its successor {new} is not active)")
            continue
        out.append(f"archive {o['name']} (successor {new})")
        if apply:
            agents.archive_no_commit(conn, ctx, o["id"], f"Štíhlá firma 2026-10: práci převzal {new}")
    return out


STEPS = (("hr_limit", step_hr_limit), ("org", step_org), ("permissions", step_permissions),
         ("routing", step_routing), ("projects", step_projects), ("tasks", step_tasks),
         ("schedules", step_schedules), ("cleanup", step_cleanup), ("budgets", step_budgets),
         ("archive", step_archive))


def run(conn: sqlite3.Connection, apply: bool = False, announce: bool = False) -> dict:
    ctx = _owner(conn)
    report = {}
    for name, step in STEPS:
        report[name] = step(conn, ctx, apply)
        if apply:
            conn.commit()
    if apply:
        audit.log(conn, ctx, "reorg_lean", None, None, **{k: len(v) for k, v in report.items()})
        if announce:
            from . import chat

            ceo = _active(conn, roles.CEO)
            if ceo is not None:
                chat.post_to_team(conn, ceo["id"], ANNOUNCEMENT)
        conn.commit()
    return report


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(prog="python -m pos.reorg_lean", description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="write the changes (default: a dry run)")
    p.add_argument("--announce", action="store_true", help="the CEO's short announcement in #team")
    p.add_argument("--db", type=Path, default=None)
    a = p.parse_args(argv)
    conn = connect(a.db or get_settings().db_path)
    report = run(conn, a.apply, a.announce)
    for step, lines in report.items():
        print(f"## {step} ({len(lines)})")
        for line in lines:
            print(f"- {line}")
    print("APPLIED" if a.apply else "DRY RUN (nothing written; --apply to do it)")


if __name__ == "__main__":
    main()
