"""Meetings: a structured, bounded discussion of several agents in a chat thread.

`meeting_start(channel, topic, agenda, participants, rounds=2, facilitator=<lead>)` opens a thread
in the channel with the agenda. The platform then gives the floor to one participant at a time
(round-robin), so the channel reads like a real discussion and the typing indicator shows who
speaks:

- round 1: each participant posts a position or proposal with its evidence (citations);
- round 2 (and further): each answers the others (agrees, challenges, builds on it), short;
- then the facilitator decides (meeting_decide): what we do and do not do and why; the tasks it
  names are created (assignee, definition of done) in the channel's project, the decision goes
  into the project's decision log and the meeting closes.

Each turn is an ordinary task for that agent ("Porada: …"), whose notes carry the agenda and the
thread so far, clipped: never the channel's history. Bounds: at most TURN_MAX characters per turn,
a budget per meeting (the cost of the turns' runs), a maximum duration, a time limit per turn. A
participant that fails, hands back or times out is skipped and the next one goes on. The owner may
write into the thread at any time: his message is highlighted in every later turn's context and
the facilitator must take it into account. Meeting threads are exempt from the chat's loop
detection (pos.chat._loop): taking turns is their point, and the bounds above end them.

A schedule with the kind `meeting` (pos.schedules) starts one on its cadence.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit
from .core import Ctx, Forbidden, NotFound, now_iso

TURN_MAX = 1500       # characters per turn: a few short paragraphs
DECISION_MAX = 4000
CONTEXT_MAX = 8000    # the thread so far, as a turn's context
CLIP = 900            # one message in that context
MAX_PARTICIPANTS = 8
MAX_ROUNDS = 3
KINDS = {"position": "návrh", "response": "reakce", "decision": "rozhodnutí"}

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS meetings (
        id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL REFERENCES channels(id),
        root_message_id INTEGER REFERENCES chat_messages(id),
        topic TEXT NOT NULL,
        agenda TEXT NOT NULL DEFAULT '',
        participants TEXT NOT NULL,
        facilitator_id INTEGER NOT NULL REFERENCES actors(id),
        rounds INTEGER NOT NULL DEFAULT 2,
        status TEXT NOT NULL DEFAULT 'running',
        seq INTEGER NOT NULL DEFAULT -1,
        budget_usd REAL NOT NULL,
        max_minutes INTEGER NOT NULL,
        turn_minutes INTEGER NOT NULL,
        project_id INTEGER,
        schedule_id INTEGER,
        decision TEXT,
        decision_message_id INTEGER,
        close_reason TEXT,
        created_by INTEGER NOT NULL REFERENCES actors(id),
        started_at TEXT NOT NULL,
        deadline_at TEXT NOT NULL,
        closed_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS meetings_running ON meetings (status, channel_id)",
    """CREATE TABLE IF NOT EXISTS meeting_turns (
        id INTEGER PRIMARY KEY,
        meeting_id INTEGER NOT NULL REFERENCES meetings(id),
        seq INTEGER NOT NULL,
        round INTEGER NOT NULL,
        kind TEXT NOT NULL,
        actor_id INTEGER NOT NULL REFERENCES actors(id),
        task_id INTEGER REFERENCES tasks(id),
        message_id INTEGER REFERENCES chat_messages(id),
        status TEXT NOT NULL DEFAULT 'open',
        note TEXT,
        started_at TEXT NOT NULL,
        ended_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS meeting_turns_meeting ON meeting_turns (meeting_id, seq)",
    "CREATE INDEX IF NOT EXISTS meeting_turns_task ON meeting_turns (task_id)",
    "CREATE INDEX IF NOT EXISTS meeting_turns_message ON meeting_turns (message_id)",
)


class MeetingError(ValueError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in SCHEMA:
        conn.execute(sql)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _row(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if row is None:
        raise NotFound(f"meeting {meeting_id}")
    return row


def _participants(m: sqlite3.Row) -> list[int]:
    return json.loads(m["participants"] or "[]")


def plan(m: sqlite3.Row) -> list[tuple[int, str, int]]:
    """(round, kind, actor) for every turn, in order: the rounds, then the facilitator's decision."""
    people = _participants(m)
    turns = [(r, "position" if r == 1 else "response", a) for r in range(1, m["rounds"] + 1) for a in people]
    return [*turns, (m["rounds"] + 1, "decision", m["facilitator_id"])]


# ------------------------------------------------------------------ lookups used by chat

def for_thread(conn: sqlite3.Connection, channel_id: int, reply_to: int | None) -> sqlite3.Row | None:
    """The running meeting whose thread this reply is in (None: an ordinary message)."""
    if not reply_to:
        return None
    try:
        return conn.execute(
            """SELECT m.* FROM meetings m WHERE m.status = 'running' AND m.channel_id = ? AND m.root_message_id = (
                   SELECT COALESCE(reply_to, id) FROM chat_messages WHERE id = ?)""",
            (channel_id, reply_to)).fetchone()
    except sqlite3.OperationalError:  # no meetings table yet
        return None


def open_turn(conn: sqlite3.Connection, meeting_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM meeting_turns WHERE meeting_id = ? AND status = 'open' ORDER BY seq DESC "
                        "LIMIT 1", (meeting_id,)).fetchone()


def check_turn_message(conn: sqlite3.Connection, m: sqlite3.Row, author_id: int, body: str) -> None:
    """Before a message lands in a meeting thread: people write any time; an agent speaks only on
    its turn, and briefly."""
    from .chat import ChatError

    author = actors.get(conn, author_id)
    if author["kind"] == "human":
        return
    turn = open_turn(conn, m["id"])
    if turn is None or turn["actor_id"] != author_id:
        raise ChatError(f"this thread is meeting {m['id']} and it is not your turn: the platform gives each "
                        f"participant the floor in turn (you get a task when it is yours)")
    if turn["kind"] == "decision":
        return  # meeting_decide posts it; a plain message is taken as the decision
    if len(body) > TURN_MAX:
        raise ChatError(f"a meeting turn is at most {TURN_MAX} characters (a few short paragraphs); shorten it")


def may_post(conn: sqlite3.Connection, actor_id: int, channel_id: int) -> bool:
    """An agent whose turn it is may post in that meeting's thread without messages:send."""
    try:
        return conn.execute("""SELECT 1 FROM meeting_turns t JOIN meetings m ON m.id = t.meeting_id
                               WHERE t.status = 'open' AND t.actor_id = ? AND m.status = 'running'
                               AND m.channel_id = ?""", (actor_id, channel_id)).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def annotations(conn: sqlite3.Connection, message_ids: list[int]) -> dict[int, dict]:
    """For the chat view: which messages open a meeting (agenda), are a turn (round, kind) or its
    decision, so the thread shows round markers and a highlighted decision."""
    if not message_ids:
        return {}
    q = ",".join("?" * len(message_ids))
    out: dict[int, dict] = {}
    try:
        for m in conn.execute(f"""SELECT id, root_message_id, decision_message_id, topic, rounds, status FROM meetings
                                  WHERE root_message_id IN ({q}) OR decision_message_id IN ({q})""",
                              [*message_ids, *message_ids]):
            base = {"meeting": m["id"], "topic": m["topic"], "rounds": m["rounds"], "status": m["status"]}
            if m["root_message_id"] in message_ids:
                out[m["root_message_id"]] = {**base, "kind": "agenda"}
            if m["decision_message_id"] in message_ids:
                out[m["decision_message_id"]] = {**base, "kind": "decision"}
        for t in conn.execute(f"""SELECT t.message_id, t.meeting_id, t.round, t.kind, m.rounds FROM meeting_turns t
                                  JOIN meetings m ON m.id = t.meeting_id WHERE t.message_id IN ({q})""",
                              message_ids):
            out.setdefault(t["message_id"], {"meeting": t["meeting_id"], "round": t["round"], "kind": t["kind"],
                                             "rounds": t["rounds"]})
    except sqlite3.OperationalError:
        return {}
    return out


# ------------------------------------------------------------------ start

def _resolve_agent(conn: sqlite3.Connection, ref) -> sqlite3.Row:
    from . import chat

    row = chat.resolve_actor(conn, ref)
    if row["kind"] == "human" or row["runtime"] == "service":
        raise MeetingError(f"{row['name']} cannot take part: meetings are for agents with a worker")
    return row


def _project_of(conn: sqlite3.Connection, ch: sqlite3.Row) -> int | None:
    try:
        row = conn.execute("SELECT id FROM projects WHERE channel_id = ? OR lower(slug) = lower(?)",
                           (ch["id"], ch["name"] or "")).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["id"] if row else None


def start(conn: sqlite3.Connection, ctx: Ctx, channel, topic: str, agenda: str | list[str] = "",
          participants: list | None = None, rounds: int = 2, facilitator=None, *,
          budget_usd: float | None = None, max_minutes: int | None = None, turn_minutes: int | None = None,
          schedule_id: int | None = None) -> dict:
    """Open a meeting in a group channel and give the first participant the floor."""
    from . import chat
    from .org import manages

    ensure_schema(conn)
    ch = chat.resolve_channel(conn, channel)
    if ch["kind"] != "group" or ch["archived_at"]:
        raise MeetingError("a meeting runs in a group channel")
    if (ch["name"] or "").lower() in chat.UNROUTED:
        raise MeetingError(f"#{ch['name']} is not for meetings")
    topic = " ".join((topic or "").split())
    if not topic:
        raise MeetingError("a meeting needs a topic")
    if isinstance(agenda, list):
        agenda = "\n".join(f"- {str(a).strip()}" for a in agenda if str(a).strip())
    agenda = (agenda or "").strip()[:3000]
    people: list[int] = []
    for p in participants or []:
        aid = _resolve_agent(conn, p)["id"]
        if aid not in people:
            people.append(aid)
    if not people:
        raise MeetingError("a meeting needs participants (agent names)")
    if len(people) > MAX_PARTICIPANTS:
        raise MeetingError(f"at most {MAX_PARTICIPANTS} participants")
    try:
        rounds = int(rounds)
    except (TypeError, ValueError):
        rounds = 2
    if not 1 <= rounds <= MAX_ROUNDS:
        raise MeetingError(f"rounds is 1 to {MAX_ROUNDS}")
    fac = _resolve_agent(conn, facilitator)["id"] if facilitator not in (None, "") else chat.channel_lead(conn, ch)
    if fac is None:
        fac = people[0]
    me = actors.get(conn, ctx.actor_id)
    if me["kind"] != "human" and ctx.actor_id != fac and not manages(conn, ctx.actor_id, fac):
        raise Forbidden("an agent starts a meeting it facilitates (or one its report facilitates)")
    if conn.execute("SELECT 1 FROM meetings WHERE channel_id = ? AND status = 'running'", (ch["id"],)).fetchone():
        raise MeetingError(f"a meeting is already running in #{ch['name']}; one at a time")
    budget = budget_usd if budget_usd is not None else _env_float("POS_MEETING_BUDGET_USD", 3.0)
    minutes = int(max_minutes or _env_float("POS_MEETING_MAX_MINUTES", 180))
    per_turn = int(turn_minutes or _env_float("POS_MEETING_TURN_MINUTES", 20))
    for aid in {*people, fac}:
        chat._add_member(conn, ch["id"], aid)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors")}
    body = (f"📋 **Porada: {topic}**\n\n**Program:**\n{agenda or '- (bez programu)'}\n\n"
            f"**Účastníci:** {', '.join(names[a] for a in people)} · **Kol:** {rounds} · "
            f"**Rozhoduje:** {names[fac]}\n"
            f"_Kolo 1: návrh s důkazy · další kola: reakce na ostatní · pak rozhodnutí. Mluví se po jednom, "
            f"ve vlákně této zprávy; majitel může kdykoli vstoupit._")
    root = chat.send(conn, Ctx(fac, via="system"), ch["id"], body, system=True)
    started = _now()
    cur = conn.execute(
        """INSERT INTO meetings (channel_id, root_message_id, topic, agenda, participants, facilitator_id, rounds,
                                 budget_usd, max_minutes, turn_minutes, project_id, schedule_id, created_by,
                                 started_at, deadline_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (ch["id"], root["id"], topic, agenda, json.dumps(people), fac, rounds, budget, minutes, per_turn,
         _project_of(conn, ch), schedule_id, ctx.actor_id, _iso(started),
         _iso(started + timedelta(minutes=minutes))))
    mid = cur.lastrowid
    audit.log(conn, ctx, "meeting_start", "meeting", mid, channel=ch["id"], participants=people,
              facilitator=fac, rounds=rounds, schedule=schedule_id)
    conn.commit()
    _advance(conn, _row(conn, mid))
    return view(conn, mid)


# ------------------------------------------------------------------ turns

def spent(conn: sqlite3.Connection, meeting_id: int) -> float:
    row = conn.execute("""SELECT COALESCE(SUM(r.cost_usd), 0) FROM runs r WHERE r.task_id IN (
                              SELECT task_id FROM meeting_turns WHERE meeting_id = ? AND task_id IS NOT NULL)""",
                       (meeting_id,)).fetchone()
    return float(row[0] or 0)


def thread_context(conn: sqlite3.Connection, m: sqlite3.Row) -> str:
    """The thread so far (after the agenda), oldest first, clipped: the newest part when it is long.
    The owner's messages are marked: every later turn must take them into account."""
    from .guard.external import wrap_external

    rows = conn.execute("""SELECT c.id, c.body, a.name, a.is_owner, a.kind FROM chat_messages c
                           JOIN actors a ON a.id = c.author_id WHERE c.reply_to = ? AND c.archived_at IS NULL
                           ORDER BY c.id""", (m["root_message_id"],)).fetchall()
    turns = {t["message_id"]: t for t in conn.execute(
        "SELECT message_id, round, kind FROM meeting_turns WHERE meeting_id = ? AND message_id IS NOT NULL",
        (m["id"],))}
    lines: list[str] = []
    for r in rows:
        text = " ".join(r["body"].split())
        text = text[:CLIP] + ("…" if len(text) > CLIP else "")
        t = turns.get(r["id"])
        tag = f"kolo {t['round']}, {KINDS.get(t['kind'], t['kind'])}" if t else (
            "MAJITEL: vezmi v úvahu" if r["is_owner"] else "poznámka")
        lines.append(f"- [{tag}] {r['name']} (message {r['id']}): {text}")
    out, size = [], 0
    for line in reversed(lines):
        if size + len(line) > CONTEXT_MAX:
            out.append(f"- … ({len(lines) - len(out)} earlier messages left out)")
            break
        out.append(line)
        size += len(line)
    if not out:
        return ""
    owner_said = [line for line in lines if "[MAJITEL" in line]
    block = wrap_external(f"meeting:{m['id']}", "\n".join(reversed(out)), ref=f"thread {m['root_message_id']}")
    if owner_said:  # never cut away, even from a long thread
        block += "\n\nThe owner wrote into this meeting (take it into account):\n" + "\n".join(owner_said[-3:])
    return block


def _turn_notes(conn: sqlite3.Connection, m: sqlite3.Row, rnd: int, kind: str, actor_id: int) -> str:
    ch = conn.execute("SELECT id, name FROM channels WHERE id = ?", (m["channel_id"],)).fetchone()
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors")}
    others = [names[a] for a in _participants(m) if a != actor_id]
    head = (f"Purpose: your turn in the meeting “{m['topic']}” in #{ch['name']} (meeting {m['id']}, "
            + (f"round {rnd} of {m['rounds']}" if kind != "decision" else "the decision") + ").\n"
            f"Source: chat channel {ch['id']} (#{ch['name']}), message {m['root_message_id']}.\n\n"
            f"Agenda:\n{m['agenda'] or '(none)'}\n\nParticipants: {', '.join(names[a] for a in _participants(m))}; "
            f"facilitator: {names[m['facilitator_id']]}.")
    ctx_block = thread_context(conn, m)
    so_far = f"\n\nThe meeting so far (oldest first):\n{ctx_block}" if ctx_block else "\n\nYou open the meeting."
    post = f"chat_send(channel={ch['id']}, reply_to={m['root_message_id']})"
    if kind == "position":
        ask = (f"Your turn (round 1, position): post ONE message with {post}: your proposal or position on the "
               f"agenda, with the evidence for it (cite the company knowledge base: the `knowledge` tool's chunk "
               f"ids, or files and tasks by ref). At most {TURN_MAX} characters, a few short paragraphs.")
    elif kind == "response":
        ask = (f"Your turn (round {rnd}, response): post ONE short message with {post}: respond to the others "
               f"({', '.join(others) or 'the others'}): agree, challenge or build on specific points and name "
               f"whose; add evidence only where you disagree. Do not repeat your position. At most {TURN_MAX} "
               f"characters.")
    else:
        ask = (f"You facilitate and decide now. Weigh the positions and the owner's input, then call "
               f"meeting_decide(meeting_id={m['id']}, decision=..., why=..., not_doing=..., tasks=[{{title, "
               f"assignee, definition_of_done, notes}}], task_refs=[...]): it posts the decision in the thread, "
               f"creates the tasks (assignees among the participants and you), logs it in the project's "
               f"decision log and closes the meeting. Existing tasks you change yourself (update_task) and list "
               f"in task_refs.")
    rules = ("\n\nRules: speak only in the meeting thread and only now; do not message the other participants "
             "(each gets the floor from the platform). Then finish the run (complete_task).")
    return head + so_far + "\n\n" + ask + rules


def _advance(conn: sqlite3.Connection, m: sqlite3.Row, *, to_decision: bool = False) -> None:
    """Give the floor to the next turn (or the decision), or close the meeting when all is said."""
    from . import tasks, wake

    steps = plan(m)
    seq = len(steps) - 1 if to_decision else m["seq"] + 1
    if seq >= len(steps):
        close(conn, m["id"], "no decision was made", status="closed")
        return
    rnd, kind, aid = steps[seq]
    who = actors.get(conn, aid)
    if who["archived_at"] or who["paused_at"]:
        _record_skip(conn, m, seq, rnd, kind, aid, "paused or archived")
        _advance(conn, _row(conn, m["id"]))
        return
    conn.execute("UPDATE meetings SET seq = ? WHERE id = ?", (seq, m["id"]))
    m = _row(conn, m["id"])
    title = (f"Porada: {m['topic']} — rozhodnutí" if kind == "decision"
             else f"Porada: {m['topic']} — kolo {rnd} ({KINDS[kind]})")
    t = tasks.create(conn, Ctx(m["facilitator_id"], via="system"), {
        "title": title[:200], "assignee": {"type": "agent", "id": aid}, "status": "next", "priority": 1,
        "topic": "meeting", "reviewer": aid, "source": f"meeting:{m['id']}",
        "notes": _turn_notes(conn, m, rnd, kind, aid),
        "definition_of_done": ("The decision is posted with meeting_decide." if kind == "decision"
                               else "One message of yours is in the meeting thread."),
    })
    conn.execute("""INSERT INTO meeting_turns (meeting_id, seq, round, kind, actor_id, task_id, started_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (m["id"], seq, rnd, kind, aid, t["id"], now_iso()))
    audit.log(conn, Ctx(m["facilitator_id"], via="system"), "meeting_turn", "meeting", m["id"], seq=seq,
              round=rnd, kind=kind, actor=aid, task=t["id"])
    conn.commit()
    wake.wake(aid)


def _record_skip(conn: sqlite3.Connection, m: sqlite3.Row, seq: int, rnd: int, kind: str, aid: int,
                 why: str) -> None:
    conn.execute("UPDATE meetings SET seq = ? WHERE id = ?", (seq, m["id"]))
    conn.execute("""INSERT INTO meeting_turns (meeting_id, seq, round, kind, actor_id, status, note, started_at,
                                               ended_at) VALUES (?, ?, ?, ?, ?, 'skipped', ?, ?, ?)""",
                 (m["id"], seq, rnd, kind, aid, why, now_iso(), now_iso()))


def _end_turn(conn: sqlite3.Connection, turn: sqlite3.Row, status: str, *, message_id: int | None = None,
              note: str = "") -> None:
    from . import tasks

    conn.execute("UPDATE meeting_turns SET status = ?, message_id = COALESCE(?, message_id), note = ?, ended_at = ? "
                 "WHERE id = ?", (status, message_id, note[:300] or None, now_iso(), turn["id"]))
    t = conn.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (turn["task_id"],)).fetchone() \
        if turn["task_id"] else None
    if t is not None and t["status"] not in ("done", "review") and t["assignee_id"] == turn["actor_id"]:
        progress = "Posted in the meeting thread." if status == "posted" else f"Meeting turn skipped: {note}"
        try:
            tasks.update(conn, Ctx(turn["actor_id"], via="system"), turn["task_id"],
                         {"status": "done", "progress_note": progress[:500]})
        except Exception:  # noqa: BLE001 - the meeting goes on either way
            pass


def on_message(conn: sqlite3.Connection, m: sqlite3.Row, author_id: int, message_id: int,
               system: bool = False) -> None:
    """A message landed in the meeting thread: the speaker's turn is over, the next one gets the floor.
    A plain message from the facilitator on the decision turn is taken as the decision."""
    if system:
        return
    turn = open_turn(conn, m["id"])
    if turn is None or turn["actor_id"] != author_id:
        return  # the owner (or another person): the later turns see it in their context
    if turn["kind"] == "decision":
        body = conn.execute("SELECT body FROM chat_messages WHERE id = ?", (message_id,)).fetchone()["body"]
        _end_turn(conn, turn, "posted", message_id=message_id)
        conn.execute("UPDATE meetings SET decision = ?, decision_message_id = ? WHERE id = ?",
                     (body[:DECISION_MAX], message_id, m["id"]))
        _log_decision(conn, _row(conn, m["id"]), body, "")
        close(conn, m["id"], "decided")
        return
    _end_turn(conn, turn, "posted", message_id=message_id)
    conn.commit()
    _advance(conn, _row(conn, m["id"]))


def skip_task(conn: sqlite3.Connection, task_id: int, why: str) -> bool:
    """A meeting turn's task was handed back or failed: skip that turn, the next one goes on.
    True when the task was a meeting turn (nothing else to do for it: no owner notice)."""
    try:
        turn = conn.execute("SELECT * FROM meeting_turns WHERE task_id = ?", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return False
    if turn is None:
        return False
    if turn["status"] == "open":
        _skip(conn, _row(conn, turn["meeting_id"]), turn, why)
    return True


def _skip(conn: sqlite3.Connection, m: sqlite3.Row, turn: sqlite3.Row, why: str) -> None:
    from . import chat

    _end_turn(conn, turn, "skipped", note=why)
    name = actors.get(conn, turn["actor_id"])["name"]
    try:
        chat.send(conn, Ctx(m["facilitator_id"], via="system"), m["channel_id"],
                  f"⏭ _Platforma: {name} svůj tah nestihl ({why}); pokračuje další._",
                  reply_to=m["root_message_id"], system=True)
    except Exception:  # noqa: BLE001 - a note only
        pass
    audit.log(conn, Ctx(m["facilitator_id"], via="system"), "meeting_skip", "meeting", m["id"],
              actor=turn["actor_id"], why=why)
    conn.commit()
    m = _row(conn, m["id"])
    if m["status"] != "running":
        return
    if turn["kind"] == "decision":
        close(conn, m["id"], f"the facilitator did not decide ({why})", status="closed")
    else:
        _advance(conn, m)


def tick(conn: sqlite3.Connection) -> dict:
    """Scheduler (every minute): time limits and the budget. A turn over its time or whose task
    ended without a message is skipped; a meeting over its duration or budget goes straight to
    the decision, and closes when even that does not come."""
    ensure_schema(conn)
    out: dict = {}
    now = _now()
    for m in conn.execute("SELECT * FROM meetings WHERE status = 'running' ORDER BY id").fetchall():
        turn = open_turn(conn, m["id"])
        over_time = now > datetime.fromisoformat(m["deadline_at"])
        over_budget = spent(conn, m["id"]) >= m["budget_usd"]
        if turn is None:
            _advance(conn, m)
            out[m["id"]] = "advanced"
            continue
        if (over_time or over_budget) and turn["kind"] != "decision":
            why = "time limit" if over_time else "budget"
            _end_turn(conn, turn, "skipped", note=f"meeting {why} reached")
            conn.commit()
            _advance(conn, _row(conn, m["id"]), to_decision=True)
            out[m["id"]] = f"to decision ({why})"
            continue
        t = conn.execute("SELECT status, assignee_id, archived_at FROM tasks WHERE id = ?", (turn["task_id"],)).fetchone()
        started = datetime.fromisoformat(turn["started_at"])
        if t is None or t["archived_at"] or t["assignee_id"] != turn["actor_id"] or t["status"] in ("done", "review"):
            _skip(conn, m, turn, "the turn ended without a message")
            out[m["id"]] = "skipped"
        elif now - started > timedelta(minutes=m["turn_minutes"]) or (
                turn["kind"] == "decision" and over_budget and spent(conn, m["id"]) >= 1.5 * m["budget_usd"]):
            from . import runner

            for r in conn.execute("SELECT id FROM runs WHERE task_id = ? AND status = 'running'", (turn["task_id"],)):
                runner.cancel(conn, r["id"], "meeting turn over its time")
            _skip(conn, m, turn, f"over {m['turn_minutes']} min")
            out[m["id"]] = "timed out"
    return out


# ------------------------------------------------------------------ decision and close

def _log_decision(conn: sqlite3.Connection, m: sqlite3.Row, decision: str, why: str) -> None:
    """Into the project's decision log (pos.project_info, when present), else the audit log."""
    ctx = Ctx(m["facilitator_id"], via="system")
    who = actors.get(conn, m["facilitator_id"])["name"]
    text = f"Porada „{m['topic']}“: {decision}"[:2000]
    logged = False
    if m["project_id"]:
        try:
            from . import project_info

            project_info.add_log(conn, ctx, m["project_id"], text=text, why=(why or None), who=who,
                                 kind="decision")
            logged = True
        except Exception:  # noqa: BLE001 - no decision log in this version, or it refused: the audit keeps it
            logged = False
    audit.log(conn, ctx, "meeting_decision", "meeting", m["id"], project=m["project_id"], decision=text[:1000],
              why=(why or "")[:1000], project_log=logged)


def decide(conn: sqlite3.Connection, ctx: Ctx, meeting_id: int, decision: str, why: str = "", not_doing: str = "",
           tasks_: list[dict] | None = None, task_refs: list[str] | None = None) -> dict:
    """The facilitator's decision: posted in the thread (highlighted), its tasks created in the
    project, logged in the decision log; the meeting closes."""
    from . import chat, tasks

    m = _row(conn, meeting_id)
    me = actors.get(conn, ctx.actor_id)
    if ctx.actor_id != m["facilitator_id"] and not me["is_owner"]:
        raise Forbidden("only the meeting's facilitator (or the owner) decides")
    if m["status"] != "running":
        raise MeetingError(f"meeting {meeting_id} is {m['status']}")
    decision = (decision or "").strip()
    if not decision:
        raise MeetingError("a decision needs its text")
    allowed = {*_participants(m), m["facilitator_id"]}
    created = []
    for spec in (tasks_ or [])[:12]:
        title = str(spec.get("title") or "").strip()
        if not title:
            continue
        who = chat.resolve_actor(conn, spec.get("assignee") or m["facilitator_id"])
        if who["id"] not in allowed and not who["is_owner"]:
            raise MeetingError(f"{who['name']} was not in the meeting: assign its tasks to participants")
        fields = {"title": title[:200], "status": "next", "priority": int(spec.get("priority") or 2),
                  "assignee": {"type": "human" if who["kind"] == "human" else "agent", "id": who["id"]},
                  "definition_of_done": str(spec.get("definition_of_done") or "").strip() or None,
                  "notes": (f"{str(spec.get('notes') or '').strip()}\n\nSource: the decision of meeting "
                            f"{m['id']} “{m['topic']}” (chat channel {m['channel_id']}, thread "
                            f"{m['root_message_id']}).").strip(),
                  "source": f"meeting:{m['id']}"}
        if m["project_id"]:
            fields["project"] = m["project_id"]
        try:
            t = tasks.create(conn, ctx, dict(fields))
        except Exception:  # noqa: BLE001 - the project refused (visibility): the task still gets made
            fields.pop("project", None)
            t = tasks.create(conn, ctx, fields)
        created.append((t["ref"], who["name"]))
    refs = [str(r) for r in (task_refs or [])][:20]
    lines = [f"✅ **Rozhodnutí:** {decision}"]
    if why.strip():
        lines.append(f"**Proč:** {why.strip()}")
    if not_doing.strip():
        lines.append(f"**Neděláme:** {not_doing.strip()}")
    if created or refs:
        lines.append("**Úkoly:** " + ", ".join([f"{r} ({n})" for r, n in created] + refs))
    body = "\n".join(lines)[:DECISION_MAX]
    msg = chat.send(conn, Ctx(m["facilitator_id"], via="system"), m["channel_id"], body,
                    reply_to=m["root_message_id"], system=True)
    turn = open_turn(conn, m["id"])
    if turn is not None:
        _end_turn(conn, turn, "posted", message_id=msg["id"])
    conn.execute("UPDATE meetings SET decision = ?, decision_message_id = ? WHERE id = ?",
                 (body, msg["id"], m["id"]))
    _log_decision(conn, _row(conn, m["id"]), decision + (f" Neděláme: {not_doing}" if not_doing else ""), why)
    close(conn, m["id"], "decided")
    return {**view(conn, m["id"]), "tasks": [r for r, _ in created]}


def close(conn: sqlite3.Connection, meeting_id: int, reason: str, status: str = "closed") -> dict:
    """End the meeting; open turn tasks are closed; a note in the thread unless it was decided."""
    from . import chat

    m = _row(conn, meeting_id)
    if m["status"] != "running":
        return view(conn, meeting_id)
    turn = open_turn(conn, meeting_id)
    if turn is not None:
        _end_turn(conn, turn, "skipped", note=f"meeting closed: {reason}")
    conn.execute("UPDATE meetings SET status = ?, close_reason = ?, closed_at = ? WHERE id = ?",
                 (status, reason[:300], now_iso(), meeting_id))
    audit.log(conn, Ctx(m["facilitator_id"], via="system"), "meeting_close", "meeting", meeting_id, reason=reason)
    if reason != "decided":
        try:
            chat.send(conn, Ctx(m["facilitator_id"], via="system"), m["channel_id"],
                      f"🔚 _Porada skončila bez rozhodnutí: {reason}._", reply_to=m["root_message_id"], system=True)
        except Exception:  # noqa: BLE001
            pass
    conn.commit()
    return view(conn, meeting_id)


def view(conn: sqlite3.Connection, meeting_id: int) -> dict:
    from .tasks import display_id

    m = _row(conn, meeting_id)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors")}
    turns = [{"seq": t["seq"], "round": t["round"], "kind": t["kind"], "actor": names.get(t["actor_id"]),
              "status": t["status"], "message_id": t["message_id"], "note": t["note"],
              "task": display_id(t["task_id"]) if t["task_id"] else None}
             for t in conn.execute("SELECT * FROM meeting_turns WHERE meeting_id = ? ORDER BY seq, id", (meeting_id,))]
    cur = next((t for t in turns if t["status"] == "open"), None)
    return {"id": m["id"], "channel_id": m["channel_id"], "thread": m["root_message_id"], "topic": m["topic"],
            "agenda": m["agenda"], "participants": [names.get(a) for a in _participants(m)],
            "facilitator": names.get(m["facilitator_id"]), "rounds": m["rounds"], "status": m["status"],
            "now_speaking": cur["actor"] if cur else None, "turns": turns, "decision": m["decision"],
            "decision_message_id": m["decision_message_id"], "close_reason": m["close_reason"],
            "spent_usd": round(spent(conn, m["id"]), 4), "budget_usd": m["budget_usd"],
            "started_at": m["started_at"], "deadline_at": m["deadline_at"], "closed_at": m["closed_at"]}


def list_meetings(conn: sqlite3.Connection, channel_id: int | None = None, limit: int = 20) -> list[dict]:
    ensure_schema(conn)
    sql, args = "SELECT id FROM meetings", []
    if channel_id is not None:
        sql += " WHERE channel_id = ?"
        args.append(channel_id)
    return [view(conn, r["id"]) for r in conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*args, limit])]
