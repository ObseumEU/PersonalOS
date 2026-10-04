"""The owner's frustration detector: his angry or repeated messages reach the CEO at once.

Prod 2026-09/10: "tvl!!!", "nefunguje HA … oprav to", "TVL to ma nekdo hlidat … Vynadej jim" came
after the same thing had failed before, and nobody looked for the cause. Now every message of the
owner (pos.chat.send) is checked in code (no model):

- **strong markers**: "!!" or more, "???", "tvl" and other swearing, "nefunguje", "dokonči",
  "kolikrát", "pořád ne", "už zase", and a **repeated request** (a message of his from the last 72 h
  with mostly the same words);
- **weak markers**: "zas" / "zase", "pořád", "furt", "proč": they flag only together with another
  marker or an "!" (alone they are often neutral: "pošli to zase Petrovi").

Text is compared without diacritics and case (he often writes without háčky).

A flagged message is stored (`owner_frustration`) and goes to the CEO the same minute as one task a
day ("Frustrace majitele · <date>", priority 1, deadline today): the message, the markers, the
conversation before it and the earlier request it repeats; a second flag the same day is a comment
on that task. The CEO fixes the cause the same day (its instructions). The scorecard and the
weekly platform meeting count them (`stats`), with two more signals computed from the chat:
**double answers** (two or more agent replies within 15 min to one owner message) and **unanswered
asks** (an owner message in a DM or with a mention that no agent answered within 2 h).
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors
from .core import TZ, Ctx, now_iso

log = logging.getLogger(__name__)

REPEAT_HOURS = 72
REPEAT_MIN_WORDS = 4
REPEAT_SIMILARITY = 0.6
DOUBLE_MINUTES = 15
UNANSWERED_HOURS = 2

STRONG = (
    ("!!!", re.compile(r"!{2,}")),
    ("???", re.compile(r"\?{3,}")),
    ("nadávka", re.compile(r"\b(tvl|kurva|sakra|do prdele|doprdele|kua|wtf)\b")),
    ("nefunguje", re.compile(r"\bnefung\w*")),
    ("dokonči", re.compile(r"\bdokonc\w*")),
    ("kolikrát", re.compile(r"\bkolikrat\b|\bpo(?:kolikate|kolikaty)\b")),
    ("pořád ne", re.compile(r"\b(?:porad|furt|stale)\s+(?:ne|nic|to\s+ne)\w*|\buz\s+zase\b|\bzase\s+ne\w*")),
)
WEAK = (
    ("zase", re.compile(r"\bzas(?:e)?\b|\bopet\b")),
    ("pořád", re.compile(r"\bporad\b|\bfurt\b")),
    ("proč", re.compile(r"\bproc\b")),
)
_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = {"ten", "tak", "jak", "aby", "ale", "jsem", "jsi", "jsou", "neni", "bude", "byl", "bylo", "uz", "pro", "pri",
         "the", "and", "kdyz", "jeste", "mas", "mam", "mame", "tohle", "toto", "tady", "taky", "nebo", "prosim", "hele",
         "zase", "zas", "porad", "furt", "proc", "ktery", "ktere", "jako", "jen", "mit", "ted", "ani", "oprav"}

_SCHEMA = """CREATE TABLE IF NOT EXISTS owner_frustration (
    message_id  INTEGER PRIMARY KEY,
    channel_id  INTEGER NOT NULL,
    markers     TEXT NOT NULL DEFAULT '[]',
    repeat_of   INTEGER,
    task_id     INTEGER,
    created_at  TEXT NOT NULL
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def _fold(text: str) -> str:
    from .scorecard import fold

    return fold(text)


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(_fold(text)) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if len(wa) < REPEAT_MIN_WORDS or len(wb) < REPEAT_MIN_WORDS:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def markers(text: str) -> list[str]:
    """The frustration markers in one text (no repeat check): strong ones, and weak ones only with
    another marker or an "!"."""
    t = _fold(text)
    strong = [name for name, rx in STRONG if rx.search(t)]
    weak = [name for name, rx in WEAK if rx.search(t)]
    if strong or (weak and ("!" in t or len(weak) >= 2)):
        return strong + weak
    return []


def repeat_of(conn: sqlite3.Connection, owner: int, message_id: int, body: str, at: str | None = None) -> int | None:
    """An earlier message of the owner (last 72 h) asking mostly the same thing."""
    since = ((datetime.fromisoformat(at) if at else datetime.now(timezone.utc)) - timedelta(hours=REPEAT_HOURS)) \
        .isoformat(timespec="seconds")
    best, best_id = 0.0, None
    for r in conn.execute("""SELECT id, body FROM chat_messages WHERE author_id = ? AND id < ? AND created_at >= ?
                             AND archived_at IS NULL ORDER BY id DESC LIMIT 200""", (owner, message_id, since)):
        s = similarity(body, r["body"] or "")
        if s >= REPEAT_SIMILARITY and s > best:
            best, best_id = s, r["id"]
    return best_id


def detect(conn: sqlite3.Connection, message_id: int, body: str, owner: int | None = None,
           at: str | None = None) -> dict | None:
    """{markers, repeat_of} when the owner's message is frustrated, else None."""
    owner = owner or actors.owner_id(conn)
    found = markers(body)
    rep = repeat_of(conn, owner, message_id, body, at)
    if rep is not None:
        found = ["opakovaný požadavek", *found]
    return {"markers": found, "repeat_of": rep} if found else None


# ------------------------------------------------------------------ the hook

def _context(conn: sqlite3.Connection, channel_id: int, message_id: int, limit: int = 4) -> str:
    rows = conn.execute("""SELECT m.id, m.body, m.created_at, a.name FROM chat_messages m JOIN actors a ON a.id = m.author_id
                           WHERE m.channel_id = ? AND m.id < ? AND m.archived_at IS NULL ORDER BY m.id DESC LIMIT ?""",
                        (channel_id, message_id, limit)).fetchall()
    return "\n".join(f"> **{r['name']}** ({r['created_at'][11:16]}): {' '.join((r['body'] or '').split())[:300]}"
                     for r in reversed(rows)) or "> (nic před tím)"


def _channel_label(conn: sqlite3.Connection, ch: sqlite3.Row) -> str:
    if ch["kind"] == "group":
        return f"#{ch['name']}"
    other = conn.execute("""SELECT a.name FROM channel_members m JOIN actors a ON a.id = m.actor_id
                            WHERE m.channel_id = ? AND a.is_owner = 0 LIMIT 1""", (ch["id"],)).fetchone()
    return f"DM s {other['name']}" if other else "DM"


def on_owner_message(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, message_id: int, body: str) -> dict | None:
    """Called for every owner message (pos.chat.send). A flagged one goes to the CEO now. Never raises."""
    try:
        return _flag(conn, ctx, ch, message_id, body)
    except Exception:  # noqa: BLE001 - the owner's message is sent whatever happens here
        log.exception("frustration check failed for message %s", message_id)
        return None


def _flag(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, message_id: int, body: str) -> dict | None:
    from . import comments, notices, tasks, wake

    found = detect(conn, message_id, body, ctx.actor_id)
    if not found:
        return None
    ensure_schema(conn)
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO owner_frustration (message_id, channel_id, markers, repeat_of, created_at) "
                 "VALUES (?, ?, ?, ?, ?)", (message_id, ch["id"], json.dumps(found["markers"], ensure_ascii=False),
                                            found["repeat_of"], now))
    ceo = notices.ceo(conn)
    if not ceo:
        return found
    where = _channel_label(conn, ch)
    text = " ".join((body or "").split())[:600]
    rep = ""
    if found["repeat_of"]:
        r = conn.execute("SELECT body, created_at FROM chat_messages WHERE id = ?", (found["repeat_of"],)).fetchone()
        rep = (f"\n\n**Opakuje požadavek z {r['created_at'][:16].replace('T', ' ')} UTC:** "
               f"„{' '.join((r['body'] or '').split())[:300]}“") if r else ""
    block = (f"**{where}**, zpráva {message_id}: „{text}“\n\nZnaky: {', '.join(found['markers'])}{rep}\n\n"
             f"Předtím:\n{_context(conn, ch['id'], message_id)}\n\nOdkaz: /chat/{ch['id']}?msg={message_id}")
    day = datetime.now(timezone.utc).astimezone(TZ).date().isoformat()
    title = f"Frustrace majitele · {day}"
    sys_ctx = notices.system_ctx(conn)
    open_task = conn.execute("SELECT id FROM tasks WHERE title = ? AND status != 'done' AND archived_at IS NULL",
                             (title,)).fetchone()
    if open_task:
        comments.add(conn, sys_ctx, open_task["id"], "Další frustrovaná zpráva majitele:\n\n" + block, notify=False)
        tid = open_task["id"]
    else:
        t = tasks.create(conn, sys_ctx, {
            "title": title, "assignee": {"type": "agent", "id": ceo}, "status": "next", "priority": 1,
            "deadline": day, "topic": "board", "source": "frustration",
            "notes": ("### Proč\nMajitel napsal frustrovanou zprávu (detektor v kódu: vykřičníky, „nefunguje“, "
                      "„zase“, „dokonči“, nadávka nebo opakovaný požadavek). Něco, co měl mít, nemá. Najdi "
                      "**příčinu** a oprav ji ještě dnes, ne jen tuhle zprávu.\n\n### Zpráva\n" + block +
                      "\n\n### Co udělat\n1. Zjisti, co nefunguje nebo co se nedodalo a proč (úkol, agent, routina).\n"
                      "2. Oprav příčinu nebo ji dej tomu, kdo ji opraví, s termínem dnes.\n"
                      "3. Majiteli jedna odpověď ve stejném vlákně: co se stalo, co je opravené, co se změní, "
                      "aby se to neopakovalo.\n4. Do poznámek úkolu zapiš příčinu (pro týdenní zlepšení platformy)."),
            "definition_of_done": "Příčina je zapsaná a opravená (nebo předaná s termínem dnes); majitel má jednu "
                                  "odpověď s tím, co se změnilo."})
        tid = t["id"]
    conn.execute("UPDATE owner_frustration SET task_id = ? WHERE message_id = ?", (tid, message_id))
    from . import audit

    audit.log(conn, sys_ctx, "owner_frustration", "task", tid, message=message_id, markers=found["markers"],
              repeat_of=found["repeat_of"])
    wake.wake(ceo)
    return {**found, "task_id": tid}


# ------------------------------------------------------------------ counts

def flagged(conn: sqlite3.Connection, s: str, u: str) -> list[dict]:
    ensure_schema(conn)
    return [{**dict(r), "markers": json.loads(r["markers"] or "[]")} for r in conn.execute(
        """SELECT f.*, m.body FROM owner_frustration f LEFT JOIN chat_messages m ON m.id = f.message_id
           WHERE f.created_at >= ? AND f.created_at <= ? ORDER BY f.message_id""", (s, u))]


def _owner_messages(conn: sqlite3.Connection, owner: int, s: str, u: str) -> list[sqlite3.Row]:
    return conn.execute("""SELECT m.*, c.kind AS ch_kind, c.name AS ch_name FROM chat_messages m
                           JOIN channels c ON c.id = m.channel_id
                           WHERE m.author_id = ? AND m.created_at >= ? AND m.created_at <= ? AND m.archived_at IS NULL
                           ORDER BY m.id""", (owner, s, u)).fetchall()


def _agent_replies(conn: sqlite3.Connection, m: sqlite3.Row, owner: int, until: datetime) -> list[sqlite3.Row]:
    """Agent messages in the channel after the owner's message, before his next one there (up to `until`)."""
    nxt = conn.execute("SELECT MIN(id) FROM chat_messages WHERE channel_id = ? AND author_id = ? AND id > ?",
                       (m["channel_id"], owner, m["id"])).fetchone()[0]
    return conn.execute(f"""SELECT x.id, x.author_id, x.created_at FROM chat_messages x JOIN actors a ON a.id = x.author_id
                            WHERE x.channel_id = ? AND x.id > ? {'AND x.id < ?' if nxt else ''} AND x.created_at < ?
                            AND a.kind != 'human' AND x.archived_at IS NULL ORDER BY x.id""",
                        (m["channel_id"], m["id"], *([nxt] if nxt else []), until.isoformat(timespec="seconds"))).fetchall()


def stats(conn: sqlite3.Connection, now: datetime | None = None, days: int = 7) -> dict:
    """{frustrations, double_answers, unanswered} over the last `days`."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    s, u = (now - timedelta(days=days)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds")
    owner = actors.owner_id(conn)
    system = actors.system_id(conn)
    double = unanswered = 0
    talks: dict[int, bool] = {}
    for m in _owner_messages(conn, owner, s, u):
        if (m["ch_name"] or "").lower() in ("system",):
            continue
        if m["ch_kind"] == "dm":
            if m["channel_id"] not in talks:  # a DM with an agent (not a person, not PersonalOS's notices)
                talks[m["channel_id"]] = conn.execute(
                    """SELECT 1 FROM channel_members cm JOIN actors a ON a.id = cm.actor_id WHERE cm.channel_id = ?
                       AND a.kind != 'human' AND a.id != ?""", (m["channel_id"], system)).fetchone() is not None
            if not talks[m["channel_id"]]:
                continue
        at = datetime.fromisoformat(m["created_at"])
        replies = [r for r in _agent_replies(conn, m, owner, min(now, at + timedelta(hours=UNANSWERED_HOURS)))
                   if r["author_id"] != system]
        quick = [r for r in replies if datetime.fromisoformat(r["created_at"]) <= at + timedelta(minutes=DOUBLE_MINUTES)]
        if len(quick) >= 2:
            double += 1
        addressed = m["ch_kind"] == "dm" or (m["mentions"] or "[]") not in ("[]", "")
        if addressed and not replies and now - at >= timedelta(hours=UNANSWERED_HOURS):
            unanswered += 1
    return {"frustrations": len(flagged(conn, s, u)), "double_answers": double, "unanswered": unanswered}
