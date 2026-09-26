"""Chat with a busy agent (docs/CHAT.md, "Talking to a working agent").

A chat message to an agent that is in the middle of a run (a DM, an @mention,
a reply to it) goes into that run at its next step boundary (the worker's
inbox check, pos_worker.loop). A long step (a build, a long tool call) can
hold that for minutes, so when the run has not picked the message up within
FASTLANE_AFTER_S, the platform answers in the chat right away from a snapshot
of the work: the task, its plan and latest progress, the last steps, the
elapsed time. One cheap model call (FASTLANE_MODEL, no tools) writes the
answer; it never claims work that is not in the snapshot and decides nothing:
instructions wait for the main run. Its cost is a run of the agent (usage,
budget). When the model or the budget is not available, a code-built status
reply goes out instead: the person is never left in silence.

Only messages from people get a fast answer (agents can wait for the step),
once per message.
"""

import asyncio
import logging
import os
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta, timezone

from . import actors, audit, chat
from .core import Ctx, now_iso

log = logging.getLogger("pos.fastlane")

FASTLANE_AFTER_S = 30  # a message the run has not picked up after this long gets a fast answer
FASTLANE_MAX_AGE_S = 30 * 60  # older unread messages are left to the run (e.g. after a restart)
FASTLANE_MODEL = os.environ.get("POS_FASTLANE_MODEL", "claude-haiku-4-5")
LIVE_S = 5 * 60  # a run whose worker spoke within this is alive (api_worker.LIVE_RUN_MINUTES)

# ------------------------------------------------------------------ the run's last steps (memory)

_steps: dict[int, dict] = {}
_lock = threading.Lock()


def step(run_id: int, label: str | None, count: int | None = None) -> None:
    """A completed step reported by the worker's heartbeat."""
    with _lock:
        s = _steps.setdefault(run_id, {"count": 0, "recent": deque(maxlen=5)})
        s["count"] = count if count is not None else s["count"] + 1
        if label:
            s["recent"].append((label[:120], now_iso()))


def forget(run_id: int) -> None:
    with _lock:
        _steps.pop(run_id, None)


def steps_of(run_id: int) -> dict:
    with _lock:
        s = _steps.get(run_id)
        return {"count": s["count"], "recent": list(s["recent"])} if s else {"count": 0, "recent": []}


# ------------------------------------------------------------------ snapshot of the work

def _minutes_since(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds() // 60))


def live_run(conn: sqlite3.Connection, actor_id: int) -> sqlite3.Row | None:
    """The agent's running run with a worker behind it (a recent heartbeat), newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LIVE_S)).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT * FROM runs WHERE actor_id = ? AND status = 'running' AND kind != 'chat_fastlane' "
        "AND COALESCE(heartbeat_at, started_at) >= ? ORDER BY id DESC LIMIT 1", (actor_id, cutoff)).fetchone()


def snapshot(conn: sqlite3.Connection, actor_id: int, run: sqlite3.Row | None = None) -> dict:
    """What the agent is doing now, from facts only (the platform's records)."""
    from . import tasks

    run = run or live_run(conn, actor_id)
    me = actors.get(conn, actor_id)
    out: dict = {"agent": me["name"], "busy": run is not None}
    # Who it is in the company, so a fast answer about itself is right (docs/REORG.md).
    lead = None
    if "reports_to" in me.keys() and me["reports_to"]:
        lead = conn.execute("SELECT name FROM actors WHERE id = ?", (me["reports_to"],)).fetchone()
    out["who"] = ", ".join(x for x in (f"role {me['role']}" if me["role"] else "",
                                       f"tým {me['team']}" if me["team"] else "",
                                       f"vedoucí {lead['name']}" if lead else "") if x)
    if run is None:
        return out
    out.update(run_id=run["id"], minutes=_minutes_since(run["started_at"]), **steps_of(run["id"]))
    if run["task_id"]:
        t = conn.execute("SELECT id, title, notes, progress, progress_note, status FROM tasks WHERE id = ?",
                         (run["task_id"],)).fetchone()
        if t:
            progress = [r["body"] for r in conn.execute(
                "SELECT body FROM task_comments WHERE task_id = ? AND kind = 'progress' ORDER BY id DESC LIMIT 3",
                (t["id"],))]
            out["task"] = {"ref": tasks.display_id(t["id"]), "id": t["id"], "title": t["title"],
                           "notes": (t["notes"] or "")[:800], "progress": t["progress"],
                           "plan": t["progress_note"] or "", "latest_progress": list(reversed(progress))}
    return out


def status_line(snap: dict) -> str:
    """The code-built status, in Czech: what is going on, from the snapshot only."""
    t = snap.get("task")
    what = f"na {t['ref']} ({t['title']})" if t else "na běhu bez úkolu"
    parts = [f"Pracuju {what}, běží {snap.get('minutes', 0)} min"]
    if snap.get("count"):
        parts.append(f"hotových kroků {snap['count']}")
    if snap.get("recent"):
        parts.append(f"poslední krok: {snap['recent'][-1][0]}")
    if t and t.get("plan"):
        parts.append(f"plán: {t['plan'][:200]}")
    return ", ".join(parts) + "."


def code_reply(snap: dict, why: str | None = None) -> str:
    body = (f"{status_line(snap)} Právě jsem uprostřed kroku; tvou zprávu si přečtu, jakmile doběhne, "
            f"a odpovím na ni.")
    if why:
        body += f" (Rychlou odpověď teď nedám: {why}.)"
    return body


# ------------------------------------------------------------------ the fast answer

def _prompt(snap: dict, author: str, body: str) -> str:
    from .guard.external import wrap_external

    t = snap.get("task") or {}
    lines = [
        f"Jsi rychlý hlas agenta {snap['agent']}. Agent právě pracuje a je uprostřed dlouhého kroku, takže "
        f"zprávu od {author} ještě nečetl. Odpověz za něj hned, česky, 1 až 3 krátké věty, v 1. osobě.",
        "Pravidla: vycházej jen ze snímku níže. Nikdy netvrď, že je něco hotové nebo uděláno, pokud to ve "
        "snímku není. Nic nerozhoduj a nic neslibuj: pokyn nebo změnu jen potvrď a řekni, že ji předáš hlavnímu "
        "běhu, jakmile doběhne aktuální krok. Na otázku odpověz ze snímku, a když odpověď ve snímku není, řekni "
        "to a že odpoví hlavní běh. Žádné nástroje, žádný úvod ani podpis.",
        "",
        "# Snímek práce",
        f"- kdo jsem: {snap['agent']}" + (f" ({snap['who']})" if snap.get("who") else ""),
        f"- úkol: {t.get('ref', '-')} {t.get('title', '')}".rstrip(),
        f"- běží: {snap.get('minutes', 0)} min, hotových kroků: {snap.get('count', 0)}",
        f"- plán / poslední poznámka: {t.get('plan') or '-'}",
        f"- průběh (%): {t.get('progress') if t.get('progress') is not None else '-'}",
    ]
    if t.get("latest_progress"):
        lines.append("- poslední hlášení: " + " | ".join(p[:200] for p in t["latest_progress"]))
    if snap.get("recent"):
        lines.append("- poslední kroky: " + "; ".join(r[0] for r in snap["recent"]))
    if t.get("notes"):
        lines += ["", "# Zadání úkolu (zkráceno)", t["notes"]]
    lines += ["", f"# Zpráva od {author}", wrap_external(f"chat:{author}", body)]
    return "\n".join(lines)


def _llm_blocked(conn: sqlite3.Connection, actor_id: int) -> str | None:
    """Why the fast answer cannot use the model now (Czech), or None."""
    from . import availability, engines, runner

    why = availability.why_not(conn, actor_id)
    if why:
        return why["reason"]
    if not runner.available("claude"):
        return "model pro rychlé odpovědi tu není k dispozici"
    until = engines.paused_until(conn, "claude")
    if until:
        return f"Claude má vyčerpaný limit do {availability.when_cz(until)}"
    return None


def ask_model(conn: sqlite3.Connection, actor_id: int, prompt: str) -> str:
    """One tool-less call, recorded as a run of the agent (usage and budget). Raises on failure."""
    from . import integrations, runner

    integrations.install()  # the budget gate and usage record (idempotent; already on in the API)
    res = runner.run(conn, runner.RunRequest(actor_id, "chat_fastlane", prompt, engine="claude",
                                             model=FASTLANE_MODEL, timeout_s=60))
    if res.status != "ok" or not res.output.strip():
        raise RuntimeError(res.error or res.status)
    return res.output.strip()


def respond(conn: sqlite3.Connection, actor_id: int, message_id: int) -> dict | None:
    """The fast answer to one message; None when it is not due (read meanwhile, already answered,
    the run ended)."""
    m = conn.execute("SELECT m.*, a.name AS author_name FROM chat_messages m JOIN actors a ON a.id = m.author_id "
                     "WHERE m.id = ?", (message_id,)).fetchone()
    inbox = conn.execute("SELECT read_at FROM chat_inbox WHERE message_id = ? AND actor_id = ?",
                         (message_id, actor_id)).fetchone()
    run = live_run(conn, actor_id)
    if m is None or inbox is None or inbox["read_at"] or run is None or m["archived_at"]:
        return None
    if conn.execute("SELECT 1 FROM audit_log WHERE action = 'chat_fastlane' AND entity = 'chat_message' "
                    "AND entity_id = ? AND actor_id = ?", (message_id, actor_id)).fetchone():
        return None
    ctx = Ctx(actor_id, via="system")
    audit.log(conn, ctx, "chat_fastlane", "chat_message", message_id, run=run["id"])  # once, even if it fails
    conn.commit()
    thread = m["reply_to"] or m["id"]
    chat.agent_typing(m["channel_id"], actor_id, None, thread)
    snap = snapshot(conn, actor_id, run)
    blocked = _llm_blocked(conn, actor_id)
    mode = "code"
    if blocked:
        body = code_reply(snap, blocked)
    else:
        try:
            body = ask_model(conn, actor_id, _prompt(snap, m["author_name"], m["body"]))
            mode = "model"
        except Exception as e:  # noqa: BLE001 - never silence: the code-built status goes out instead
            log.info("fast lane model failed for %s: %s", actor_id, e)
            body = code_reply(snap, "rychlý model teď neodpovídá")
    if conn.execute("SELECT 1 FROM chat_messages WHERE channel_id = ? AND author_id = ? AND id > ? "
                    "AND reply_to = ? AND archived_at IS NULL", (m["channel_id"], actor_id, m["id"], thread)).fetchone():
        chat.typing_clear(m["channel_id"], actor_id)  # the run answered meanwhile: no second answer
        return None
    try:
        chat._add_member(conn, m["channel_id"], actor_id)
        out = chat.send(conn, ctx, m["channel_id"], body[:2000], reply_to=thread, system=True)
    except Exception:  # noqa: BLE001
        log.exception("fast lane reply failed")
        chat.typing_clear(m["channel_id"], actor_id)
        return None
    audit.log(conn, ctx, "chat_fastlane_reply", "chat_message", out["id"], to_message=message_id, mode=mode,
              run=run["id"])
    conn.commit()
    return {**out, "mode": mode}


def due(conn: sqlite3.Connection, now: datetime | None = None) -> list[tuple[int, int]]:
    """(agent, message) pairs waiting for a fast answer: a person's DM, mention or reply to an agent
    with a live run, unread by that run for FASTLANE_AFTER_S."""
    now = now or datetime.now(timezone.utc)
    newest = (now - timedelta(seconds=FASTLANE_AFTER_S)).isoformat(timespec="seconds")
    oldest = (now - timedelta(seconds=FASTLANE_MAX_AGE_S)).isoformat(timespec="seconds")
    live = (now - timedelta(seconds=LIVE_S)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT i.actor_id, i.message_id FROM chat_inbox i
           JOIN chat_messages m ON m.id = i.message_id JOIN actors au ON au.id = m.author_id
           JOIN actors ag ON ag.id = i.actor_id
           WHERE i.read_at IS NULL AND i.reason IN ('dm', 'mention', 'reply') AND m.archived_at IS NULL
             AND COALESCE(m.priority, 'fyi') != 'stop' AND au.kind = 'human' AND ag.kind != 'human'
             AND m.created_at <= ? AND m.created_at >= ?
             AND EXISTS (SELECT 1 FROM runs r WHERE r.actor_id = i.actor_id AND r.status = 'running'
                         AND r.kind != 'chat_fastlane' AND COALESCE(r.heartbeat_at, r.started_at) >= ?)
             AND NOT EXISTS (SELECT 1 FROM audit_log l WHERE l.action = 'chat_fastlane'
                             AND l.entity = 'chat_message' AND l.entity_id = i.message_id AND l.actor_id = i.actor_id)
           ORDER BY i.message_id""", (newest, oldest, live)).fetchall()
    return [(r["actor_id"], r["message_id"]) for r in rows]


def tick(db_path) -> int:
    from .db import connect

    conn = connect(db_path)
    n = 0
    try:
        for aid, mid in due(conn):
            if respond(conn, aid, mid):
                n += 1
    finally:
        conn.close()
    return n


async def loop(db_path, interval_s: float = 10) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            await asyncio.to_thread(tick, db_path)
        except Exception:  # noqa: BLE001 - keep ticking
            log.exception("fast lane tick failed")


def current_work(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per busy agent: the task its live run works on and for how long (the chat's "pracuje na T-046 · 12 min")."""
    from . import tasks

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LIVE_S)).isoformat(timespec="seconds")
    out: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT r.actor_id, r.task_id, r.started_at, t.title FROM runs r LEFT JOIN tasks t ON t.id = r.task_id "
            "WHERE r.status = 'running' AND r.kind != 'chat_fastlane' AND COALESCE(r.heartbeat_at, r.started_at) >= ? "
            "ORDER BY r.id", (cutoff,)):
        out[r["actor_id"]] = {"task_id": r["task_id"], "task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None,
                              "title": r["title"], "since": r["started_at"], "minutes": _minutes_since(r["started_at"])}
    return out


