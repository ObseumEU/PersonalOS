"""The Chief of Staff ("Asistent vedení"): the weekly report and the weekly meeting.

Once a week (job `weekly_report`, default Friday 14:00 Europe/Prague, change
it on Automations or with POS_WEEKLY_REPORT_SCHEDULE) the core:

1. builds the week packet in code (pos.weekly_packet) and stores it;
2. gives the Chief of Staff one task: read the packet, write the narrative
   and the decisions needed, publish the report and open the meeting.

The agent then works through a few MCP tools (registered here):

- `weekly_packet`   the stored packet (numbers only; `refresh` rebuilds it);
- `report_publish`  saves the narrative and decisions, and opens the meeting:
                    a Czech message to the owner in #weekly with the link and
                    3-5 questions; the agent's task waits for the answer;
- `meeting_status`  the conversation so far (the owner's replies), the questions,
                    how long the owner has left;
- `meeting_reply`   a follow-up in the thread; the task waits again;
- `meeting_close`   the meeting notes into the report, a short summary in the
                    thread; the goals and tasks made during the meeting are
                    recorded on the report; the task is done;
- `goal_list`, `goal_upsert`, `goal_link` (pos.goals).

The owner's messages in #weekly bring the waiting task back to the agent's
queue (`on_owner_message`, called from pos.chat.send) and wake its worker, so
each meeting turn is a short run. If the owner does not answer within 24 hours
(`meeting_timeouts`, every 30 minutes) the meeting closes with the report only;
if the owner answered but went quiet, the agent is asked once to close with
what it has, and the core closes it after that.

The report itself (packet, narrative, decisions, questions, meeting notes,
goals and tasks created) is stored in `weekly_reports`, created on first use
(no numbered migration, so it cannot collide with one added elsewhere), and
shown on the Reports page of the web app (pos.api_reports).
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, goals as goals_mod, tasks, versioning, weekly_packet
from .core import Ctx, Forbidden, NotFound, now_iso

log = logging.getLogger(__name__)

ROLE = "chief_of_staff"
NAME = "Chief of Staff"  # "Asistent vedení" in Czech (its name until the 2026-09 reorganisation)
ALIASES = (NAME, "Asistent vedení")
CHANNEL = "weekly"
TOPIC = "weekly"
ANSWER_HOURS = 24
NUDGE_HOURS = 6
MAX_QUESTIONS = 5
DEFAULT_SCHEDULE = "weekly fri 14:00"

_SCHEMA = """CREATE TABLE IF NOT EXISTS weekly_reports (
    id                 INTEGER PRIMARY KEY,
    week               TEXT NOT NULL UNIQUE,
    period_start       TEXT NOT NULL,
    period_end         TEXT NOT NULL,
    packet             TEXT NOT NULL DEFAULT '{}',
    narrative          TEXT NOT NULL DEFAULT '',
    headline           TEXT NOT NULL DEFAULT '',
    decisions          TEXT NOT NULL DEFAULT '[]',
    questions          TEXT NOT NULL DEFAULT '[]',
    meeting_notes      TEXT NOT NULL DEFAULT '',
    meeting_summary    TEXT NOT NULL DEFAULT '',
    created_goals      TEXT NOT NULL DEFAULT '[]',
    created_tasks      TEXT NOT NULL DEFAULT '[]',
    status             TEXT NOT NULL DEFAULT 'draft',
    task_id            INTEGER REFERENCES tasks(id),
    author_id          INTEGER REFERENCES actors(id),
    channel_id         INTEGER,
    thread_message_id  INTEGER,
    agent_seen_id      INTEGER NOT NULL DEFAULT 0,
    asked_at           TEXT,
    answered_at        TEXT,
    last_owner_at      TEXT,
    deadline_at        TEXT,
    nudged_at          TEXT,
    published_at       TEXT,
    closed_at          TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
)"""

STATUSES = ("draft", "meeting", "published", "closed", "no_reply")
OPEN_MEETING = "meeting"


class Invalid(ValueError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    goals_mod.ensure_schema(conn)


def _in(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ who and where

def agent_id(conn: sqlite3.Connection) -> int | None:
    """The Chief of Staff: the member with role chief_of_staff, else by name."""
    row = conn.execute("SELECT id FROM actors WHERE role = ? AND archived_at IS NULL AND kind != 'human' "
                       "ORDER BY id LIMIT 1", (ROLE,)).fetchone()
    if row:
        return row["id"]
    for name in ALIASES:
        r = actors.find_by_name(conn, name)
        if r is not None and not r["archived_at"]:
            return r["id"]
    return None


def channel_id(conn: sqlite3.Connection) -> int:
    """#weekly: the owner and the Chief of Staff (private; the owner invites others)."""
    from . import chat

    owner = actors.owner_id(conn)
    row = conn.execute("SELECT id FROM channels WHERE kind = 'group' AND name = ? COLLATE NOCASE "
                       "AND archived_at IS NULL", (CHANNEL,)).fetchone()
    if row is None:
        ch = versioning.insert(conn, Ctx(owner, via="system"), "channel", {
            "kind": "group", "name": CHANNEL, "visibility": "private", "created_by": owner, "created_at": now_iso(),
            "topic": "Týdenní report a krátký meeting s Asistentem vedení: co se povedlo, co ne, priority a cíle."})
        cid = ch["id"]
        chat._add_member(conn, cid, owner, "owner")
    else:
        cid = row["id"]
    cos = agent_id(conn)
    if cos:
        chat._add_member(conn, cid, cos)
    # The CEO takes part in the Friday board meeting (docs/REORG.md).
    ceo = conn.execute("SELECT id FROM actors WHERE role = 'ceo' AND archived_at IS NULL AND kind != 'human' "
                       "ORDER BY id LIMIT 1").fetchone()
    if ceo:
        chat._add_member(conn, cid, ceo["id"])
    return cid


def report_url(week: str) -> str:
    base = (os.environ.get("POS_PUBLIC_URL") or "").rstrip("/")
    return f"{base}/reports/{week}"


def _may_run_meeting(conn: sqlite3.Connection, ctx: Ctx) -> None:
    me = actors.get(conn, ctx.actor_id)
    if me["kind"] == "human" or ctx.actor_id == agent_id(conn):
        return
    raise Forbidden("only the Chief of Staff or a person runs the weekly meeting")


# ------------------------------------------------------------------ storage

def _row(conn: sqlite3.Connection, week: str) -> sqlite3.Row | None:
    ensure_schema(conn)
    return conn.execute("SELECT * FROM weekly_reports WHERE week = ?", (week.strip().upper(),)).fetchone()


def _require(conn: sqlite3.Connection, week: str) -> sqlite3.Row:
    row = _row(conn, week)
    if row is None:
        raise NotFound(f"no weekly report for {week}; call weekly_packet first")
    return row


def _set(conn: sqlite3.Connection, week: str, **fields) -> None:
    fields["updated_at"] = now_iso()
    for k, v in list(fields.items()):
        if isinstance(v, (list, dict)):
            fields[k] = json.dumps(v, ensure_ascii=False)
    conn.execute(f"UPDATE weekly_reports SET {', '.join(f'{k} = ?' for k in fields)} WHERE week = ?",
                 [*fields.values(), week])


def default_week(conn: sqlite3.Connection) -> str:
    """The report being worked on (a draft or an open meeting), else the current week."""
    ensure_schema(conn)
    row = conn.execute("SELECT week FROM weekly_reports WHERE status IN ('draft', 'meeting') "
                       "ORDER BY week DESC LIMIT 1").fetchone()
    return row["week"] if row else weekly_packet.current_week()


def store_packet(conn: sqlite3.Connection, packet: dict) -> None:
    ensure_schema(conn)
    week, now = packet["week"], now_iso()
    conn.execute(
        """INSERT INTO weekly_reports (week, period_start, period_end, packet, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT (week) DO UPDATE SET packet = excluded.packet, period_start = excluded.period_start,
             period_end = excluded.period_end, updated_at = excluded.updated_at""",
        (week, packet["period"]["start"], packet["period"]["end"], json.dumps(packet, ensure_ascii=False), now, now))


def packet_for(conn: sqlite3.Connection, week: str | None = None, *, refresh: bool = False,
               outside: bool = True) -> dict:
    """The stored packet for the week; built (and stored) when missing or on refresh.
    A published report keeps the numbers it was written from."""
    week = (week or default_week(conn)).strip().upper()
    row = _row(conn, week)
    if row is not None and row["packet"] not in (None, "", "{}"):
        if not refresh or row["status"] not in ("draft",):
            return json.loads(row["packet"])
    packet = weekly_packet.build(conn, week, outside=outside)
    store_packet(conn, packet)
    return packet


def _task_briefs(conn: sqlite3.Connection, refs: list) -> list[dict]:
    out = []
    for r in refs:
        s = str(r).upper().removeprefix("T-")
        if not s.isdigit():
            continue
        t = conn.execute("SELECT id, title, status, assignee_name, deadline FROM tasks WHERE id = ?",
                         (int(s),)).fetchone()
        if t:
            out.append({"ref": tasks.display_id(t["id"]), "title": t["title"], "status": t["status"],
                        "assignee": t["assignee_name"], "deadline": t["deadline"]})
    return out


def to_dict(conn: sqlite3.Connection, row: sqlite3.Row, *, full: bool = True) -> dict:
    d = {k: row[k] for k in row.keys() if k not in ("packet",)}
    for k in ("decisions", "questions", "created_goals", "created_tasks"):
        d[k] = json.loads(row[k] or "[]")
    d["url"] = report_url(row["week"])
    packet = json.loads(row["packet"] or "{}")
    if full:
        d["packet"] = packet
        d["tasks_created"] = _task_briefs(conn, d["created_tasks"])
        gl = []
        for gid in d["created_goals"]:
            try:
                gl.append(goals_mod.brief(goals_mod.get(conn, int(gid))))
            except (NotFound, ValueError):
                continue
        d["goals_changed"] = gl
    else:
        d["kpis"] = packet.get("kpis", {})
        d["summary"] = weekly_packet.summary_line(packet) if packet.get("kpis") else ""
    return d


def list_reports(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM weekly_reports ORDER BY week DESC LIMIT ?", (limit,)).fetchall()
    return [to_dict(conn, r, full=False) for r in rows]


def get_report(conn: sqlite3.Connection, week: str, viewer: int | None = None) -> dict:
    row = _require(conn, week)
    out = to_dict(conn, row)
    out["transcript"] = transcript(conn, row, viewer) if row["thread_message_id"] else []
    return out


# ------------------------------------------------------------------ the conversation

def transcript(conn: sqlite3.Connection, row: sqlite3.Row, viewer: int | None = None,
               after: int = 0) -> list[dict]:
    """Messages in #weekly since the meeting opened (the thread and the channel)."""
    if not row["channel_id"] or not row["thread_message_id"]:
        return []
    from . import chat

    names = {r["id"]: r for r in conn.execute("SELECT id, name, kind, is_owner FROM actors")}
    rows = conn.execute(
        """SELECT * FROM chat_messages WHERE channel_id = ? AND id >= ? AND id > ? AND archived_at IS NULL
           ORDER BY id LIMIT 200""", (row["channel_id"], row["thread_message_id"], after)).fetchall()
    for_agent = viewer is not None and actors.get(conn, viewer)["kind"] != "human"
    out = []
    for m in rows:
        a = names.get(m["author_id"])
        body = m["body"]
        if for_agent and m["author_id"] != viewer:
            body, _ = chat._wrap_for_agent({**dict(m), "from_name": a["name"] if a else "?"}, body)
        out.append({"id": m["id"], "author": a["name"] if a else "?", "kind": a["kind"] if a else "agent",
                    "owner": bool(a and a["is_owner"]), "at": m["created_at"], "body": body,
                    "reply_to": m["reply_to"]})
    return out


def _post(conn: sqlite3.Connection, ctx: Ctx, cid: int, body: str, reply_to: int | None = None) -> int:
    from . import chat

    return chat.send(conn, ctx, cid, body, reply_to=reply_to, system=True)["id"]


def _owner_messages_after(conn: sqlite3.Connection, row: sqlite3.Row, after_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT m.* FROM chat_messages m JOIN actors a ON a.id = m.author_id
           WHERE m.channel_id = ? AND m.id > ? AND m.id >= ? AND a.kind = 'human' AND m.archived_at IS NULL
           ORDER BY m.id""", (row["channel_id"], after_id, row["thread_message_id"] or 0)).fetchall()


def _park(conn: sqlite3.Connection, ctx: Ctx, row: sqlite3.Row, note: str) -> None:
    """The meeting task waits for the owner (its worker goes idle; no tokens)."""
    if not row["task_id"]:
        return
    t = conn.execute("SELECT status FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
    if t and t["status"] not in ("done", "waiting"):
        versioning.update(conn, ctx, tasks.ENTITY, row["task_id"],
                          {"status": "waiting", "progress_note": note[:500]}, action="wait")


def _resume(conn: sqlite3.Connection, ctx: Ctx, row: sqlite3.Row, note: str) -> bool:
    """Back to the agent's queue, and wake its worker."""
    if not row["task_id"]:
        return False
    t = conn.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
    if not t or t["status"] != "waiting":
        return False
    # retry_after: a meeting turn is never held back by the hand-back back-off (pos.api_worker).
    versioning.update(conn, ctx, tasks.ENTITY, row["task_id"],
                      {"status": "next", "progress_note": note[:500], "retry_after": None}, action="resume")
    if t["assignee_id"]:
        from . import wake

        wake.wake(t["assignee_id"])
    return True


# ------------------------------------------------------------------ the flow

def _questions(qs: list[str] | None) -> list[str]:
    out = [" ".join(str(q).split()) for q in (qs or []) if str(q).strip()]
    if len(out) > MAX_QUESTIONS:
        raise Invalid(f"at most {MAX_QUESTIONS} questions: keep the meeting to 15 minutes")
    return out


def opening_message(owner_name: str, week: str, url: str, questions: list[str], headline: str = "") -> str:
    from .asks import vocative

    lines = [f"{vocative(owner_name)}, týdenní report je hotový: [{week}]({url}). Máš 15 minut na krátký meeting?"]
    if headline.strip():
        lines.append(f"V kostce: {' '.join(headline.split())}")
    if questions:
        lines += ["", "Odpověz prosím tady ve vlákně, klidně jen na část:"]
        lines += [f"{i}. {q}" for i, q in enumerate(questions, 1)]
    lines += ["", f"@{owner_name}"]
    return "\n".join(lines)


def publish(conn: sqlite3.Connection, ctx: Ctx, *, week: str | None = None, narrative: str,
            decisions: list[str] | None = None, questions: list[str] | None = None, headline: str = "",
            task_id: int | None = None) -> dict:
    """Save the narrative and open the meeting (or only publish, without questions)."""
    _may_run_meeting(conn, ctx)
    week = (week or default_week(conn)).strip().upper()
    if not (narrative or "").strip():
        raise Invalid("the report needs its narrative (Markdown): what happened, wins, problems, where we head")
    qs = _questions(questions)
    packet_for(conn, week)  # the numbers the narrative was written from are stored with it
    row = _require(conn, week)
    if row["status"] in ("closed", "no_reply"):
        raise Invalid(f"the report for {week} is already closed ({row['status']})")
    if task_id is None and row["task_id"]:
        task_id = row["task_id"]
    fields = dict(narrative=narrative.strip(), questions=qs, author_id=ctx.actor_id,
                  published_at=row["published_at"] or now_iso(), task_id=task_id)
    if decisions is not None:
        fields["decisions"] = [str(d).strip() for d in decisions if str(d).strip()]
    if (headline or "").strip():
        fields["headline"] = " ".join(headline.split())[:300]
    if row["status"] == OPEN_MEETING:
        # Already asked: an update of the text only (what was given), no second ping.
        _set(conn, week, **{k: v for k, v in fields.items() if k != "questions"})
        audit.log(conn, ctx, "weekly_report_update", "weekly_report", row["id"], week=week)
        return {"week": week, "url": report_url(week), "status": OPEN_MEETING, "updated": True,
                "note": "The meeting is already open; the report text was updated. Finish this run."}
    _set(conn, week, **fields, status="published" if not qs else OPEN_MEETING)
    audit.log(conn, ctx, "weekly_report_publish", "weekly_report", row["id"], week=week, questions=len(qs))
    if not qs:
        return {"week": week, "url": report_url(week), "status": "published",
                "note": "Published without a meeting (no questions)."}
    owner = actors.get(conn, actors.owner_id(conn))
    cid = channel_id(conn)
    mid = _post(conn, ctx, cid, opening_message(owner["name"], week, report_url(week), qs, fields.get("headline", "")))
    _set(conn, week, channel_id=cid, thread_message_id=mid, agent_seen_id=mid, asked_at=now_iso(),
         deadline_at=_in(ANSWER_HOURS))
    row = _require(conn, week)
    _park(conn, ctx, row, f"Čekám na odpověď v #{CHANNEL} (meeting {week}).")
    conn.commit()
    return {"week": week, "url": report_url(week), "status": OPEN_MEETING, "channel": f"#{CHANNEL}",
            "message_id": mid,
            "note": (f"The meeting is open in #{CHANNEL}. Your task now waits for the owner's answer and "
                     "comes back to your queue when they reply (or the meeting closes by itself after "
                     f"{ANSWER_HOURS} h without an answer). Finish this run now with one line.")}


def status(conn: sqlite3.Connection, ctx: Ctx, week: str | None = None) -> dict:
    """Where the meeting is, and the conversation; marks what the agent has seen."""
    week = (week or default_week(conn)).strip().upper()
    row = _require(conn, week)
    conv = transcript(conn, row, ctx.actor_id)
    unanswered = [m for m in conv if m["kind"] == "human" and m["id"] > (row["agent_seen_id"] or 0)]
    if conv and ctx.actor_id == agent_id(conn):
        _set(conn, week, agent_seen_id=max(m["id"] for m in conv))
    left = None
    if row["deadline_at"]:
        left = round((datetime.fromisoformat(row["deadline_at"]) - datetime.now(timezone.utc)).total_seconds() / 3600, 1)
    nxt = {
        "draft": "Read weekly_packet, write the narrative, then report_publish with 3-5 questions.",
        "published": "Published without a meeting; nothing to do.",
        OPEN_MEETING: ("The owner answered: react with meeting_reply (a short follow-up or the next question), "
                       "or, when you have what you need, create goals and tasks and call meeting_close."
                       if unanswered else "No new answer yet: finish the run; you will be woken when they reply."),
        "closed": "Closed.", "no_reply": "Closed without an answer.",
    }[row["status"]]
    return {"week": week, "status": row["status"], "url": report_url(week),
            "questions": json.loads(row["questions"] or "[]"), "decisions": json.loads(row["decisions"] or "[]"),
            "asked_at": row["asked_at"], "hours_left": left, "new_from_owner": [m["id"] for m in unanswered],
            "conversation": [{k: m[k] for k in ("id", "author", "owner", "at", "body")} for m in conv],
            "goals": [goals_mod.brief(g) for g in goals_mod.list_goals(conn, "active")],
            "next": nxt}


def reply(conn: sqlite3.Connection, ctx: Ctx, body: str, week: str | None = None) -> dict:
    """A follow-up in the meeting thread; the task waits again for the owner."""
    _may_run_meeting(conn, ctx)
    week = (week or default_week(conn)).strip().upper()
    row = _require(conn, week)
    if row["status"] != OPEN_MEETING:
        raise Invalid(f"the meeting for {week} is not open ({row['status']})")
    if not (body or "").strip():
        raise Invalid("an empty reply")
    new = _owner_messages_after(conn, row, row["agent_seen_id"] or 0)
    if new and ctx.actor_id == agent_id(conn):
        # The owner wrote something the agent has not seen: show it first, post nothing yet.
        _set(conn, week, agent_seen_id=max(m["id"] for m in new))
        return {"posted": False, "parked": False,
                "new_from_owner": [{"id": m["id"], "body": m["body"], "at": m["created_at"]} for m in new],
                "note": "The owner wrote meanwhile (above). Take it into account and call meeting_reply again."}
    mid = _post(conn, ctx, row["channel_id"], body.strip(), reply_to=row["thread_message_id"])
    _set(conn, week, agent_seen_id=mid, deadline_at=_in(ANSWER_HOURS))
    _park(conn, ctx, _require(conn, week), f"Čekám na odpověď v #{CHANNEL} (meeting {week}).")
    conn.commit()
    return {"posted": True, "parked": True, "message_id": mid,
            "note": "Posted. Your task waits for the owner's answer; finish this run now."}


def _made_during(conn: sqlite3.Connection, row: sqlite3.Row, author: int | None) -> tuple[list[int], list[str]]:
    since = row["asked_at"] or row["published_at"] or row["created_at"]
    goals_mod.ensure_schema(conn)
    gids = [r["id"] for r in conn.execute(
        "SELECT id FROM goals WHERE (created_at >= ? OR updated_at >= ?) ORDER BY id", (since, since))]
    trefs = []
    if author:
        trefs = [tasks.display_id(r["id"]) for r in conn.execute(
            """SELECT id FROM tasks WHERE created_by = ? AND created_at >= ? AND archived_at IS NULL
               AND id IS NOT ? AND COALESCE(topic, '') != 'chat' ORDER BY id""", (author, since, row["task_id"]))]
    return gids, trefs


def _finish_task(conn: sqlite3.Connection, row: sqlite3.Row, note: str) -> None:
    if not row["task_id"]:
        return
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
    if not t or t["status"] == "done":
        return
    ctx = Ctx(t["assignee_id"] or actors.owner_id(conn), via="system")
    try:
        tasks.complete(conn, ctx, t["id"], note[:2000])
    except Exception:  # noqa: BLE001 - the report is closed either way; the task is a record
        log.exception("could not complete the weekly task %s", t["id"])


def close(conn: sqlite3.Connection, ctx: Ctx, *, notes: str, summary: str, week: str | None = None,
          tasks_created: list[str] | None = None, goals_changed: list[int] | None = None) -> dict:
    """Write the meeting notes into the report, post the summary, finish the task."""
    _may_run_meeting(conn, ctx)
    week = (week or default_week(conn)).strip().upper()
    row = _require(conn, week)
    if row["status"] in ("closed", "no_reply"):
        raise Invalid(f"the meeting for {week} is already closed")
    if not (notes or "").strip():
        raise Invalid("meeting_close needs the notes (Markdown): what was said, decided, and what happens next")
    gids, trefs = _made_during(conn, row, ctx.actor_id if ctx.actor_id == agent_id(conn) else None)
    for g in goals_changed or []:
        if int(g) not in gids:
            gids.append(int(g))
    for t in tasks_created or []:
        ref = tasks.display_id(tasks.parse_id(t))
        if ref not in trefs:
            trefs.append(ref)
    _set(conn, week, meeting_notes=notes.strip(), meeting_summary=(summary or "").strip(), created_goals=gids,
         created_tasks=trefs, status="closed", closed_at=now_iso())
    audit.log(conn, ctx, "weekly_meeting_close", "weekly_report", row["id"], week=week, goals=gids, tasks=trefs)
    if row["channel_id"]:
        text = (summary or "").strip() or "Díky, meeting je uzavřený."
        extra = []
        if trefs:
            extra.append("Úkoly na příští týden: " + ", ".join(trefs))
        if gids:
            extra.append(f"Cíle upravené nebo nové: {len(gids)}")
        extra.append(f"Zápis je v reportu: [{week}]({report_url(week)})")
        _post(conn, ctx, row["channel_id"], text + "\n\n" + "\n".join(f"- {e}" for e in extra),
              reply_to=row["thread_message_id"])
    _finish_task(conn, _require(conn, week), f"Weekly report {week} published; meeting closed. "
                                             f"{len(trefs)} tasks, {len(gids)} goals.")
    conn.commit()
    return {"week": week, "status": "closed", "url": report_url(week), "tasks": trefs, "goals": gids,
            "note": "Closed. The task is done; finish the run."}


def on_owner_message(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, message_id: int) -> None:
    """A person wrote in #weekly: an open meeting's task comes back to the agent
    (pos.chat.send calls this before it commits)."""
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM weekly_reports WHERE channel_id = ? AND status = ? ORDER BY week DESC LIMIT 1",
                       (ch["id"], OPEN_MEETING)).fetchone()
    if row is None:
        return
    now = now_iso()
    _set(conn, row["week"], answered_at=row["answered_at"] or now, last_owner_at=now, deadline_at=_in(ANSWER_HOURS))
    who = actors.get(conn, ctx.actor_id)["name"]
    if row["task_id"]:
        from . import comments

        body = conn.execute("SELECT body FROM chat_messages WHERE id = ?", (message_id,)).fetchone()["body"]
        comments.log(conn, ctx, row["task_id"], f"{who} v #{CHANNEL} (zpráva {message_id}): {body[:1500]}", "system")
    _resume(conn, ctx, row, f"{who} odpověděl v #{CHANNEL}: pokračuj v meetingu (meeting_status).")
    audit.log(conn, ctx, "weekly_meeting_answer", "weekly_report", row["id"], message=message_id)


# ------------------------------------------------------------------ the routine (pos.scheduler)

def task_notes(week: str, packet: dict) -> str:
    p = packet.get("period", {})
    return "\n".join([
        f"### Proč",
        f"Týdenní report firmy za **{week}** ({p.get('start')} – {p.get('end')}) a krátký meeting s majitelem, "
        "na kterém se nastaví cíle a priority na další týden.",
        "",
        "### Odkud",
        "Týdenní rutina PersonalOS (job `weekly_report`). Čísla jsou už spočítaná v balíčku týdne "
        f"(`weekly_packet`, uložený {packet.get('generated_at', '')[:16]}).",
        "",
        "### Postup",
        "1. `weekly_packet` → přečti čísla (nic dalšího nenačítej, pokud to není nutné).",
        "2. Napiš narativ podle svých instrukcí a seznam rozhodnutí, která potřebuješ.",
        "3. `report_publish` s 3–5 otázkami. Pak run ukonči.",
        "4. Po každé odpovědi: `meeting_status` → `meeting_reply`, nebo cíle (`goal_upsert`, `goal_link`), "
        "úkoly (`create_task`) a `meeting_close`.",
        "",
        "### Hotovo znamená",
        "Report je publikovaný, meeting uzavřený (nebo sám skončil bez odpovědi), cíle a úkoly na další týden "
        "jsou založené a zápis je v reportu.",
    ])


def weekly_job(conn: sqlite3.Connection, week: str | None = None) -> dict:
    """Build the week's packet and give the Chief of Staff its task (no tokens here). `week`: a
    past week to catch up (catch_up); default the current one."""
    ensure_schema(conn)
    if week is None:
        catch_up(conn, include_current=False)  # a recent past week nobody wrote gets its task first
        publish_overdue(conn)  # an older draft is published from its numbers
    cos = agent_id(conn)
    week = (week or weekly_packet.current_week()).strip().upper()
    row = _row(conn, week)
    if row is not None and row["status"] in (OPEN_MEETING, "closed", "no_reply", "published"):
        return {"skipped": f"the report for {week} is already {row['status']}"}
    if cos is None:
        packet = packet_for(conn, week, refresh=True)
        conn.commit()
        return {"skipped": "no Chief of Staff agent (agents/asistent-vedeni); packet stored", "week": week,
                "summary": weekly_packet.summary_line(packet)}
    if row is not None and row["task_id"]:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
        if t and t["status"] != "done":
            return {"skipped": f"{tasks.display_id(row['task_id'])} for {week} is still open"}
    packet = packet_for(conn, week, refresh=True)
    owner = Ctx(actors.owner_id(conn), via="scheduler")
    t = tasks.create(conn, owner, {
        "title": f"Týdenní report a meeting · {week}", "notes": task_notes(week, packet),
        "definition_of_done": "Report je publikovaný, meeting uzavřený nebo vypršel, cíle a úkoly na další týden "
                              "jsou založené a zápis je v reportu.",
        "assignee": {"type": "agent", "id": cos}, "reviewer": cos, "status": "next", "priority": 2,
        "topic": TOPIC, "source": "weekly_report"})
    _set(conn, week, task_id=t["id"])
    channel_id(conn)
    audit.log(conn, owner, "weekly_report_task", "task", t["id"], week=week)
    conn.commit()
    return {"task": t["ref"], "week": week, "summary": weekly_packet.summary_line(packet)}


def meeting_timeouts(conn: sqlite3.Connection) -> dict:
    """No answer within 24 h: close with the report only. Answered, then silent:
    ask the agent once to close; the core closes it after that."""
    ensure_schema(conn)
    now = now_iso()
    closed, nudged = [], []
    for row in conn.execute("SELECT * FROM weekly_reports WHERE status = ? AND deadline_at IS NOT NULL "
                            "AND deadline_at < ?", (OPEN_MEETING, now)).fetchall():
        cos = agent_id(conn) or actors.owner_id(conn)
        ctx = Ctx(cos, via="scheduler")
        week = row["week"]
        if row["answered_at"] and not row["nudged_at"]:
            _set(conn, week, nudged_at=now, deadline_at=_in(NUDGE_HOURS))
            if not _resume(conn, ctx, row, f"Majitel {ANSWER_HOURS} h neodpověděl: uzavři meeting s tím, co máš "
                                           "(meeting_close)."):
                continue
            nudged.append(week)
            continue
        owner = actors.get(conn, actors.owner_id(conn))
        from .asks import vocative

        if row["answered_at"]:
            notes = "_Meeting skončil po 24 h ticha; zápis obsahuje jen to, co zaznělo ve vlákně #weekly._"
            text = f"{vocative(owner['name'])}, meeting uzavírám — pokračovat můžeme příští týden."
            state = "closed"
        else:
            notes = "_Majitel do 24 h neodpověděl; report je publikovaný bez meetingu. Cíle zůstávají beze změny._"
            text = (f"{vocative(owner['name'])}, na meeting nedošlo — nevadí. Report zůstává tady: "
                    f"[{week}]({report_url(week)}). Cíle nechávám, jak jsou.")
            state = "no_reply"
        gids, trefs = _made_during(conn, row, agent_id(conn))
        _set(conn, week, status=state, meeting_notes=row["meeting_notes"] or notes, closed_at=now,
             created_goals=gids, created_tasks=trefs)
        try:
            if row["channel_id"]:
                _post(conn, ctx, row["channel_id"], text, reply_to=row["thread_message_id"])
        except Exception:  # noqa: BLE001 - closing must not fail on the chat
            log.exception("could not post the close of %s", week)
        _finish_task(conn, _require(conn, week), f"Meeting {week} closed by the core ({state}).")
        audit.log(conn, ctx, "weekly_meeting_timeout", "weekly_report", row["id"], week=week, state=state)
        closed.append(week)
    conn.commit()
    out = {}
    if closed:
        out["closed"] = closed
    if nudged:
        out["nudged"] = nudged
    return out


# ------------------------------------------------------------------ catching up a missed week

CATCH_UP_DAYS = 7  # a past week that ended this recently still gets a written report, not only numbers


def _slot(week: str) -> datetime:
    """When the weekly job fires in this week (the Friday slot, POS_WEEKLY_REPORT_SCHEDULE)."""
    from .scheduler import next_run

    start, _ = weekly_packet.bounds(week)
    return next_run(schedule(), start - timedelta(seconds=1))


def catch_up(conn: sqlite3.Connection, now: datetime | None = None, *, include_current: bool = True) -> dict:
    """The job missed a week (created after its Friday slot, the api was down at the time, or the
    week was left a draft with no author and no task, W39): an unpublished week whose slot has
    passed and that ended at most CATCH_UP_DAYS ago gets the Chief of Staff's task now, with a fresh
    packet. Such a week is a draft nobody writes, or a week with no report at all whose slot passed
    while the (enabled) job existed. Run at startup and before the hourly auto-publish; older drafts
    are published from their numbers (publish_overdue)."""
    ensure_schema(conn)
    if agent_id(conn) is None:
        return {}
    now = now or datetime.now(timezone.utc)
    current = weekly_packet.current_week()
    weeks = {r["week"] for r in conn.execute("SELECT week FROM weekly_reports WHERE status = 'draft'")}
    job = conn.execute("SELECT enabled, created_at FROM jobs WHERE action = 'weekly_report'").fetchone()
    if job is not None and job["enabled"]:
        week = current
        for _ in range(2):
            week = weekly_packet.previous_week(week)
            try:
                if _row(conn, week) is None and _slot(week).isoformat(timespec="seconds") > job["created_at"]:
                    weeks.add(week)
            except ValueError:
                pass
        if (include_current and _row(conn, current) is None
                and _slot(current).isoformat(timespec="seconds") > job["created_at"]):
            weeks.add(current)
    if not include_current:
        weeks.discard(current)
    started = []
    for week in sorted(weeks):
        try:
            _, end = weekly_packet.bounds(week)
            slot = _slot(week)
        except ValueError:
            continue
        if now < slot or now - end > timedelta(days=CATCH_UP_DAYS):
            continue
        row = _row(conn, week)
        if row is not None and (row["status"] != "draft" or (row["narrative"] or "").strip() or row["task_id"]):
            continue  # written, or already given out (publish_overdue publishes it if nobody does)
        out = weekly_job(conn, week)
        if out.get("task"):
            started.append(f"{week}: {out['task']}")
    return {"started": started} if started else {}


# ------------------------------------------------------------------ the report always gets published

# The Chief of Staff has this long after the Friday job to publish; then the core publishes the report
# from the packet itself (a code-written narrative), so a week never stays an empty draft. The W39
# draft stayed empty because the job was created on Saturday (after its Friday slot) and the draft
# came from a packet read with nobody asked to write it; publish_overdue closes that gap too.
AUTO_PUBLISH_HOURS = 20


def _fmt_delta(k: dict) -> str:
    d = k.get("delta")
    return "" if d in (None, 0) else (f" ({'+' if d > 0 else ''}{d:g})" if isinstance(d, (int, float)) else "")


def auto_narrative(packet: dict) -> tuple[str, str]:
    """(headline, Markdown narrative) from the packet's numbers, in Czech. No tokens."""
    k = packet.get("kpis", {})
    t = packet.get("tasks", {})
    a = packet.get("agents", {})
    biz = packet.get("business") or {}
    split = biz.get("cost_split") or {}
    owner = biz.get("owner_time") or {}
    inv = biz.get("invoices") or {}
    val = lambda key: (k.get(key) or {}).get("value")  # noqa: E731
    lines = ["_Report napsal automaticky PersonalOS z čísel týdne (Chief of Staff ho včas nezveřejnil)._", "",
             "## Co se stalo",
             f"- Hotovo **{val('tasks_done')}** úkolů{_fmt_delta(k.get('tasks_done', {}))}, nových {val('tasks_new')}, "
             f"čeká {val('waiting')}, po termínu {val('overdue')}.",
             f"- Agenti: {a.get('runs', 0)} běhů, úspěšnost "
             f"{'—' if a.get('success_rate') is None else str(round(a['success_rate'] * 100)) + ' %'}, "
             f"náklady ${a.get('cost_usd', 0):.2f}."]
    if split:
        per = split.get("usd_per_business_outcome")
        lines.append(f"- Byznys vs. platforma: ${split.get('business_usd', 0):.2f} / ${split.get('platform_usd', 0):.2f}; "
                     f"{split.get('business_outcomes', 0)} byznys výsledků"
                     + (f", ${per:.2f} za výsledek." if per is not None else "."))
    if owner.get("line"):
        lines.append(f"- {owner['line']}.")
    if inv.get("available"):
        tot = inv.get("totals") or {}
        money = lambda d: ", ".join(f"{v:,.0f} {c}".replace(",", " ") for c, v in d.items()) or "—"  # noqa: E731
        lines.append(f"- Faktury: vydané {inv.get('sent', 0)} ({money(tot.get('sent', {}))}), přijaté "
                     f"{inv.get('received', 0)} ({money(tot.get('received', {}))}) — odhad z knowlage.")
    ct = biz.get("customer_threads") or {}
    pl = biz.get("pipeline") or {}
    if ct or pl:
        lines.append(f"- Otevřená zákaznická vlákna {ct.get('open', 0)}; pipeline (Growth) otevřeno {pl.get('open', 0)}, "
                     f"nových {pl.get('new', 0)}, uzavřeno {pl.get('done', 0)}.")
    hl = t.get("highlights") or []
    if hl:
        lines += ["", "## Co se povedlo"] + [f"- {h['ref']} {h['title']} ({h.get('assignee') or '—'})" for h in hl[:6]]
    probs = [f"- {o['ref']} {o['title']} (termín {o['deadline']})" for o in (t.get("overdue_list") or [])[:5]]
    probs += [f"- {w['ref']} {w['title']} čeká od {w['since']}" for w in (t.get("waiting_list") or [])[:3]]
    if probs:
        lines += ["", "## Problémy a rizika"] + probs
    goals = packet.get("goals") or []
    if goals:
        lines += ["", "## Kam míříme"] + [f"- {g['title']}: {g.get('progress') or 0} %" for g in goals[:6]]
    headline = f"Týden {packet.get('week')}: " + summary_or_blank(packet)
    return headline[:300], "\n".join(lines)


def summary_or_blank(packet: dict) -> str:
    try:
        return weekly_packet.summary_line(packet)
    except (KeyError, TypeError):
        return ""


def publish_overdue(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Drafts nobody published: a past week's draft, or the current one AUTO_PUBLISH_HOURS after the
    Chief of Staff got its task. Published by the core from the packet; the Chief of Staff can still
    rewrite the text or open the meeting (report_publish) while its task is open."""
    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    caught = catch_up(conn, now)  # a recent missed week is the Chief of Staff's to write first
    done = []
    for row in conn.execute("SELECT * FROM weekly_reports WHERE status = 'draft' ORDER BY week").fetchall():
        try:
            _, end = weekly_packet.bounds(row["week"])
        except ValueError:
            continue
        created = None
        if row["task_id"]:
            t = conn.execute("SELECT created_at FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
            created = datetime.fromisoformat(t["created_at"]) if t else None
        due = (created is not None and now - created >= timedelta(hours=AUTO_PUBLISH_HOURS)) or \
              (created is None and now >= end)
        if not due or (row["narrative"] or "").strip():
            continue
        packet = packet_for(conn, row["week"], refresh=now < end + timedelta(days=2))
        headline, narrative = auto_narrative(packet)
        _set(conn, row["week"], narrative=narrative, headline=headline, status="published",
             published_at=now.isoformat(timespec="seconds"))
        ctx = Ctx(agent_id(conn) or actors.owner_id(conn), via="scheduler")
        audit.log(conn, ctx, "weekly_report_auto_publish", "weekly_report", row["id"], week=row["week"])
        try:
            _post(conn, ctx, channel_id(conn), f"Týdenní report {row['week']} je zveřejněný (automaticky z čísel): "
                                               f"[{row['week']}]({report_url(row['week'])}). {headline}")
        except Exception:  # noqa: BLE001 - the report is published either way
            log.exception("could not announce the report %s", row["week"])
        done.append(row["week"])
    conn.commit()
    out = {"published": done} if done else {}
    return {**out, **caught}


def schedule() -> str:
    return os.environ.get("POS_WEEKLY_REPORT_SCHEDULE") or DEFAULT_SCHEDULE


# ------------------------------------------------------------------ MCP tools

# What an agent needs for each tool (merged into pos.mcp_server.TOOL_PERMISSIONS at registration,
# so the shared table stays untouched). Meeting tools also check that the caller is the Chief of Staff.
TOOL_PERMISSIONS = {
    "weekly_packet": "tasks:read", "report_publish": "tasks:claim", "meeting_status": "tasks:read",
    "meeting_reply": "tasks:claim", "meeting_close": "tasks:claim",
    "goal_list": "tasks:read", "goal_upsert": "tasks:write", "goal_link": "tasks:write",
}


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context

    from . import mcp_server

    for k, v in TOOL_PERMISSIONS.items():
        mcp_server.TOOL_PERMISSIONS.setdefault(k, v)

    def invalid(fn):
        try:
            return fn()
        except (Invalid, goals_mod.Invalid, ValueError) as e:
            if isinstance(e, tasks.Invalid):
                raise
            raise tasks.Invalid(str(e)) from e

    @mcp.tool(description="The week packet: the week's numbers built by code (tasks done/new/waiting/overdue, "
                          "by project and assignee, highlights; agents' runs, success, cost; commits and "
                          "deploys; communication; incidents; goals; last meeting's tasks; deltas). week: "
                          "'2026-W39' (default: the report being worked on). refresh=true rebuilds a draft.")
    def weekly_packet(ctx: Context, week: str | None = None, refresh: bool = False) -> dict:
        with session(ctx, "weekly_packet", week=week, refresh=refresh) as (conn, _):
            return invalid(lambda: packet_for(conn, week, refresh=refresh))

    @mcp.tool(description="Publish the weekly report and open the meeting. narrative: Markdown in Czech "
                          "(## Co se stalo, ## Co se povedlo, ## Problémy a rizika, ## Kam míříme). "
                          "decisions: what the owner must decide (short lines). questions: 3-5 short Czech "
                          "questions for the meeting; they go to the owner in #weekly with the report link. "
                          "headline: one Czech sentence, the week in brief. Your task then waits for the answer.")
    def report_publish(ctx: Context, narrative: str, questions: list[str], decisions: list[str] | None = None,
                       headline: str = "", week: str | None = None, task_id: str | None = None) -> dict:
        with session(ctx, "report_publish", week=week, questions=len(questions or [])) as (conn, c):
            return invalid(lambda: publish(conn, c, week=week, narrative=narrative, decisions=decisions,
                                           questions=questions, headline=headline,
                                           task_id=tasks.parse_id(task_id) if task_id else None))

    @mcp.tool(description="The weekly meeting now: status, the questions, the conversation in #weekly (the "
                          "owner's replies), hours left for an answer, active goals, and what to do next.")
    def meeting_status(ctx: Context, week: str | None = None) -> dict:
        with session(ctx, "meeting_status", week=week) as (conn, c):
            return invalid(lambda: status(conn, c, week))

    @mcp.tool(description="Reply in the weekly meeting thread (Czech, short: a follow-up or the next "
                          "question). Your task then waits for the owner again; finish the run.")
    def meeting_reply(ctx: Context, body: str, week: str | None = None) -> dict:
        with session(ctx, "meeting_reply", week=week) as (conn, c):
            return invalid(lambda: reply(conn, c, body, week))

    @mcp.tool(description="Close the weekly meeting: notes (Markdown: what was said, decided, goals changed, "
                          "tasks created) go into the report; summary (2-4 Czech lines) is posted in #weekly. "
                          "Goals and tasks you made since the meeting opened are recorded automatically; list "
                          "others in tasks_created / goals_changed. Finishes your task.")
    def meeting_close(ctx: Context, notes: str, summary: str, week: str | None = None,
                      tasks_created: list[str] | None = None, goals_changed: list[int] | None = None) -> dict:
        with session(ctx, "meeting_close", week=week) as (conn, c):
            return invalid(lambda: close(conn, c, notes=notes, summary=summary, week=week,
                                         tasks_created=tasks_created, goals_changed=goals_changed))

    @mcp.tool(description="Goals: status active (default), paused, done, dropped or all. Each has title, why, "
                          "target, owner, due, progress 0-100, parent, linked tasks/topics.")
    def goal_list(ctx: Context, status: str = "active") -> list[dict]:
        with session(ctx, "goal_list", status=status) as (conn, _):
            return goals_mod.list_goals(conn, status)

    @mcp.tool(description="Create a goal (no goal_id) or change one. title, why (one sentence), target "
                          "(measurable: a number and a date), owner (member name), due (YYYY-MM-DD), status "
                          "(active, paused, done, dropped), progress 0-100, parent_id, links (['T-12', "
                          "'topic:acme', 'project:web']). Propose goals the owner agreed to, not your own.")
    def goal_upsert(ctx: Context, goal_id: int | None = None, title: str | None = None, why: str | None = None,
                    target: str | None = None, owner: str | None = None, due: str | None = None,
                    status: str | None = None, progress: int | None = None, parent_id: int | None = None,
                    links: list[str] | None = None) -> dict:
        fields = {k: v for k, v in {"title": title, "why": why, "target": target, "owner": owner, "due": due,
                                    "status": status, "progress": progress, "parent_id": parent_id,
                                    "links": links}.items() if v is not None}
        with session(ctx, "goal_upsert", goal_id=goal_id, title=title) as (conn, c):
            if goal_id:
                return invalid(lambda: goals_mod.update(conn, c, goal_id, fields))
            return invalid(lambda: goals_mod.create(conn, c, fields))

    @mcp.tool(description="Link tasks (T-12), topics ('topic:acme') or projects ('project:web') to a goal.")
    def goal_link(ctx: Context, goal_id: int, links: list[str]) -> dict:
        with session(ctx, "goal_link", goal_id=goal_id, links=links) as (conn, c):
            def go():
                for link in links or []:
                    goals_mod.link_to(conn, c, goal_id, link)
                return goals_mod.get(conn, goal_id)
            return invalid(go)
