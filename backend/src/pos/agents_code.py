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
NEVER_FROM_FILE = {"agents:create", "browser:use", "access:manage"}


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


def ensure_from_repo(conn: sqlite3.Connection, data_dir: Path, base: Path | None = None) -> dict:
    """Create the missing role agents from their agent.json and fill a missing
    place in the chart. Returns what changed."""
    from . import agents

    if os.environ.get("POS_AGENTS_AS_CODE", "1") == "0":
        return {"created": [], "placed": []}
    owner = Ctx(actors.owner_id(conn), via="agents-as-code")
    created, placed = [], []
    for s in specs(base):
        if s.get("enabled") is False or s.get("worker") == "none" and s.get("runtime") == "builtin":
            continue
        row = conn.execute("SELECT * FROM actors WHERE name = ?", (s["name"],)).fetchone()
        if row is None:
            perms = sorted(set(s.get("permissions") or agents.DEFAULT_AGENT_PERMISSIONS) - NEVER_FROM_FILE)
            made = agents.create_agent(conn, owner, name=s["name"], purpose=s.get("purpose") or s["name"],
                                       lifetime=s.get("lifetime", "long_lived"), permissions=perms,
                                       budget_class=s.get("budget_class", "normal"),
                                       runtime=s.get("runtime", "codex_worker"), data_dir=data_dir)
            if not made.get("created"):
                log.warning("could not create %s: %s", s["name"], made)
                continue
            row = actors.get(conn, made["agent"]["id"])
            created.append(s["name"])
        sets = {}
        if not row["role"] and s.get("role"):
            sets["role"] = s["role"]
        if not row["team"] and s.get("team"):
            sets["team"] = s["team"]
        if s.get("reports_to") and not row["is_owner"]:
            lead = actors.find_by_name(conn, s["reports_to"])
            if lead and row["reports_to"] is None and lead["id"] != row["id"]:
                sets["reports_to"] = lead["id"]
        if sets:
            versioning.update(conn, owner, "actor", row["id"], sets, action="agents_as_code")
            placed.append(s["name"])
    if created or placed:
        audit.log(conn, owner, "agents_as_code", None, None, created=created or None, placed=placed or None)
    conn.commit()
    return {"created": created, "placed": placed}


def write_worker_keys(conn: sqlite3.Connection, keys_dir: Path, base: Path | None = None) -> list[str]:
    """A valid key in <keys_dir>/<worker>/key for every agent with a worker;
    an existing valid key stays. Returns the workers that got a new key."""
    written = []
    for s in specs(base):
        worker = s.get("worker")
        if not worker or worker == "none" or s.get("enabled") is False:
            continue
        row = actors.find_by_name(conn, s["name"])
        if row is None:
            continue
        path = keys_dir / worker / "key"
        if path.exists():
            current = path.read_text(encoding="utf-8").strip()
            if current and actors.actor_for_key(conn, current) == row["id"]:
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        key = actors.create_key(conn, row["id"], label=f"worker {worker} (agents as code)")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(key + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        written.append(worker)
    conn.commit()
    return written
