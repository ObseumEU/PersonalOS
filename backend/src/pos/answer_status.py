"""Who answers the owner's message, and where it stands: the line under his message in the chat.

The owner wrote and did not know whether anything happened (prod 2026-10-13, #kniha: "Dejte mi link kde
si to můžu otestovat!", and the CEO's DM showed "nikdo nepíše" while T-1398 was working). For his latest
message in a conversation this derives, from the chat-answer task (pos.chat._ask_to_answer) and its runs,
one status that is updated in place, never posted as a message (it wakes nobody and costs nothing):

  queued     "<Agent> to vzal, odpoví do pár minut"     the task waits for its run
  working    "<Agent> píše…"                            a run on the task is live
  held       "<Agent> čeká na rozpočet, odpoví nejpozději HH:MM"   the reserve, a cap or its own limit
  unclaimed  "Zatím to nikdo nevzal" + "Připomenout"    queued for UNCLAIMED_S with no run, or no task and
                                                        no agent reply at all
  answered   "<Agent> odpověděl"                        the task closed and its agent posted after him

The stream (pos.chat.stream) sends it with the presence; the typing indicator gets the same agents
(queued / held / working) so a conversation never says "nikdo nepíše" while its answer is on the way.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from . import actors, audit, chat
from .core import Ctx, Forbidden, NotFound

WINDOW_MIN = 30  # the owner's messages this recent get a status (an open task keeps its own longer)
UNCLAIMED_S = 120  # queued this long with no run: "Zatím to nikdo nevzal"
ANSWERED_KEEP_MIN = 10  # "odpověděl" stays this long after the task closed
CACHE_S = 3.0  # the stream polls every second per client; the snapshot is shared
OPEN = ("inbox", "next", "working")

_cache: dict[str, tuple[float, dict[int, dict]]] = {}
_lock = threading.Lock()


def invalidate() -> None:
    """A message or a task changed: the next read computes afresh."""
    with _lock:
        _cache.clear()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _live_run(conn: sqlite3.Connection, task_id: int, now: datetime) -> bool:
    from .api_worker import LIVE_RUN_MINUTES

    cutoff = _iso(now - timedelta(minutes=LIVE_RUN_MINUTES))
    return conn.execute("""SELECT 1 FROM runs WHERE task_id = ? AND status = 'running'
                           AND COALESCE(heartbeat_at, started_at) >= ?""", (task_id, cutoff)).fetchone() is not None


def _ever_ran(conn: sqlite3.Connection, task_id: int) -> bool:
    return conn.execute("SELECT 1 FROM runs WHERE task_id = ?", (task_id,)).fetchone() is not None


def budget_hold(conn: sqlite3.Connection, task: sqlite3.Row) -> tuple[bool, str | None]:
    """(held, until): the task waits for money, not for a worker. Read-only (pos.access, the picker's checks)."""
    from .access import service as access
    from .api_worker import _reserve_admits

    aid = task["assignee_id"]
    try:
        until = access.limited_until(conn, aid)
        if until:
            return True, until
        cap = access.admission(conn, aid)
        if cap:
            return True, cap.get("until") or cap.get("queue_until")
        if task["status"] != "working" and access.business_only(conn) and not _reserve_admits(conn, task, {}):
            return True, access.business_only_until(conn)
    except sqlite3.OperationalError:  # budget tables missing (an old database): never held
        return False, None
    return False, None


def _agents(conn: sqlite3.Connection, channel_id: int, owner: int) -> list[int]:
    return [r[0] for r in conn.execute(
        """SELECT a.id FROM channel_members cm JOIN actors a ON a.id = cm.actor_id
           WHERE cm.channel_id = ? AND a.kind != 'human' AND a.archived_at IS NULL AND a.id != ?""",
        (channel_id, owner))]


def _replied(conn: sqlite3.Connection, msg: sqlite3.Row, by: int | None = None) -> bool:
    """An agent (or `by`) posted in this conversation after the message."""
    q = """SELECT 1 FROM chat_messages m JOIN actors a ON a.id = m.author_id
           WHERE m.channel_id = ? AND m.id > ? AND m.archived_at IS NULL AND a.kind != 'human'"""
    args: list = [msg["channel_id"], msg["id"]]
    if by is not None:
        q += " AND m.author_id = ?"
        args.append(by)
    return conn.execute(q + " LIMIT 1", args).fetchone() is not None


def _chat_tasks(conn: sqlite3.Connection, now: datetime) -> dict[int, list[sqlite3.Row]]:
    """Chat-answer tasks open now or closed lately, by the channel they answer (newest first)."""
    since = _iso(now - timedelta(minutes=ANSWERED_KEEP_MIN))
    rows = conn.execute(
        f"""SELECT * FROM tasks WHERE topic = 'chat' AND archived_at IS NULL
            AND (status IN ({','.join('?' * len(OPEN))}) OR updated_at >= ?) ORDER BY id DESC""",
        (*OPEN, since)).fetchall()
    out: dict[int, list[sqlite3.Row]] = {}
    for t in rows:
        origin = chat.chat_origin(conn, t["id"])
        if origin:
            out.setdefault(origin[0], []).append(t)
    return out


def _status(conn: sqlite3.Connection, msg: sqlite3.Row, tasks_here: list[sqlite3.Row], owner: int,
            now: datetime, names: dict) -> dict | None:
    base = {"message_id": msg["id"], "channel_id": msg["channel_id"],
            "thread": (msg["reply_to"] or None), "since": msg["created_at"]}
    open_ = [t for t in tasks_here if t["status"] in OPEN]
    task = open_[0] if open_ else None
    if task is not None:
        aid = task["assignee_id"]
        who = {"agent_id": aid, "agent": names[aid]["name"] if aid in names else None, "task": f"T-{task['id']}"}
        if _live_run(conn, task["id"], now):
            return {**base, **who, "state": "working", "until": None}
        if _replied(conn, msg, aid):  # it answered; the task closes after its run
            return {**base, **who, "state": "answered", "until": None}
        held, until = budget_hold(conn, task)
        if held:
            return {**base, **who, "state": "held", "until": until}
        retry = _parse(task["retry_after"])
        waited = (now - (_parse(msg["created_at"]) or now)).total_seconds()
        if retry is None and waited >= UNCLAIMED_S and not _ever_ran(conn, task["id"]):
            return {**base, **who, "state": "unclaimed", "until": None}
        return {**base, **who, "state": "queued", "until": task["retry_after"] if retry and retry > now else None}
    done = [t for t in tasks_here if t["status"] not in OPEN and (t["updated_at"] or "") >= msg["created_at"]]
    if done and _replied(conn, msg, done[0]["assignee_id"]):
        aid = done[0]["assignee_id"]
        return {**base, "agent_id": aid, "agent": names[aid]["name"] if aid in names else None,
                "task": f"T-{done[0]['id']}", "state": "answered", "until": None}
    if _replied(conn, msg):
        return None  # someone answered: the answer is the status
    age = (now - (_parse(msg["created_at"]) or now)).total_seconds()
    if age >= UNCLAIMED_S and _agents(conn, msg["channel_id"], owner):
        return {**base, "agent_id": None, "agent": None, "task": None, "state": "unclaimed", "until": None}
    return None


def _wants_answer(msg: sqlite3.Row) -> bool:
    return msg["priority"] != "stop" and not chat.is_ack(msg["body"] or "")


def compute(conn: sqlite3.Connection, now: datetime | None = None) -> dict[int, dict]:
    """{channel id: status} for the owner's latest message in each conversation that waits for, or just got, an
    answer (no viewer filter)."""
    now = now or datetime.now(timezone.utc)
    owner = actors.owner_id(conn)
    by_channel = _chat_tasks(conn, now)
    since = _iso(now - timedelta(minutes=WINDOW_MIN))
    channels = {r[0] for r in conn.execute(
        "SELECT DISTINCT channel_id FROM chat_messages WHERE author_id = ? AND created_at >= ? AND archived_at IS NULL",
        (owner, since))}
    channels |= {c for c, ts in by_channel.items() if any(t["status"] in OPEN for t in ts)}
    names = chat._names(conn)
    out: dict[int, dict] = {}
    for cid in channels:
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
        if ch is None or ch["archived_at"] or (ch["name"] or "").lower() in chat.UNROUTED:
            continue
        msg = conn.execute("""SELECT * FROM chat_messages WHERE channel_id = ? AND author_id = ? AND archived_at IS NULL
                              ORDER BY id DESC LIMIT 1""", (cid, owner)).fetchone()
        if msg is None or not _wants_answer(msg):
            continue
        st = _status(conn, msg, by_channel.get(cid, []), owner, now, names)
        if st:
            out[cid] = st
    return out


def snapshot(conn: sqlite3.Connection, cached: bool = True) -> dict[int, dict]:
    key = conn.execute("PRAGMA database_list").fetchone()[2]
    now = time.monotonic()
    if cached:
        with _lock:
            hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    snap = compute(conn)
    with _lock:
        _cache[key] = (now, snap)
    return snap


def view(conn: sqlite3.Connection, viewer: int, cached: bool = True) -> dict[str, dict]:
    """The statuses of the conversations the viewer may read."""
    out = {}
    for cid, st in snapshot(conn, cached).items():
        try:
            if chat.can_read(conn, chat._channel(conn, cid), viewer):
                out[str(cid)] = st
        except NotFound:
            continue
    return out


TYPING_STATE = {"queued": "queued", "unclaimed": "queued", "held": "held", "working": "working"}


def typing_entries(conn: sqlite3.Connection, statuses: dict[str, dict], viewer: int) -> dict[str, list[dict]]:
    """The agent an answer waits for, as a typing entry (state queued / held / working)."""
    out: dict[str, list[dict]] = {}
    names = None
    for cid, st in statuses.items():
        state = TYPING_STATE.get(st["state"])
        if state is None or not st.get("agent_id") or st["agent_id"] == viewer:
            continue
        names = names or chat._names(conn)
        a = names.get(st["agent_id"])
        if a is None:
            continue
        out[cid] = [{"id": a["id"], "name": a["name"], "kind": a["kind"], "state": state, "thread": st["thread"],
                     "until": st.get("until")}]
    return out


def merge_typing(typing: dict[str, list[dict]], extra: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Live typing wins; an agent whose answer is on its way is added where it is not typing already."""
    out = {k: list(v) for k, v in typing.items()}
    for cid, rows in extra.items():
        have = {e["id"] for e in out.get(cid, [])}
        add = [e for e in rows if e["id"] not in have]
        if add:
            out[cid] = out.get(cid, []) + add
    return out


def remind(conn: sqlite3.Connection, ctx: Ctx, message_id: int) -> dict:
    """"Připomenout": the owner's message nobody took. An open task goes back to the front of its agent's queue
    (no back-off, `next`, its worker woken); without one, the message is routed again as when it was sent
    (the DM's agent, those named, else the channel's lead) and gets its chat-answer task."""
    from . import comments, tasks, versioning, wake

    msg = conn.execute("SELECT * FROM chat_messages WHERE id = ? AND archived_at IS NULL", (message_id,)).fetchone()
    if msg is None:
        raise NotFound(f"message {message_id}")
    if msg["author_id"] != ctx.actor_id:

        raise Forbidden("only its author reminds a message")
    ch = chat._channel(conn, msg["channel_id"])
    task = next((t for t in _chat_tasks(conn, datetime.now(timezone.utc)).get(ch["id"], []) if t["status"] in OPEN),
                None)
    if task is not None:
        changes = {"retry_after": None} | ({"status": "next"} if task["status"] == "inbox" else {})
        if task["retry_after"] or task["status"] == "inbox":
            versioning.update(conn, ctx, tasks.ENTITY, task["id"], changes, action="resume")
        comments.log(conn, ctx, task["id"], f"The owner reminded message {message_id}: nobody has answered yet.",
                     "comment")
        audit.log(conn, ctx, "chat_remind", "task", task["id"], channel=ch["id"], message=message_id)
        conn.commit()
        wake.wake(task["assignee_id"])
    else:
        owner = actors.owner_id(conn)
        if ch["kind"] == "dm":
            targets = [a for a in chat.member_ids(conn, ch["id"]) if a != ctx.actor_id]
        else:
            targets = [a for a in chat._mentions(conn, msg["body"], None, ch["id"]) if a != ctx.actor_id]
            if not targets:
                parent = (conn.execute("SELECT * FROM chat_messages WHERE id = ?", (msg["reply_to"],)).fetchone()
                          if msg["reply_to"] else None)
                hit = chat._route_unaddressed(conn, ch, parent)
                targets = [hit[0]] if hit and hit[0] != owner else []
        for aid in targets:
            chat._ask_to_answer(conn, ctx, ch, aid, message_id, msg["body"], None)
        audit.log(conn, ctx, "chat_remind", "chat_message", message_id, channel=ch["id"], targets=targets)
        conn.commit()
        for aid in targets:
            wake.wake(aid)
    invalidate()
    return compute(conn).get(ch["id"]) or {"message_id": message_id, "channel_id": ch["id"], "state": "queued"}
