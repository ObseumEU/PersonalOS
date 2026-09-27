"""Agents as code (REVIZE-FUNKCI 3.7): every role agent is described in git by
`agents/<slug>/agent.json` (name, role, team, reports_to, permissions,
budget_class, lifetime, runtime, worker, enabled).

At start the core makes sure each enabled one exists (idempotent): it creates
a missing agent on the owner's behalf and fills a missing role, team or lead.
It never changes an existing agent's permissions (those are the owner's), and
a file never grants agents:create or browser:use (the owner grants those).

Worker keys: when POS_WORKER_KEYS_DIR is set (on the server a volume shared
with the worker containers, one subdirectory per worker), the core writes a
valid key to <dir>/<worker>/key for every agent with a worker, so no one has
to copy keys into .env. A worker reads it when POS_AGENT_KEY is empty.

POS_AGENTS_AS_CODE=0 turns the start-up creation off (tests create their own).
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

from . import actors, audit, versioning
from .core import Ctx

log = logging.getLogger(__name__)
# The owner grants these (access:manage: the Access manager's own, created by pos.access).
NEVER_FROM_FILE = {"agents:create", "browser:use", "access:manage",
                   # the browser and the desktop come from grants (pos.browser.ensure_grants, the Access manager)
                   "tool:browser", "tool:computer"}


def repo_dir() -> Path | None:
    if os.environ.get("POS_AGENTS_REPO_DIR"):
        return Path(os.environ["POS_AGENTS_REPO_DIR"])
    from .tools import repo_root

    root = repo_root()
    return root / "agents" if root else None


def specs(base: Path | None = None) -> list[dict]:
    base = base or repo_dir()
    out = []
    if base is None or not base.is_dir():
        return out
    for f in sorted(base.glob("*/agent.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as e:
            log.warning("skipping %s: %s", f, e)
            continue
        if d.get("name"):
            out.append({**d, "slug": f.parent.name})
    return out


def answers_chat(name: str) -> bool:
    """Does this agent answer people in chat (agent.json "answers_chat": true)?"""
    return any(s.get("answers_chat") and s["name"] == name for s in specs())


def has_worker(name: str) -> bool:
    """Does this agent run in a worker of its own here (agent.json runtime codex_worker, enabled)?
    Such an agent always answers the owner in chat (pos.chat._ask_to_answer)."""
    return any(s["name"] == name and s.get("runtime", "codex_worker") == "codex_worker"
               and s.get("worker") not in (None, "none") and s.get("enabled") is not False for s in specs())


def ensure_from_repo(conn: sqlite3.Connection, data_dir: Path, base: Path | None = None) -> dict:
    """Create the missing role agents from their agent.json and fill a missing
    place in the chart. Returns what changed."""
    from . import agents

    if os.environ.get("POS_AGENTS_AS_CODE", "1") == "0":
        return {"created": [], "placed": []}
    owner = Ctx(actors.owner_id(conn), via="agents-as-code")
    created, placed = [], []
    wanted = [s for s in specs(base)
              if not (s.get("enabled") is False or s.get("worker") == "none" and s.get("runtime") == "builtin")]
    # Pass 1: create every missing agent, so pass 2 finds each lead whatever the folder order.
    from .hr import service as hr

    over = []
    for s in wanted:
        if conn.execute("SELECT 1 FROM actors WHERE name = ?", (s["name"],)).fetchone():
            continue
        if hr.room_for_agents(conn) <= 0:  # no HR decision (no approval per agent, no replacement)
            over.append(s["name"])
            continue
        perms = sorted(p for p in set(s.get("permissions") or agents.DEFAULT_AGENT_PERMISSIONS) - NEVER_FROM_FILE
                       if not str(p).startswith("scope:browser"))
        # A role from git never makes HR archive someone else to fit under the limit: over it, the
        # agent is not created (the owner raises hr.max_active_agents; docs/REORG.md).
        made = agents.create_agent(conn, owner, name=s["name"], purpose=s.get("purpose") or s["name"],
                                   lifetime=s.get("lifetime", "long_lived"), permissions=perms,
                                   budget_class=s.get("budget_class", "normal"),
                                   runtime=s.get("runtime", "codex_worker"), data_dir=data_dir, allow_replace=False)
        if not made.get("created"):
            log.warning("could not create %s: %s", s["name"], made)
            continue
        row = actors.get(conn, made["agent"]["id"])
        created.append(s["name"])
        if s.get("engine") or s.get("model"):  # its runtime from the file, once (then the owner's)
            versioning.update(conn, owner, "actor", row["id"], {"engine": s.get("engine"), "model": s.get("model")},
                              action="agents_as_code")
    # Pass 2: its place in the chart. A missing role, team or lead is filled; an agent created just now
    # takes the file's lead (not the COO default it got at creation). Later changes are the owner's.
    for s in wanted:
        row = conn.execute("SELECT * FROM actors WHERE name = ?", (s["name"],)).fetchone()
        if row is None:
            continue
        sets = {}
        if not row["role"] and s.get("role"):
            sets["role"] = s["role"]
        if not row["team"] and s.get("team"):
            sets["team"] = s["team"]
        if s.get("reports_to") and not row["is_owner"] and not row["archived_at"]:
            lead = actors.find_by_name(conn, s["reports_to"])
            if lead and lead["id"] != row["id"] and row["reports_to"] != lead["id"] \
                    and (row["reports_to"] is None or s["name"] in created or not _lead_set_by_hand(conn, row["id"])):
                sets["reports_to"] = lead["id"]
        if sets:
            versioning.update(conn, owner, "actor", row["id"], sets, action="agents_as_code")
            placed.append(s["name"])
        if _adopt_worker(conn, owner, row, s):
            placed.append(f"{s['name']} (worker)")
        _budget_from_file(conn, owner, row, s)
        _grants_from_file(conn, owner, row, s)
        _schedules_from_file(conn, owner, row, s)
    if over:
        log.warning("over the HR limit (hr.max_active_agents), not created yet: %s", ", ".join(over))
    if created or placed or over:
        audit.log(conn, owner, "agents_as_code", None, None, created=created or None, placed=placed or None,
                  over_limit=over or None)
    conn.commit()
    return {"created": created, "placed": placed, "over_limit": over}


def _adopt_worker(conn: sqlite3.Connection, owner: Ctx, row: sqlite3.Row, s: dict) -> bool:
    """A member that ran inside the core (runtime builtin) and now has a worker in
    its file (the HR agent, 2026-09-26): its runtime follows the file, and its
    engine and model when it has none yet (after that they are the owner's)."""
    if s.get("runtime", "codex_worker") != "codex_worker" or s.get("worker") in (None, "none")             or s.get("enabled") is False or row["runtime"] not in ("builtin",):
        return False
    sets = {"runtime": "codex_worker"}
    if not row["engine"] and not row["model"] and (s.get("engine") or s.get("model")):
        sets.update({"engine": s.get("engine"), "model": s.get("model")})
    versioning.update(conn, owner, "actor", row["id"], sets, action="agents_as_code")
    return True


def _budget_from_file(conn: sqlite3.Connection, owner: Ctx, row: sqlite3.Row, s: dict) -> None:
    """agent.json "budget" ({metric: amount}): the agent's own limits, set once while
    it has none (then they are the Access manager's and the owner's)."""
    from .access import service as access
    from .access import store as access_store

    budget = s.get("budget") or {}
    if not budget or not access_store.ready(conn) or conn.execute(
            "SELECT 1 FROM access_budgets WHERE agent_id = ?", (row["id"],)).fetchone():
        return
    for metric, amount in budget.items():
        if metric in access.METRICS:
            access._insert_budget(conn, row["id"], metric, float(amount), owner.actor_id, "platform",
                                  f"{s['name']}: výchozí rozpočet z agent.json; mění Správce přístupů nebo majitel")
    audit.log(conn, owner, "access_budget", "actor", row["id"], budget="agent.json", **budget)


# What agent.json "grants" may hand out: single pos tools only (tool:<name>), never an owner-only
# capability (credentials, access, the guard), never a permission group or outbound.
FILE_GRANT_KINDS = ("tool",)


def _grants_from_file(conn: sqlite3.Connection, owner: Ctx, row: sqlite3.Row, s: dict) -> list[str]:
    """agent.json "grants" (e.g. ["tool:knowledge"]): each is granted once, by the platform. A grant the
    agent had before (active, revoked or expired) is never given again: a revoke stays a revoke."""
    from .access import service as access
    from .access import store as access_store

    wanted = [str(c).strip() for c in s.get("grants") or [] if str(c).strip()]
    if not wanted or row["archived_at"] or not access_store.ready(conn) or not access_store.seeded(conn, row["id"]):
        return []
    made = []
    for cap in wanted:
        if cap in NEVER_FROM_FILE:  # the browser and the desktop: pos.browser seeds them, then the Access manager
            log.warning("%s: agent.json grant %s is never given from a file; ignored", s["name"], cap)
            continue
        try:
            if access.kind_of(cap) not in FILE_GRANT_KINDS:
                log.warning("%s: agent.json grant %s is not a tool grant; ignored", s["name"], cap)
                continue
        except access.AccessError as e:
            log.warning("%s: agent.json grant %s: %s", s["name"], cap, e)
            continue
        if conn.execute("SELECT 1 FROM access_grants WHERE agent_id = ? AND capability = ?",
                        (row["id"], cap)).fetchone():
            continue
        access._insert_grant(conn, row["id"], cap, owner.actor_id, "platform",
                             f"{s['name']}: z agent.json (role potřebuje tento nástroj); odebírá majitel "
                             "nebo Správce přístupů")
        made.append(cap)
    if made:
        access.refresh_cache(conn, row["id"])
        audit.log(conn, owner, "access_grant", "actor", row["id"], capabilities=made, source="agent.json")
    return made


def grants_from_repo(conn: sqlite3.Connection, base: Path | None = None) -> dict:
    """Every agent's agent.json "grants", once each (after pos.access has seeded the agents)."""
    if os.environ.get("POS_AGENTS_AS_CODE", "1") == "0":
        return {}
    owner = Ctx(actors.owner_id(conn), via="agents-as-code")
    out = {}
    for s in specs(base):
        row = conn.execute("SELECT * FROM actors WHERE name = ?", (s["name"],)).fetchone()
        if row is not None and (made := _grants_from_file(conn, owner, row, s)):
            out[s["name"]] = made
    conn.commit()
    return out


def _lead_set_by_hand(conn: sqlite3.Connection, actor_id: int) -> bool:
    """Someone moved this member in the chart (set_org by the owner or a lead, or pos.reorg): the
    file's lead no longer applies. A lead from the defaults (the COO) is not a choice by hand."""
    return any(h["action"] in ("set_org", "reorg") for h in versioning.history(conn, "actor", actor_id))


SCHEDULE_FIELDS = ("name", "schedule", "title", "notes", "definition_of_done", "priority", "topic", "estimate_min")


def _schedules_from_file(conn: sqlite3.Connection, owner: Ctx, row: sqlite3.Row, s: dict) -> list[str]:
    """agent.json "schedules": the role's routines, created once as the owner's team schedules
    (assigned to the agent). One that exists under the same name for the agent, even archived or
    paused, is left alone: after creation a schedule is its lead's and the owner's to change."""
    from . import schedules

    made = []
    if row["archived_at"] or row["paused_at"]:
        return made
    for item in s.get("schedules") or []:
        if not item.get("name") or not item.get("schedule"):
            continue
        if conn.execute("SELECT 1 FROM schedules WHERE name = ? AND assignee_id = ?",
                        (item["name"], row["id"])).fetchone():
            continue
        try:
            schedules.create(conn, owner, {**{k: item[k] for k in SCHEDULE_FIELDS if k in item},
                                           "visibility": "team", "assignee": {"type": "agent", "id": row["id"]}})
            made.append(item["name"])
        except Exception as e:  # noqa: BLE001 - a bad schedule in a file must not stop the start-up
            log.warning("schedule %s for %s not created: %s", item.get("name"), s["name"], e)
    return made


def spec_of(name: str, base: Path | None = None) -> dict | None:
    return next((s for s in specs(base) if s["name"] == name), None)


def is_dormant(name: str) -> bool:
    """A role kept for when it is needed (agent.json "dormant": true): no routine, and HR never
    proposes to archive it for being idle."""
    s = spec_of(name)
    return bool(s and s.get("dormant"))


# What agent.json "profile" may set for the agent's worker (pos_worker reads it from /api/worker/me);
# it overrides the worker's environment for this agent only (the agent pool serves many agents).
PROFILE_KEYS = {"pos_tools", "claude_tools", "claude_builtin", "claude_disallowed", "max_usd_run", "max_steps",
                "max_steps_owner", "workdir"}
WORKDIR_ROOTS = ("/work/", "/repos/")
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def worker_profile(name: str, base: Path | None = None) -> dict:
    """The worker settings from the agent's file: effort and "profile" (only the known keys; a
    workdir only under /work or /repos)."""
    s = spec_of(name, base)
    if not s:
        return {}
    out = {k: v for k, v in (s.get("profile") or {}).items() if k in PROFILE_KEYS}
    if "workdir" in out and not (isinstance(out["workdir"], str) and out["workdir"].startswith(WORKDIR_ROOTS)
                                 and ".." not in out["workdir"]):
        out.pop("workdir")
    if s.get("effort") in EFFORTS:
        out["effort"] = s["effort"]
    return out


def write_worker_keys(conn: sqlite3.Connection, keys_dir: Path, base: Path | None = None) -> list[str]:
    """A valid key in <keys_dir>/<worker>/key for every agent with a worker;
    an existing valid key stays. The agents without a worker of their own get
    one in the agent pool (pos.workers). Returns the workers that got a new key."""
    from . import workers

    written = []
    for s in specs(base):
        worker = s.get("worker")
        if not worker or worker in ("none", workers.POOL) or s.get("enabled") is False:
            continue  # (pool agents: sync_pool_keys below)
        row = actors.find_by_name(conn, s["name"])
        if row is None:
            continue
        if workers.write_key(conn, keys_dir / worker / "key", row["id"], f"worker {worker} (agents as code)"):
            written.append(worker)
    conn.commit()
    written += workers.sync_pool_keys(conn, keys_dir, base).get("written", [])
    return written
