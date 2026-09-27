"""Every agent has a worker; everything else is a service (the owner's rule, 2026-09-26).

An agent is a member that thinks with a model, so it runs somewhere: its own
worker container (agents/<slug>/agent.json "worker"), the shared agent pool
(agents created at runtime, by hiring or on the Agents page: the core writes a
key into <POS_WORKER_KEYS_DIR>/pool/<slug>/key and the `agent-pool` container
starts a worker for every key it finds, pos_worker.pool), or a remote app over
A2A (a2a_url, e.g. knowlage's Knowledge agent). A built-in member that is plain
automation or an outside system (the Deployer, knowlage's Knowledge agent,
Nexus) is a *service*: runtime "service", no worker, no HR review, not on the
Team page, the org chart or the chat's member list; agents use it through its
own interface (ask_agent / the A2A bridge for knowlage and Nexus, the deploy
API), and its health shows among the subsystems. A message to it anyway gets a
code-built reply and goes to the CEO.

`reply_path` is what the chat uses so that no message from the owner is ever
left unanswered (pos.chat._ask_to_answer, pos.availability); `worker_down`
says, in Czech, why an agent's worker is not running; `sync_pool_keys` keeps
the pool's keys in step with the agents (scheduler, every 2 min).
"""

import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import actors

# Built-in members that are automation, not agents (see the module docstring).
SERVICES = {
    "Deployer": "nasazovací služba: slučuje commity Vývojáře, pouští testy a nasazuje (pos.selfdeploy), "
                "bez AI",
    "Knowledge agent": "znalostní báze knowlage: agenti se jí ptají přes ask_agent (hledání s citacemi)",
    "Nexus": "platforma pro automatizaci procesů (Nexus), napojená přes A2A",
}
POOL = "pool"
LIVE_S = 300               # a run whose worker spoke (heartbeat or alive tick) this recently is live
WORKER_STALE_S = 300       # workers long-poll every 60 s; 5 min of silence and no run: not running
NEVER_SEEN_GRACE_S = 600   # a new agent's worker gets 10 min to start


def mark_services(conn: sqlite3.Connection) -> list[str]:
    """The built-in members that are services, not agents (idempotent); their A2A
    address stays (pos.a2a.configure_builtin), their worker never exists."""
    done = []
    for name in SERVICES:
        row = conn.execute("SELECT id, runtime FROM actors WHERE name = ?", (name,)).fetchone()
        if row is None or row["runtime"] == "service":
            continue
        conn.execute("UPDATE actors SET runtime = 'service' WHERE id = ?", (row["id"],))
        done.append(name)
    conn.commit()
    return done


def is_service(row) -> bool:
    return row["kind"] != "human" and row["runtime"] == "service"


def _spec(name: str, base: Path | None = None) -> dict | None:
    from . import agents_code

    return next((s for s in agents_code.specs(base) if s["name"] == name), None)


def slug(name: str) -> str:
    from .agents import _slug

    return _slug(name)


def reply_path(conn: sqlite3.Connection, row, specs_base: Path | None = None) -> dict | None:
    """Where this member's work runs: {"kind": "dedicated" | "pool" | "a2a", "name": …};
    None for people, services and agents without any worker (the chat then
    answers in code and passes the message to the CEO)."""
    if row is None or row["kind"] == "human" or row["runtime"] == "service":
        return None
    if row["a2a_url"]:
        return {"kind": "a2a", "name": row["a2a_url"]}
    spec = _spec(row["name"], specs_base)
    if spec is not None:
        worker = spec.get("worker")
        if spec.get("enabled") is False or not worker or worker == "none" \
                or spec.get("runtime", "codex_worker") != "codex_worker":
            return None
        if worker == POOL:  # defined in git, run by the agent pool (e.g. the CEO, hired agents)
            return {"kind": "pool", "name": f"{POOL}/{slug(row['name'])}"}
        return {"kind": "dedicated", "name": worker}
    if row["runtime"] == "codex_worker":
        return {"kind": "pool", "name": f"{POOL}/{slug(row['name'])}"}
    return None


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds()


def live_cutoff(now: datetime | None = None, live_s: int = LIVE_S) -> str:
    """Heartbeats at or after this are fresh: the run has a worker behind it."""
    from datetime import timedelta

    return ((now or datetime.now(timezone.utc)) - timedelta(seconds=live_s)).isoformat(timespec="seconds")


LIVE_RUN_SQL = ("status = 'running' AND kind != 'chat_fastlane' "
                "AND COALESCE(heartbeat_at, started_at) >= ?")  # with live_cutoff()


def working_on(conn: sqlite3.Connection, actor_id: int | None = None) -> dict[int, dict]:
    """THE "working" state (the chat, the Team page, the org chart, the network): per agent
    with a live run (running, a fresh heartbeat), the newest such run:
    {"task_ref", "since", "task_id", "title", "run_id", "minutes"}. A run whose worker went
    silent does not count, however its row reads."""
    from . import tasks

    sql = ("SELECT r.id, r.actor_id, r.task_id, r.started_at, t.title FROM runs r LEFT JOIN tasks t "
           "ON t.id = r.task_id WHERE r.status = 'running' AND r.kind != 'chat_fastlane' "
           "AND COALESCE(r.heartbeat_at, r.started_at) >= ?")
    args: list = [live_cutoff()]
    if actor_id is not None:
        sql += " AND r.actor_id = ?"
        args.append(actor_id)
    out: dict[int, dict] = {}
    for r in conn.execute(sql + " ORDER BY r.id", args):
        age = _age_s(r["started_at"]) or 0
        out[r["actor_id"]] = {"task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None,
                              "since": r["started_at"], "task_id": r["task_id"], "title": r["title"],
                              "run_id": r["id"], "minutes": max(0, int(age // 60))}
    return out


STALE_WORKING_S = 600  # a task read "working" this long after its last change with no live run on it


def reset_stale_working(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """A task is "working" only while a live run is on it (working_on). An agent's task left in
    `working` with no live run (the run ended or died, or the task was backed off: T-167 read
    "working" with no run and a back-off until the next morning) goes back to `next`; a back-off
    (retry_after) stays. People's tasks are left alone: they work without runs."""
    from datetime import timedelta

    from . import tasks, versioning
    from .core import Ctx

    now = now or datetime.now(timezone.utc)
    quiet = (now - timedelta(seconds=STALE_WORKING_S)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT t.id FROM tasks t JOIN actors a ON a.id = t.assignee_id
           WHERE t.status = 'working' AND t.archived_at IS NULL AND a.kind != 'human' AND t.updated_at < ?
             AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.task_id = t.id AND r.status = 'running'
                             AND COALESCE(r.heartbeat_at, r.started_at) >= ?)
             AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.task_id = t.id AND r.started_at >= ?)""",
        (quiet, live_cutoff(now), quiet)).fetchall()
    ctx = Ctx(actors.assistant_id(conn), via="system")
    for r in rows:
        versioning.update(conn, ctx, tasks.ENTITY, r["id"], {"status": "next"}, action="no_live_run")
    return [tasks.display_id(r["id"]) for r in rows]


def a2a_bridge_off(conn: sqlite3.Connection) -> bool:
    """The scheduler job that hands tasks to remote agents is switched off (Automations)."""
    job = conn.execute("SELECT enabled FROM jobs WHERE action = 'a2a_sync'").fetchone()
    return job is not None and not job["enabled"]


def worker_down(conn: sqlite3.Connection, row, stale_s: int = WORKER_STALE_S) -> str | None:
    """Czech reason when the agent's worker is not running now (its container or
    pool worker is silent, or the A2A bridge to its remote app is off); None
    when it runs."""
    path = reply_path(conn, row)
    if path is None:
        return None
    if path["kind"] == "a2a":
        return ("jeho spojení se vzdálenou aplikací je vypnuté (úloha „A2A: hand tasks to remote agents“ "
                "na stránce Automatizace)") if a2a_bridge_off(conn) else None
    # A run counts only while its worker still speaks: a run left "running" by a worker that
    # died in an outage is no proof that a worker is there (2026-09-27).
    if conn.execute(f"SELECT 1 FROM runs WHERE actor_id = ? AND {LIVE_RUN_SQL}",
                    (row["id"], live_cutoff())).fetchone():
        return None
    age = _age_s(row["last_seen_at"])
    if age is None:
        created = _age_s(row["created_at"])
        if created is not None and created > NEVER_SEEN_GRACE_S:
            return f"jeho worker ({path['name']}) ještě nikdy neběžel"
        return None
    if age > stale_s:
        from .availability import when_cz

        return f"jeho worker ({path['name']}) neběží (naposledy aktivní {when_cz(row['last_seen_at'])})"
    return None


def agents_needing_workers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Active agents (not people, not services, not paused)."""
    return conn.execute("""SELECT * FROM actors WHERE kind != 'human' AND archived_at IS NULL AND paused_at IS NULL
                           AND runtime != 'service' ORDER BY id""").fetchall()


# ------------------------------------------------------------------ keys for the agent pool

def write_key(conn: sqlite3.Connection, path: Path, actor_id: int, label: str) -> bool:
    """A valid key for the actor in `path`; an existing valid key stays. True when written."""
    if path.exists():
        current = path.read_text(encoding="utf-8").strip()
        if current and actors.actor_for_key(conn, current) == actor_id:
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    key = actors.create_key(conn, actor_id, label=label)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return True


def keys_dir() -> Path | None:
    raw = os.environ.get("POS_WORKER_KEYS_DIR")
    return Path(raw) if raw else None


def provision(conn: sqlite3.Connection, actor_id: int, base: Path | None = None) -> str | None:
    """Give an agent without a worker of its own a place in the agent pool: its
    key in <keys dir>/pool/<slug>/key. Returns the pool name, or None."""
    base = base or keys_dir()
    row = actors.get(conn, actor_id)
    path = reply_path(conn, row)
    if base is None or row["archived_at"] or path is None or path["kind"] != "pool":
        return None
    write_key(conn, base / path["name"] / "key", actor_id, f"worker {path['name']} (agent pool)")
    conn.commit()
    return path["name"]


def sync_pool_keys(conn: sqlite3.Connection, base: Path | None = None, specs_base: Path | None = None) -> dict:
    """Every active pool agent has a valid key in the pool; the folders of
    archived or paused ones go (the pool stops their worker)."""
    base = base or keys_dir()
    if base is None:
        return {}
    written, removed, wanted = [], [], set()
    for row in agents_needing_workers(conn):
        path = reply_path(conn, row, specs_base)
        if path is None or path["kind"] != "pool":
            continue
        wanted.add(path["name"].split("/", 1)[1])
        if write_key(conn, base / path["name"] / "key", row["id"], f"worker {path['name']} (agent pool)"):
            written.append(path["name"])
    pool = base / POOL
    if pool.is_dir():
        for d in pool.iterdir():
            if d.is_dir() and d.name not in wanted:
                shutil.rmtree(d, ignore_errors=True)
                removed.append(d.name)
    conn.commit()
    return {"written": written, "removed": removed}


# ------------------------------------------------------------------ the watch (scheduler, every 2 min)

UNANSWERED_S = 600          # an owner message without an answer for 10 min is an incident
UNANSWERED_WINDOW_S = 86400
WORKER_DOWN_S = 600         # a worker silent for 10 min (no run) is an incident
AUTOREPLY_PREFIX = "Automatická odpověď platformy"


def _state(conn: sqlite3.Connection, key: str, default=None):
    from . import monitor

    monitor.ensure_schema(conn)
    return monitor._state(conn, key, default)


def _set_state(conn: sqlite3.Connection, key: str, value) -> None:
    from . import monitor

    monitor.ensure_schema(conn)
    monitor._set_state(conn, key, value)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _incident(conn: sqlite3.Connection, *, iid: str, kind: str, key: str, title: str, body: str,
              detail: dict, resolved: bool = False) -> dict | None:
    """A PersonalOS incident for the Monitor, shaped like the sentinel's (pos.monitor:
    dedup by service, kind and key; the daily caps; the owner fallback)."""
    from . import monitor, routing
    from .core import Ctx

    now = _iso(datetime.now(timezone.utc))
    event = {"source": "sentinel", "kind": "incident_resolved" if resolved else "incident",
             "ref": f"personalos:{iid}#{'resolved' if resolved else 0}",
             "title": f"[high] personalos: {title}", "body": body, "author": "personalos",
             "meta": {"labels": ["service:personalos", f"kind:{kind}"],
                      "data": {"incident": {"incident_id": iid, "service": "personalos", "kind": kind,
                                            "severity": "high", "key": key, "count": 1, "container": "api",
                                            "detail": detail, "level": 0, "first_seen": now, "last_seen": now}}}}
    who = monitor.monitor_id(conn) or actors.owner_id(conn)
    try:
        out = routing.ingest(conn, Ctx(who, via="personalos-watch"), event)
    except Exception as e:  # noqa: BLE001 - the watch runs again in 2 min
        import logging

        logging.getLogger(__name__).warning("incident %s not filed: %s", iid, e)
        return None
    conn.commit()
    return out


def _owner_messages(conn: sqlite3.Connection, since: str, until: str) -> list[tuple[sqlite3.Row, int]]:
    """(message, agent) for the owner's plain messages to agents: DMs and @mentions. What the
    platform sends by itself under the owner's name (a system message: reminders, notices) is not
    his message and waits for no answer (T-194/T-195: review reminders counted as unanswered)."""
    import json

    owner = actors.owner_id(conn)
    out = []
    for m in conn.execute(
            """SELECT m.*, c.kind AS ch_kind FROM chat_messages m JOIN channels c ON c.id = m.channel_id
               WHERE m.author_id = ? AND m.archived_at IS NULL AND m.created_at >= ? AND m.created_at <= ?
                 AND m.priority IS NULL AND NOT EXISTS (
                   SELECT 1 FROM audit_log l WHERE l.action = 'chat_send' AND l.entity = 'chat_message'
                   AND l.entity_id = m.id AND (l.via IN ('system', 'scheduler', 'personalos-watch')
                                               OR json_extract(l.detail, '$.system') = 1))
               ORDER BY m.id""", (owner, since, until)).fetchall():
        if m["ch_kind"] == "dm":
            targets = [r[0] for r in conn.execute(
                "SELECT actor_id FROM channel_members WHERE channel_id = ? AND actor_id != ?", (m["channel_id"], owner))]
        else:
            targets = json.loads(m["mentions"] or "[]")
        for aid in targets:
            row = conn.execute("SELECT kind, archived_at, runtime FROM actors WHERE id = ?", (aid,)).fetchone()
            # services (the Deployer, knowlage, Nexus) never answer: the chat's code reply covers them
            if row and row["kind"] != "human" and not row["archived_at"] and row["runtime"] != "service":
                out.append((m, aid))
    return out


def answered(conn: sqlite3.Connection, message: sqlite3.Row, agent_id: int) -> bool:
    """Did the agent (or whoever the message went to) answer it for real? A reply
    by the agent in the channel after it, another member's reply in its thread, or
    the chat task for it is done. The platform's automatic replies do not count."""
    if conn.execute("""SELECT 1 FROM chat_messages WHERE channel_id = ? AND id > ? AND archived_at IS NULL
                       AND body NOT LIKE ? AND (author_id = ? OR (reply_to = ? AND author_id != ?))""",
                    (message["channel_id"], message["id"], AUTOREPLY_PREFIX + "%", agent_id, message["id"],
                     message["author_id"])).fetchone():
        return True
    for (tid,) in conn.execute("""SELECT entity_id FROM audit_log WHERE action = 'chat_task' AND entity = 'task'
                                  AND json_extract(detail, '$.message') = ?""", (message["id"],)).fetchall():
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()
        if t and t["status"] == "done":
            return True
    fwd = conn.execute("""SELECT json_extract(detail, '$.task') FROM audit_log WHERE action = 'chat_forwarded'
                          AND entity = 'chat_message' AND entity_id = ?""", (message["id"],)).fetchone()
    if fwd and fwd[0]:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (fwd[0],)).fetchone()
        return bool(t and t["status"] == "done")
    return False


def _nudge(conn: sqlite3.Connection, message: sqlite3.Row, agent) -> None:
    """No automatic reply went out for this message yet: say now why there is no
    answer (code-built, Czech), once."""
    from . import availability, chat
    from .core import Ctx

    if conn.execute("""SELECT 1 FROM chat_messages WHERE channel_id = ? AND reply_to = ? AND author_id = ?
                       AND body LIKE ?""", (message["channel_id"], message["id"], agent["id"],
                                            AUTOREPLY_PREFIX + "%")).fetchone():
        return
    why = availability.why_not(conn, agent["id"])
    reason = why["reason"] if why else "se ke zprávě zatím nedostal (odpověď čeká déle než 10 minut)"
    retry = why["retry"] if why else "hned, jak to půjde"
    body = (f"{AUTOREPLY_PREFIX}: {agent['name']} ti zatím neodpověděl, protože {reason}. Hlídač o tom dostal "
            f"incident; {agent['name']} to zkusí znovu {retry}.")
    try:
        chat._add_member(conn, message["channel_id"], agent["id"])
        chat.send(conn, Ctx(agent["id"], via="system"), message["channel_id"], body, reply_to=message["id"],
                  system=True)
    except Exception:  # noqa: BLE001 - the incident still goes out
        pass


def watch(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Scheduler (every 2 min): the pool's keys follow the agents; an agent whose
    worker has been silent for 10 min is an incident (resolved when it is back);
    an owner message to an agent without a real answer after 10 min gets a
    code-built reply (if none went out yet) and is an incident. Nothing here
    runs a model."""
    from datetime import timedelta

    from . import audit
    from .core import Ctx

    now = now or datetime.now(timezone.utc)
    out: dict = {"pool": sync_pool_keys(conn), "down": [], "back": [], "unanswered": []}
    stale = reset_stale_working(conn, now)  # "working" needs a live run
    if stale:
        out["no_live_run"] = stale

    # workers that are not running
    down_now = {}
    for row in agents_needing_workers(conn):
        reason = worker_down(conn, row, stale_s=WORKER_DOWN_S)
        if reason:
            down_now[row["name"]] = (row, reason)
    known = dict(_state(conn, "workers_down", {}) or {})
    for name, (row, reason) in down_now.items():
        if name in known:
            continue
        iid = f"worker-down-{slug(name)}-{now.strftime('%Y%m%d%H%M')}"
        path = reply_path(conn, row)
        wname = path["name"] if path else "?"
        _incident(conn, iid=iid, kind="worker_down", key=f"worker:{slug(name)}",
                  title=f"worker agenta {name} neběží",
                  body=(f"**Agent:** {name}\n**Worker:** `{wname}`\n**Proč:** {reason}\n\n"
                        "Zprávy a úkoly tohoto agenta čekají; majitel dostává automatickou odpověď. Zkontroluj "
                        "kontejner (`docker compose ps`, `docker compose logs --tail 50 <worker>`) a jeho klíč "
                        "(data/worker-keys)."),
                  detail={"agent": name, "worker": wname, "reason": reason})
        known[name] = iid
        out["down"].append(name)
    for name in [n for n in known if n not in down_now]:
        _incident(conn, iid=known.pop(name), kind="worker_down", key=f"worker:{slug(name)}",
                  title=f"worker agenta {name} zase běží", body="", detail={"agent": name}, resolved=True)
        out["back"].append(name)
    _set_state(conn, "workers_down", known)

    # owner messages without an answer (only messages after the first watch: no alarms about old history)
    since = _state(conn, "chat_watch_since")
    if not since:
        _set_state(conn, "chat_watch_since", _iso(now))
        conn.commit()
        return out
    since = max(since, _iso(now - timedelta(seconds=UNANSWERED_WINDOW_S)))
    for m, aid in _owner_messages(conn, since, _iso(now - timedelta(seconds=UNANSWERED_S))):
        if conn.execute("""SELECT 1 FROM audit_log WHERE action = 'chat_unanswered' AND entity = 'chat_message'
                           AND entity_id = ? AND json_extract(detail, '$.agent') = ?""", (m["id"], aid)).fetchone():
            continue
        if answered(conn, m, aid):
            continue
        agent = actors.get(conn, aid)
        _nudge(conn, m, agent)
        route = (reply_path(conn, agent) or {}).get("name") or "žádná"
        _incident(conn, iid=f"chat-unanswered-{m['id']}-{aid}", kind="chat_unanswered",
                  key=f"chat:{slug(agent['name'])}",
                  title=f"{agent['name']} neodpověděl majiteli v chatu přes 10 minut",
                  body=(f"**Agent:** {agent['name']}\n**Zpráva:** {m['id']} v kanálu {m['channel_id']} "
                        f"({m['created_at']})\n**Cesta odpovědi:** {route}\n\n"
                        "Majitel dostal automatickou odpověď. Zjisti, proč agent neodpověděl (worker, rozpočet, "
                        "úkol „Chat: answer“ ve frontě), a zařiď opravu."),
                  detail={"agent": agent["name"], "message": m["id"], "channel": m["channel_id"]})
        audit.log(conn, Ctx(actors.owner_id(conn), via="personalos-watch"), "chat_unanswered", "chat_message",
                  m["id"], agent=aid)
        out["unanswered"].append({"message": m["id"], "agent": agent["name"]})
    out["answered"] = resolve_answered(conn)
    conn.commit()
    return out


def resolve_answered(conn: sqlite3.Connection) -> list[dict]:
    """An unanswered-chat incident ends once the agent has answered (or the message went
    to a service, which never answers): the incident is resolved, so its untouched task
    or ticket closes (pos.monitor)."""
    from . import audit
    from .core import Ctx

    done = []
    rows = conn.execute(
        """SELECT l.entity_id AS message_id, json_extract(l.detail, '$.agent') AS agent_id FROM audit_log l
           WHERE l.action = 'chat_unanswered' AND l.entity = 'chat_message' AND NOT EXISTS (
               SELECT 1 FROM audit_log r WHERE r.action = 'chat_unanswered_resolved' AND r.entity = 'chat_message'
                 AND r.entity_id = l.entity_id AND json_extract(r.detail, '$.agent') = json_extract(l.detail, '$.agent'))
           ORDER BY l.id LIMIT 200""").fetchall()
    for r in rows:
        m = conn.execute("SELECT * FROM chat_messages WHERE id = ?", (r["message_id"],)).fetchone()
        agent = conn.execute("SELECT * FROM actors WHERE id = ?", (r["agent_id"],)).fetchone()
        if m is None or agent is None:
            continue
        why = ("service" if agent["runtime"] == "service" else "archived" if agent["archived_at"]
               else "answered" if answered(conn, m, agent["id"]) else None)
        if why is None:
            continue
        _incident(conn, iid=f"chat-unanswered-{m['id']}-{agent['id']}", kind="chat_unanswered",
                  key=f"chat:{slug(agent['name'])}", title=f"{agent['name']} už majiteli odpověděl",
                  body="", detail={"agent": agent["name"], "message": m["id"], "why": why}, resolved=True)
        audit.log(conn, Ctx(actors.owner_id(conn), via="personalos-watch"), "chat_unanswered_resolved",
                  "chat_message", m["id"], agent=agent["id"], why=why)
        done.append({"message": m["id"], "agent": agent["name"], "why": why})
    return done
