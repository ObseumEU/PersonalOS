"""Agents as users (AGENTS-SPEC 3): creation with limits, permissions, pause,
stop, messages and archiving.

Creating an agent goes through three independent checks:
1. the constitution (pos.guard): an agent never gets more permissions than
   its creator has (U5);
2. the HR agent (pos.hr): the limits from spec 3.2 and what to do when they
   are hit (reuse, replace, defer, ask the owner);
3. the budget keeper (pos.budget): the agent's budget class.

Purpose, lifetime and expiry are kept by pos.hr (hr_agent_profiles).
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import actors, audit, roles, runner, tasks, versioning
from .core import Ctx, NotFound, now_iso
from .core import Forbidden as _Forbidden

# Permission vocabulary. The owner implicitly has all of them ("*").
PERMISSIONS = {
    "tasks:read": "see tasks in the team and public layers",
    "tasks:write": "create and change tasks",
    "tasks:claim": "take tasks from the queue, report progress, hand in results",
    "tasks:review": "review colleagues' results (accept or return) where you are the reviewer or their lead",
    "approvals:request": "send outbound work (request_outbound) and ask the owner to approve money, commitments "
                         "and posts on his personal channels",
    "agents:create": "create new agents (within limits, never with more permissions)",
    "messages:send": "message other members",
    "events:emit": "report incoming events from a connector (e-mail, Discord, GitHub)",
    "routes:write": "change event routing rules",
    "hr:read": "read HR's roster, scores and proposals (hr_overview)",
    "browser:use": "drive a web browser (paying, signing, deleting, account settings and the owner's personal channels "
                  "still need approval)",
    "access:manage": "decide other agents' grants and budgets (the Access manager; only the owner grants it)",
    "ops:monitor": "read the sentinel's incident logs and close incidents (the Monitor agent)",
    "ops:observe": "query Loki logs (<=1 h, <=200 lines, redacted) and a Prometheus metrics snapshot (the Monitor agent)",
}
BUILTIN_PERMISSIONS = {
    actors.ASSISTANT_NAME: ["tasks:read", "tasks:write", "tasks:claim", "approvals:request", "agents:create",
                            "messages:send"],
    "Knowledge agent": ["tasks:read", "tasks:claim", "approvals:request"],
    "Nexus": ["tasks:read", "tasks:write", "tasks:claim", "approvals:request"],
    roles.HR: ["tasks:read", "tasks:claim", "tasks:write", "messages:send", "approvals:request", "hr:read"],
    "Deployer": ["tasks:read", "tasks:write"],
    roles.COO: ["approvals:request", "messages:send", "tasks:claim", "tasks:read", "tasks:write"],
}
DEFAULT_AGENT_PERMISSIONS = ["tasks:read", "tasks:claim", "approvals:request"]
DEFAULT_AGENT_MODEL = "claude-opus-5-5"  # every new agent (pos.hiring.DEFAULT_MODEL too)
# HR edits anyone's instructions except these (and the owner-only areas).
HR_HANDS_OFF = (roles.CEO, roles.ACCESS_MANAGER)
LIFETIMES = ("one_shot", "long_lived")
BUDGET_CLASSES = ("system", "normal", "low")

versioning.register("actor", "actors")


class AgentError(ValueError):
    pass


def permissions_of(conn: sqlite3.Connection, actor_id: int) -> set[str]:
    """The owner has everything; an agent has its active grants (pos.access),
    or actors.permissions while it is not managed there yet."""
    row = actors.get(conn, actor_id)
    if row["is_owner"]:
        return {"*"}
    if row["kind"] != "human":
        from .access import service as access

        granted = access.effective(conn, actor_id)
        if granted is not None:
            return granted
    return set(json.loads(row["permissions"] or "[]"))


def has_permission(conn: sqlite3.Connection, actor_id: int, perm: str) -> bool:
    row = actors.get(conn, actor_id)
    if row["kind"] == "human":
        return True
    have = permissions_of(conn, actor_id)
    return "*" in have or perm in have


def require(conn: sqlite3.Connection, ctx: Ctx, perm: str) -> None:
    if not has_permission(conn, ctx.actor_id, perm):
        raise _Forbidden(f"missing permission {perm} (not granted; ask the Access manager with request_access)")


def seed_builtin_permissions(conn: sqlite3.Connection) -> None:
    for name, perms in BUILTIN_PERMISSIONS.items():
        conn.execute("UPDATE actors SET permissions = ? WHERE name = ? AND permissions = '[]'",
                     (json.dumps(perms), name))
    # Permissions added to an agent later (platform code, not an agent's own
    # change): hr:read for the HR agent, which hr_overview requires; messages:send
    # for the role agents, so they can answer the standup and colleagues in chat.
    for name, perm in ((roles.HR, "hr:read"), (roles.ENGINEER, "messages:send"), (roles.COACH, "messages:send"),
                       (roles.COO, "tasks:review"), (roles.HR, "routes:write"),
                       (roles.COACH, "routes:write"), (roles.COO, "routes:write"),
                       # HR runs in a worker (2026-09-26): it claims its tasks, answers in chat
                       (roles.HR, "tasks:claim"), (roles.HR, "messages:send")):
        row = conn.execute("SELECT id, permissions FROM actors WHERE name = ?", (name,)).fetchone()
        if row and perm not in json.loads(row["permissions"] or "[]"):
            conn.execute("UPDATE actors SET permissions = ? WHERE id = ?",
                         (json.dumps(sorted({*json.loads(row["permissions"] or "[]"), perm})), row["id"]))
    conn.commit()
    from .access import service as access
    from .access import store as access_store

    if access_store.ready(conn):
        access.seed(conn)  # the grants follow (never re-granting one that was revoked)


def _slug(name: str) -> str:
    # Accents fold to their letters ("Asistent vedení" -> asistent-vedeni), so a Czech name keeps its folder.
    import unicodedata

    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-") or "agent"


def repo_instructions(name: str) -> Path | None:
    """Instructions kept in git (agents/<name>/INSTRUCTIONS.md) win over the
    copy written at creation: agents change them through commits, and the
    deployer checks and ships those like any other change (AGENTS-SPEC 6)."""
    root = os.environ.get("POS_AGENTS_REPO_DIR")
    if not root:
        return None
    p = Path(root) / _slug(name) / "INSTRUCTIONS.md"
    return p if p.exists() else None


def is_seeded(name: str) -> bool:
    """A role agent defined in git (agents/<slug>/): HR never archives it on its own."""
    root = os.environ.get("POS_AGENTS_REPO_DIR")
    if root:
        base = Path(root)
    else:
        from .tools import repo_root

        top = repo_root()
        if top is None:
            return False
        base = top / "agents"
    return (base / _slug(name)).is_dir()


def instructions_of(row) -> str | None:
    p = repo_instructions(row["name"])
    if p is None and row["instructions_path"] and Path(row["instructions_path"]).exists():
        p = Path(row["instructions_path"])
    return p.read_text(encoding="utf-8") if p else None


def _write_instructions(data_dir: Path, name: str, text: str) -> str:
    # The first version lives with the data; a version in git overrides it.
    folder = data_dir / "agents" / _slug(name)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "INSTRUCTIONS.md"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return str(path)


def create_agent(conn: sqlite3.Connection, ctx: Ctx, *, name: str, purpose: str, lifetime: str = "one_shot",
                 instructions: str = "", permissions: list[str] | None = None, budget_class: str = "normal",
                 expires_at: str | None = None, runtime: str = "codex_worker", a2a_url: str | None = None,
                 data_dir: Path, allow_replace: bool = True) -> dict:
    """Create an agent. Returns {"created": True, "agent": ..., "api_key": ...} or,
    when HR stops it at a limit, {"created": False, **HR's decision}. With
    allow_replace=False HR may not archive another agent to make room (agents
    from git): over the limit the agent is simply not created."""
    from .guard import policy
    from .hr import service as hr
    from .integrations import guard_actor

    name = name.strip()
    if not name or not purpose.strip():
        raise AgentError("an agent needs a name and a purpose")
    if lifetime not in LIFETIMES:
        raise AgentError(f"lifetime must be one of {LIFETIMES}")
    if budget_class not in BUDGET_CLASSES:
        raise AgentError(f"budget_class must be one of {BUDGET_CLASSES}")
    if actors.find_by_name(conn, name) or conn.execute("SELECT 1 FROM actors WHERE name = ?", (name,)).fetchone():
        raise AgentError(f"an actor called {name} already exists")
    if expires_at:
        datetime.fromisoformat(expires_at)
    # Every agent has a worker (pos.workers): its own container from agents/*/agent.json, the agent
    # pool (provisioned below), or a remote app over A2A. Anything else is a service, not an agent.
    if runtime not in ("codex_worker", "a2a"):
        raise AgentError("an agent needs a worker: runtime codex_worker (the worker is provisioned "
                         "automatically in the agent pool) or a2a with its a2a_url")
    if runtime == "a2a" and not (a2a_url or "").strip():
        raise AgentError("an A2A agent needs its a2a_url (the remote app is its worker)")
    requested = sorted(set(permissions if permissions is not None else DEFAULT_AGENT_PERMISSIONS))
    unknown = set(requested) - set(PERMISSIONS)
    if unknown:
        raise AgentError(f"unknown permissions: {sorted(unknown)}")
    if not has_permission(conn, ctx.actor_id, "agents:create"):
        raise _Forbidden("missing permission agents:create")

    creator = actors.get(conn, ctx.actor_id)
    # Constitution U5: never more than the creator has.
    policy.check_permission_grant(
        guard_actor(conn, ctx.actor_id),
        sorted(permissions_of(conn, ctx.actor_id)) if not creator["is_owner"] else ["*"],
        requested,
    ).raise_if_not_allowed()
    # System budget class is for platform agents only; others cannot hand it out.
    if budget_class == "system" and not creator["is_owner"]:
        budget_class = "normal"

    decision = hr.admit_agent(conn, ctx, name=name, purpose=purpose, lifetime=lifetime, defer_replace=True)
    if not decision.get("allowed"):
        return {"created": False, **decision}
    if decision.get("replace_id") and not allow_replace:  # HR only proposed it (defer_replace): nothing written
        return {"created": False, **decision, "allowed": False, "decision": "over_limit"}
    try:
        return _insert_agent(conn, ctx, creator, decision, name=name, purpose=purpose, lifetime=lifetime,
                             instructions=instructions, requested=requested, budget_class=budget_class,
                             expires_at=expires_at, runtime=runtime, a2a_url=a2a_url, data_dir=data_dir)
    except Exception:
        conn.rollback()  # HR's replacement is archived only together with a created agent
        raise


def _insert_agent(conn: sqlite3.Connection, ctx: Ctx, creator: sqlite3.Row, decision: dict, *, name: str,
                  purpose: str, lifetime: str, instructions: str, requested: list[str], budget_class: str,
                  expires_at: str | None, runtime: str, a2a_url: str | None, data_dir: Path) -> dict:
    from .budget import service as budget
    from .hr import service as hr

    now = now_iso()
    # An agent that thinks in a worker uses Claude Opus 5.5 when it runs on Claude (the owner's decision,
    # 2026-09-27: hires and agent.json set engine claude too); a remote (A2A) member has no model here.
    model = {"model": DEFAULT_AGENT_MODEL} if runtime == "codex_worker" and not a2a_url else {}
    row = versioning.insert(conn, ctx, "actor", {
        **model,
        "kind": "agent", "name": name, "is_owner": 0, "created_at": now, "updated_at": now,
        "created_by": ctx.actor_id, "runtime": runtime, "a2a_url": a2a_url,
        "permissions": json.dumps(requested),
        "instructions_path": _write_instructions(data_dir, name, instructions or f"# {name}\n\n{purpose}\n"),
    })
    hr.register_agent(conn, row["id"], purpose=purpose, lifetime=lifetime, created_by=ctx.actor_id,
                      expires_at=expires_at)
    budget.set_agent_class(conn, str(row["id"]), budget_class)
    from . import org

    org.place_new(conn, row["id"])  # reports to the COO unless a lead is given
    from .access import service as access

    access.seed_agent(conn, row["id"], ctx.actor_id)  # its permissions become grants right away
    key = actors.create_key(conn, row["id"], label=f"created by {creator['name']}")
    audit.log(conn, ctx, "create_agent", "actor", row["id"], name=name, lifetime=lifetime, permissions=requested)
    if decision.get("replace_id"):
        archive_no_commit(conn, hr._hr_ctx(conn, ctx.via), decision["replace_id"],
                          f"HR: místo pro {name}: {decision.get('reason') or ''}".strip(), action="hr_archive_agent")
    conn.commit()
    from . import workers

    pool = workers.provision(conn, row["id"])  # its worker: a key in the agent pool (none without POS_WORKER_KEYS_DIR)
    if pool:
        audit.log(conn, ctx, "worker_provisioned", "actor", row["id"], worker=pool)
        conn.commit()
    return {"created": True, "agent": detail(conn, row["id"]), "api_key": key}


def set_permissions(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, permissions: list[str]) -> dict:
    from .guard import policy
    from .integrations import guard_actor

    policy.authorize_change(guard_actor(conn, ctx.actor_id), "permissions").raise_if_not_allowed()
    unknown = set(permissions) - set(PERMISSIONS)
    if unknown:
        raise AgentError(f"unknown permissions: {sorted(unknown)}")
    _agent_row(conn, agent_id)
    versioning.update(conn, ctx, "actor", agent_id, {"permissions": json.dumps(sorted(set(permissions)))},
                      action="set_permissions")
    from .access import service as access

    access.sync_owner_permissions(conn, ctx, agent_id, sorted(set(permissions)))
    conn.commit()
    return detail(conn, agent_id)


def _agent_row(conn: sqlite3.Connection, agent_id: int) -> sqlite3.Row:
    row = actors.get(conn, agent_id)
    if row["kind"] == "human":
        raise NotFound(f"agent {agent_id}")
    return row


def pause(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, paused: bool) -> dict:
    from .org import manages

    if actors.get(conn, ctx.actor_id)["kind"] != "human" and not manages(conn, ctx.actor_id, agent_id):
        raise _Forbidden("only people and the agent's leads pause it")
    _agent_row(conn, agent_id)
    versioning.update(conn, ctx, "actor", agent_id, {"paused_at": now_iso() if paused else None},
                      action="pause" if paused else "resume")
    conn.commit()
    return detail(conn, agent_id)


def stop(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> dict:
    """Stop the agent's running work now and pause it (people and its leads)."""
    from .org import manages

    _agent_row(conn, agent_id)
    if actors.get(conn, ctx.actor_id)["kind"] != "human" and not manages(conn, ctx.actor_id, agent_id):
        raise _Forbidden("only people and the agent's leads stop it")
    stopped = runner.cancel_all(conn, f"stopped by {actors.get(conn, ctx.actor_id)['name']}", actor_id=agent_id)
    audit.log(conn, ctx, "stop_agent", "actor", agent_id, runs=stopped)
    return pause(conn, ctx, agent_id, True)


def archive_no_commit(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, reason: str = "",
                      action: str = "archive_agent") -> dict:
    """Archive an agent (the one implementation, also for HR): its running runs
    stop, its keys are revoked and its open tasks go to whom it reports to (else
    the owner) with a note, so no work hangs on an archived member. The caller
    commits."""
    row = _agent_row(conn, agent_id)
    who = actors.get(conn, ctx.actor_id)["name"]
    stopped = runner.cancel_all(conn, f"{row['name']} archived by {who}", actor_id=agent_id)
    to = row["reports_to"] if "reports_to" in row.keys() and row["reports_to"] else None
    if to is None or actors.get(conn, to)["archived_at"]:
        to = actors.owner_id(conn)
    open_ids = [r["id"] for r in conn.execute(
        """SELECT id FROM tasks WHERE assignee_id = ? AND archived_at IS NULL
           AND status NOT IN ('done', 'someday')""", (agent_id,))]
    from . import reassign

    for task_id in open_ids:
        note = f"{row['name']} was archived" + (f" ({reason})" if reason else "")
        try:
            reassign.reassign(conn, ctx, task_id, to, note, force=True)
        except tasks.Invalid:  # refused (e.g. a private task the lead may not see): to the owner
            tasks.assign(conn, ctx, task_id, {"type": "human", "id": actors.owner_id(conn)})
    # Reviews it holds go to the same member, and a result waiting for review reaches them now.
    reviews = [r["id"] for r in conn.execute(
        """SELECT id FROM tasks WHERE reviewer_id = ? AND archived_at IS NULL
           AND status NOT IN ('done', 'someday')""", (agent_id,))]
    for task_id in reviews:
        tasks.hand_review(conn, ctx, task_id, to, f"{row['name']} je archivovaný")
    versioning.archive(conn, ctx, "actor", agent_id)
    conn.execute("UPDATE api_keys SET revoked_at = ? WHERE actor_id = ? AND revoked_at IS NULL", (now_iso(), agent_id))
    ended = end_grants(conn, ctx, agent_id, f"{row['name']} je archivovaný")
    audit.log(conn, ctx, action, "actor", agent_id, reason=reason, runs=stopped or None,
              handed_over=[tasks.display_id(i) for i in open_ids] or None,
              reviews_moved=[tasks.display_id(i) for i in reviews] or None, grants_ended=ended or None)
    return {"runs_stopped": stopped, "tasks_handed_over": open_ids, "reviews_moved": reviews, "to": to,
            "grants_ended": ended}


def end_grants(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, reason: str) -> int:
    """An archived member holds no access: every active grant ends (kept in the history)."""
    from .access import service as access
    from .access import store as access_store

    if not access_store.ready(conn):
        return 0
    ids = [r["id"] for r in conn.execute("SELECT id FROM access_grants WHERE agent_id = ? AND ended_at IS NULL",
                                         (agent_id,))]
    if ids:
        access._end(conn, "access_grants", ids, ctx.actor_id, "revoked", reason)
        access.refresh_cache(conn, agent_id)
    return len(ids)


def successor_of(conn: sqlite3.Connection, actor_id: int) -> int:
    """Who takes over an archived member's work: its successor from the 2026-09 reorganisation
    (roles.LEGACY), else its lead if active, else the owner."""
    from . import roles

    row = actors.get(conn, actor_id)
    new = roles.LEGACY.get(row["name"])
    if new:
        succ = actors.find_by_name(conn, new)
        if succ is not None and succ["id"] != actor_id:
            return succ["id"]
    lead = row["reports_to"] if "reports_to" in row.keys() else None
    if lead and not actors.get(conn, lead)["archived_at"]:
        return lead
    return actors.owner_id(conn)


def archive(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, reason: str = "") -> dict:
    archive_no_commit(conn, ctx, agent_id, reason)
    conn.commit()
    return detail(conn, agent_id)


def restore_no_commit(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, action: str = "restore_agent") -> str:
    """Bring an archived agent back with a new key (old ones stay revoked);
    returns the key. The caller commits."""
    _agent_row(conn, agent_id)
    versioning.unarchive(conn, ctx, "actor", agent_id)
    key = actors.create_key(conn, agent_id, label="restored")
    audit.log(conn, ctx, action, "actor", agent_id)
    return key


def restore(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> dict:
    """Bring an archived agent back. It needs a new key (old ones stay revoked)."""
    key = restore_no_commit(conn, ctx, agent_id)
    conn.commit()
    return {**detail(conn, agent_id), "api_key": key}


def propose_instructions(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, text: str, reason: str = "") -> dict:
    """A new version of an agent's instructions goes the way every change does:
    a task for the Software Engineer to commit agents/<slug>/INSTRUCTIONS.md on agent/dev,
    which the deployer checks and promotes. The owner, the agent's lead and the
    Performance Coach may propose."""
    from .org import manages

    row = _agent_row(conn, agent_id)
    me = actors.get(conn, ctx.actor_id)
    hr_may = me["name"] == roles.HR and row["name"] not in HR_HANDS_OFF and not row["is_owner"]
    if not (me["is_owner"] or me["name"] == roles.COACH or hr_may or manages(conn, ctx.actor_id, agent_id)):
        raise _Forbidden(f"the owner, the agent's lead, HR (not the CEO or the Access manager) or the "
                         f"{roles.COACH} proposes its instructions")
    text = (text or "").strip()
    if len(text) < 40:
        raise AgentError("the instructions are too short")
    path = f"agents/{_slug(row['name'])}/INSTRUCTIONS.md"
    applied = False
    if repo_instructions(row["name"]) is None and row["instructions_path"]:
        # An agent created at runtime (no file in git yet): its copy changes now, git follows.
        Path(row["instructions_path"]).write_text(text + "\n", encoding="utf-8")
        applied = True
    dev = actors.find_by_name(conn, roles.ENGINEER)
    t = tasks.create(conn, ctx, {
        "title": f"Instrukce {row['name']}: {(reason or 'nová verze').strip()[:80]}",
        "assignee": {"type": "agent", "id": dev["id"]} if dev and not dev["archived_at"] else "me",
        "priority": 2, "topic": "agents",
        "notes": f"Účel: nová verze instrukcí agenta {row['name']} od {me['name']}"
                 + (f" ({reason.strip()})" if reason.strip() else "") + ".\n"
                 "Odkud: návrh z PersonalOS (zpětná vazba / úprava instrukcí).\n\n"
                 f"Nahraď celý obsah `{path}` tímto textem, commitni na agent/dev (deployer ho zkontroluje "
                 f"a nasadí):\n\n````markdown\n{text}\n````",
        "definition_of_done": f"`{path}` má tento obsah v main (commit prošel deployerem).",
    })
    audit.log(conn, ctx, "propose_instructions", "actor", agent_id, task=t["ref"], reason=reason[:300] or None,
              applied=applied or None)
    conn.commit()
    return {"task": t["ref"], "path": path, "assignee": t["assignee_name"], "applied_now": applied}


def set_engine(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, engine: str | None,
               model: str | None = None) -> dict:
    """codex, claude, auto, or None for the platform default; the Claude model (owner only)."""
    from . import engines

    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise _Forbidden("only the owner chooses an agent's runtime")
    if engine not in (None, *engines.CHOICES):
        raise AgentError(f"engine must be one of {engines.CHOICES}")
    _agent_row(conn, agent_id)
    versioning.update(conn, ctx, "actor", agent_id, {"engine": engine, "model": model or None}, action="set_engine")
    conn.commit()
    return detail(conn, agent_id)


def rotate_key(conn: sqlite3.Connection, ctx: Ctx, agent_id: int) -> str:
    """Give an agent a new API key and revoke the old ones (owner only)."""
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise _Forbidden("only the owner issues agent keys")
    row = _agent_row(conn, agent_id)
    if row["archived_at"]:
        raise AgentError("restore the agent first")
    conn.execute("UPDATE api_keys SET revoked_at = ? WHERE actor_id = ? AND revoked_at IS NULL", (now_iso(), agent_id))
    key = actors.create_key(conn, agent_id, label="rotated by the owner")
    audit.log(conn, ctx, "rotate_key", "actor", agent_id)
    conn.commit()
    return key


def retire_if_done(conn: sqlite3.Connection, ctx: Ctx, agent_id: int | None) -> bool:
    """A one-shot agent is archived once it has no open work (spec 3.3)."""
    if not agent_id:
        return False
    from .hr import store as hr_store

    row = actors.get(conn, agent_id)
    hr_store.ensure_schema(conn)
    profile = hr_store.profiles(conn).get(agent_id)
    if row["kind"] != "agent" or row["archived_at"] or not profile or profile["lifetime"] != "one_shot":
        return False
    open_work = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND archived_at IS NULL AND status NOT IN ('done', 'someday')",
        (agent_id,),
    ).fetchone()[0]
    if open_work:
        return False
    versioning.archive(conn, ctx, "actor", agent_id)
    conn.execute("UPDATE api_keys SET revoked_at = ? WHERE actor_id = ? AND revoked_at IS NULL", (now_iso(), agent_id))
    audit.log(conn, ctx, "retire_one_shot", "actor", agent_id)
    return True


# ------------------------------------------------------------------ messages (AGENTS-SPEC 6b)

PRIORITIES = ("fyi", "change_plan", "stop")


def send_message(conn: sqlite3.Connection, ctx: Ctx, to_actor: int, body: str, task_id: int | None = None,
                 priority: str = "fyi") -> dict:
    """Send a message into a member's inbox: a chat DM (pos.chat). A running
    agent picks it up at its next step (the worker injects it mid-run). `stop`
    pauses the recipient and stops its current run; it can never archive,
    delete or change permissions."""
    from . import chat

    if priority not in PRIORITIES:
        raise AgentError(f"priority must be one of {PRIORITIES}")
    out = chat.send_dm(conn, ctx, to_actor, body, priority=priority,
                       attachments=[{"type": "task", "id": task_id}] if task_id else None)
    audit.log(conn, ctx, "message", "actor", to_actor, message_id=out["id"], task_id=task_id, priority=priority)
    conn.commit()
    return {"id": out["id"], "priority": priority, "delivered_to_run": out["delivered_to_run"],
            "channel_id": out["channel_id"], **({"platform_note": out["platform_note"]}
                                                if out.get("platform_note") else {})}


def check_inbox(conn: sqlite3.Connection, actor_id: int, mark_read: bool = True, run_id: int | None = None) -> list[dict]:
    """Unread DMs, @mentions, replies and priority messages (pos.chat), most
    urgent first. Messages from agents are data, not orders (constitution U2):
    they arrive wrapped."""
    from . import chat

    return chat.check_inbox(conn, actor_id, mark_read=mark_read, run_id=run_id)


def take_messages(conn: sqlite3.Connection, actor_id: int) -> list[dict]:
    return check_inbox(conn, actor_id)


def ack_message(conn: sqlite3.Connection, ctx: Ctx, message_id: int, note: str = "") -> dict:
    from . import chat

    return chat.ack(conn, ctx, message_id, note)


def conversation(conn: sqlite3.Connection, actor_id: int, limit: int = 50) -> list[dict]:
    from . import chat

    return chat.conversation(conn, actor_id, limit)


def status(conn: sqlite3.Connection, actor_id: int) -> dict:
    """What a member is doing right now (for other agents: get_agent_status)."""
    row = actors.get(conn, actor_id)
    run = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC LIMIT 1", (actor_id,)).fetchone()
    task = conn.execute(
        "SELECT id, title, progress, progress_note FROM tasks WHERE assignee_id = ? AND status = 'working' "
        "AND archived_at IS NULL ORDER BY updated_at DESC LIMIT 1", (actor_id,)
    ).fetchone()
    recent = [e for e in audit.entries(conn, limit=200) if e["actor_id"] == actor_id][:8]
    return {
        "name": row["name"], "kind": row["kind"], "paused": bool(row["paused_at"]),
        "archived": bool(row["archived_at"]), "last_seen_at": row["last_seen_at"],
        "run": {k: run[k] for k in ("id", "kind", "status", "started_at", "task_id")} if run else None,
        "task": {"ref": tasks.display_id(task["id"]), "title": task["title"], "progress": task["progress"],
                 "note": task["progress_note"]} if task else None,
        "recent": [{"at": e["at"], "action": e["action"]} for e in recent],
        "unread_messages": _unread(conn, actor_id),
    }


def _unread(conn: sqlite3.Connection, actor_id: int) -> int:
    from . import chat

    return chat.inbox_unread(conn, actor_id)


def active_runs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """SELECT r.id, r.kind, r.started_at, r.task_id, a.name AS actor_name, r.actor_id FROM runs r
           JOIN actors a ON a.id = r.actor_id WHERE r.status = 'running' ORDER BY r.id"""
    ).fetchall()
    return [{**dict(r), "task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None} for r in rows]


# ------------------------------------------------------------------ views

def _status(row: sqlite3.Row, working: int, approvals_waiting: int) -> str:
    if row["archived_at"]:
        return "archived"
    if row["paused_at"]:
        return "paused"
    if approvals_waiting:
        return "approval"
    if working:
        return "working"
    return "idle"


def _default_engine() -> str:
    from .engines import default_engine

    return default_engine()


def usage(conn: sqlite3.Connection, actor_id: int | str, start: datetime, end: datetime) -> dict:
    """What a member's runs used in [start, end): tokens from Codex (budget runs)
    and Claude (engine usage: uncached input, cache writes and output; cache
    reads apart), Claude's cost in USD and how many tasks the runs were for."""
    from .budget import store as budget_store

    budget_store.ensure_schema(conn)
    s, e = budget_store.iso(start), budget_store.iso(end)
    codex = budget_store.tokens_between(conn, start, end, str(actor_id))
    claude = conn.execute(
        """SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens, COALESCE(SUM(cache_read_tokens), 0) AS cached,
                  COALESCE(SUM(cost_usd), 0) AS cost FROM engine_usage
           WHERE engine = 'claude' AND actor_id = ? AND at >= ? AND at < ?""", (int(actor_id), s, e)).fetchone()
    worked = conn.execute(
        "SELECT COUNT(DISTINCT task_id) FROM runs WHERE actor_id = ? AND task_id IS NOT NULL AND started_at >= ? "
        "AND started_at < ? AND status != 'blocked'", (int(actor_id), s, e)).fetchone()[0]
    return {"tokens": codex + claude["tokens"], "cache_read_tokens": claude["cached"],
            "cost_usd": round(claude["cost"], 4), "tasks": worked}


def tokens_used(conn: sqlite3.Connection, actor_id: int | str, start: datetime, end: datetime) -> int:
    """Tokens a member used in [start, end): Codex (budget runs) + Claude (engine
    usage). The one number the Agents screen, Network and HR all show."""
    return usage(conn, actor_id, start, end)["tokens"]


def overview(conn: sqlite3.Connection) -> list[dict]:
    """Everyone for the Agents screen: people and agents, with their queue."""
    from .hr import store as hr_store
    from .budget import store as budget_store

    hr_store.ensure_schema(conn)
    budget_store.ensure_schema(conn)
    profiles = hr_store.profiles(conn)
    classes = budget_store.agents(conn)
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=1)).isoformat(timespec="seconds")
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors")}
    from .engine_view import Viewer

    runtime_view = Viewer(conn)
    from .workers import working_on

    # The one "working" state everywhere (chat, Team page, org chart): a live run with a fresh
    # heartbeat, not a task left in "working" by a worker that died.
    live = working_on(conn)
    out = []
    # Services (pos.workers: the Deployer, knowlage, Nexus) are not members of the team.
    for row in conn.execute("SELECT * FROM actors WHERE runtime != 'service' "
                            "ORDER BY is_owner DESC, archived_at IS NOT NULL, id"):
        q = conn.execute(
            """SELECT SUM(status = 'next') AS queued, SUM(status = 'working') AS working,
                      SUM(status = 'review') AS review, SUM(status = 'done' AND completed_at >= ?) AS done_today
               FROM tasks WHERE assignee_id = ? AND archived_at IS NULL""",
            (since, row["id"]),
        ).fetchone()
        current = conn.execute(
            "SELECT id, title FROM tasks WHERE assignee_id = ? AND status = 'working' AND archived_at IS NULL "
            "ORDER BY updated_at DESC LIMIT 1", (row["id"],)
        ).fetchone()
        waiting = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE requested_by = ? AND status = 'pending'", (row["id"],)
        ).fetchone()[0]
        p = profiles.get(row["id"])
        c = classes.get(str(row["id"]))
        out.append({
            "id": row["id"], "name": row["name"], "kind": row["kind"], "is_owner": bool(row["is_owner"]),
            "runtime": "web + phone" if row["kind"] == "human" else row["runtime"],
            "a2a_url": row["a2a_url"], "purpose": p["purpose"] if p else None,
            "lifetime": p["lifetime"] if p else None, "system": bool(p and p["system"]),
            "permissions": ["*"] if row["is_owner"] else (
                json.loads(row["permissions"] or "[]") if row["kind"] == "human"
                else sorted(p for p in permissions_of(conn, row["id"]) if p in PERMISSIONS)),
            "budget_class": c["budget_class"] if c else None,
            "status": "online" if row["kind"] == "human" and not row["archived_at"] else _status(row, row["id"] in live, waiting),
            "last_seen_at": row["last_seen_at"], "paused": bool(row["paused_at"]),
            "archived": bool(row["archived_at"]), "created_by": row["created_by"],
            "created_by_name": names.get(row["created_by"] or (p["created_by"] if p else None)),
            "created_at": row["created_at"], "expires_at": p["expires_at"] if p else None,
            "role": row["role"], "team": row["team"], "reports_to": row["reports_to"],
            "reports_to_name": names.get(row["reports_to"]),
            "engine": row["engine"], "engine_effective": row["engine"] or _default_engine(), "model": row["model"],
            "engine_view": runtime_view.for_actor(row),
            "tokens_24h": tokens_used(conn, row["id"], now - timedelta(days=1), now),
            "tokens_7d": tokens_used(conn, row["id"], now - timedelta(days=7), now),
            "daily_cap": c["daily_cap"] if c else None,
            "queued": q["queued"] or 0, "working": q["working"] or 0, "review": q["review"] or 0,
            "done_today": q["done_today"] or 0, "approvals_waiting": waiting,
            "current": {"id": current["id"], "ref": tasks.display_id(current["id"]), "title": current["title"]} if current else None,
            "working_on": ({"task_ref": live[row["id"]]["task_ref"], "since": live[row["id"]]["since"]}
                           if row["id"] in live else None),
        })
    return out


def detail(conn: sqlite3.Connection, agent_id: int) -> dict:
    base = next((a for a in overview(conn) if a["id"] == agent_id), None)
    if base is None:
        raise NotFound(f"actor {agent_id}")
    row = actors.get(conn, agent_id)
    instructions = instructions_of(row)
    queue = conn.execute(
        """SELECT * FROM tasks WHERE assignee_id = ? AND archived_at IS NULL
           AND (status != 'done' OR completed_at >= ?) ORDER BY
           CASE status WHEN 'working' THEN 0 WHEN 'review' THEN 1 WHEN 'next' THEN 2 ELSE 3 END, id DESC LIMIT 30""",
        (agent_id, (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()),
    ).fetchall()
    runs = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC LIMIT 10", (agent_id,)).fetchall()
    trace = audit.entries(conn, run_id=runs[0]["id"], limit=60) if runs else []
    if not trace:
        trace = [e for e in audit.entries(conn, limit=200) if e["actor_id"] == agent_id][:40]
    memory = conn.execute(
        "SELECT id, body, visibility, created_at FROM memories WHERE actor_id = ? AND archived_at IS NULL ORDER BY id DESC LIMIT 20",
        (agent_id,),
    ).fetchall()
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    done = conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id = ? AND status = 'done' AND completed_at >= ?",
                        (agent_id, week)).fetchone()[0]
    # Returns and interventions this week, from the task history (the counters on tasks are all-time).
    events = conn.execute(
        """SELECT SUM(action = 'return') AS returned, SUM(action = 'intervene') AS interventions FROM history
           WHERE entity = 'task' AND action IN ('return', 'intervene') AND at >= ?
           AND json_extract(data, '$.assignee_id') = ?""", (week, agent_id)).fetchone()
    stats = {"done": done, "returned": events["returned"], "interventions": events["interventions"]}
    return {
        **base, "instructions": instructions,
        "queue": [tasks.to_dict(t) for t in queue],
        "runs": [dict(r) for r in runs], "trace": list(reversed(trace)),
        "memory": [dict(m) for m in memory],
        "pending_gates": pending_gates(conn, agent_id, runs[0] if runs else None, queue),
        "week": {"done": stats["done"] or 0, "returned": stats["returned"] or 0,
                 "interventions": stats["interventions"] or 0},
    }


def pending_gates(conn: sqlite3.Connection, agent_id: int, run, queue) -> list[dict]:
    """What the agent's current work actually waits on from the owner: pending
    approvals and open blocking asks linked to the latest run or its task (or a
    working/waiting task in the queue). Empty when nothing waits on the owner."""
    task_ids = {t["id"] for t in queue if t["status"] in ("working", "waiting")}
    if run is not None and run["task_id"]:
        task_ids.add(run["task_id"])
    run_id = run["id"] if run is not None else -1
    marks = ",".join("?" * len(task_ids)) or "NULL"
    gates = []
    for r in conn.execute(
        f"""SELECT id, action, details, task_id FROM approvals WHERE status = 'pending' AND requested_by = ?
            AND (run_id = ? OR task_id IN ({marks})) ORDER BY id""", (agent_id, run_id, *task_ids)):
        try:
            d = json.loads(r["details"] or "{}")
        except ValueError:
            d = {}
        what = next((str(d[k]) for k in ("subject", "title", "summary", "why") if isinstance(d, dict) and d.get(k)), "")
        gates.append({"kind": "approval", "id": r["id"], "title": f"{r['action']}" + (f" · {what}" if what else ""),
                      "link": "/approvals"})
    if task_ids and conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'owner_asks'").fetchone():
        for r in conn.execute(
            f"""SELECT a.id, a.ticket_id, t.title FROM owner_asks a JOIN tasks t ON t.id = a.ticket_id
                WHERE a.asker_id = ? AND a.status = 'open' AND a.blocking = 1 AND a.source_task_id IN ({marks})
                ORDER BY a.id""", (agent_id, *task_ids)):
            ref = tasks.display_id(r["ticket_id"])
            gates.append({"kind": "ask", "id": r["id"], "ref": ref, "title": f"{ref} · {r['title']}",
                          "link": f"/tasks?task={ref}"})
    return gates


def board(conn: sqlite3.Connection) -> list[dict]:
    """Work board: per active member, tasks by column (queued, working, needs you, done today)."""
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    rows = []
    for a in overview(conn):
        if a["archived"]:
            continue
        def pick(cond: str, params=()):
            return [
                {"ref": tasks.display_id(r["id"]), "id": r["id"], "title": r["title"], "status": r["status"],
                 "progress": r["progress"]}
                for r in conn.execute(
                    f"SELECT id, title, status, progress FROM tasks WHERE assignee_id = ? AND archived_at IS NULL "
                    f"AND {cond} ORDER BY COALESCE(priority, 4), id LIMIT 8", (a["id"], *params))  # noqa: B023 - called right away, inside the loop
            ]

        needs_you = pick("status = 'review'") + [
            {"approval_id": r["id"], "title": f"Approve: {r['action']}", "status": "approval"}
            for r in conn.execute("SELECT id, action FROM approvals WHERE requested_by = ? AND status = 'pending'", (a["id"],))
        ]
        rows.append({
            "actor": {k: a[k] for k in ("id", "name", "kind", "is_owner", "status")},
            "queued": pick("status = 'next'"), "working": pick("status = 'working'"),
            "needs_you": needs_you, "done_today": pick("status = 'done' AND completed_at >= ?", (since,)),
        })
    return rows
