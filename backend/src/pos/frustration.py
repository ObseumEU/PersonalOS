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

Made robust 2026-10-05 (prod: 0 rows in owner_frustration after two days; it missed "…at se
nezasejavaj v zduchoprazdnu!", the same status question 4 times in 5 days and 9 owner messages
unanswered for over 2 h):

- markers match with typos: a stem of 6+ letters within one edit of a word (or of two adjacent words
  run together: "v zduchoprazdnu"), so "nezasejavaj" is "nezasekavaj"; new strong stems
  ("vzduchoprazdno", "naprd", "neotravuj") and weak ones ("zasekl…", "nekdo musi", "konecne",
  "bez vysledku");
- a **repeated status question** ("v jakem stavu", "jak to vypada", "kdy bude", "na cem stoji"):
  the third one in REPEAT_STATUS_HOURS (5 days) on the same subject (or in the same conversation,
  when it names none) is flagged;
- **unanswered**: `unanswered_sweep` (in the core routines_overdue loop) flags each owner message to
  an agent that nobody answered in UNANSWERED_HOURS, once, to the same CEO task;
- the platform's notices that once went out in the owner's name ("Owner handed in T-375 …",
  msgs 1230, 1400, 1406) are not his words: never flagged nor counted. (They are sent by PersonalOS
  now: pos.chat.send_dm.)
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
REPEAT_STATUS_HOURS = 120
REPEAT_STATUS_EARLIER = 2  # the third status question on one subject in 5 days is flagged
UNANSWERED_LOOKBACK_HOURS = 48
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
    ("někdo musí", re.compile(r"\bnekdo\s+(?:musi|ma)\b")),
    ("konečně", re.compile(r"\bkonecne\b")),
    ("bez výsledku", re.compile(r"\bbez\s+vysledk\w*")),
)
# Stems matched with one typo (pos. 0-2 of a word, or of two adjacent words run together): the owner
# writes fast and without háčky ("nezasejavaj v zduchoprazdnu").
FUZZY = (
    ("nefunguje", ("nefunguj",)),
    ("vzduchoprázdno", ("vzduchoprazd",)),
    ("na prd", ("naprd",)),
    ("neotravuj", ("neotravuj",)),
    ("dokonči", ("dokoncet", "dokonci")),
    ("kolikrát", ("kolikrat",)),
)
# Typo-tolerant weak stems: "zaseknuté úkoly" is often a plain description; with an "!" or another
# marker it is a complaint ("at se nezasejavaj v zduchoprazdnu!").
FUZZY_WEAK = (
    ("zaseklé", ("nezasek", "zasekl", "zaseknut", "zasekav")),
)
_TOKEN = re.compile(r"[a-z0-9]+")
STATUS_Q = re.compile(r"\bv\s+jakem\s+stavu\b|\bjak\s+(?:to\s+)?(?:vypada|jsme\s+na\s+tom|je\s+na\s+tom)\b|"
                      r"\bkdy\s+(?:bude|budu|to\s+bude|uz)\b|\bna\s+cem\s+stoji\b|\bco\s+je\s+s\b|"
                      r"\bje\s+(?:to\s+)?uz\b|\buz\s+je\b|\bjak\s+daleko\b|\bstav\s+(?:aplikace|ukolu|projektu)\b")
_STATUS_WORDS = {"jakem", "stavu", "vypada", "kdy", "bude", "budu", "stoji", "cem", "daleko", "nekde", "vyzkouseni",
                 "vysledek", "hotovo", "hotove", "stav", "neco", "nejak", "myslim"}
# Platform notices sent in the owner's name before they had their own voice: not his words.
NOTICE_RE = re.compile(r"^(?:Owner|David)\s+(?:handed in|resolved your ask|approved your request|rejected your "
                       r"request|rozhodl k)\b")
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


def _within_one(a: str, b: str) -> bool:
    """Levenshtein distance of two strings is at most 1."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:] if len(a) < len(b) else a[i + 1:] == b[i + 1:]


def _fuzzy_hit(stem: str, token: str) -> bool:
    """`stem` at the start of `token` (after up to two letters: "ne", "be"), exact for short stems,
    within one typo for stems of 6+ letters."""
    for i in range(0, 3):
        part = token[i:]
        if len(part) < len(stem) - 1:
            break
        if part.startswith(stem):
            return True
        if len(stem) >= 6 and any(_within_one(stem, part[:n]) for n in (len(stem) - 1, len(stem), len(stem) + 1)):
            return True
    return False


def fuzzy_markers(folded: str, table=FUZZY) -> list[str]:
    toks = _TOKEN.findall(folded)
    cands = toks + [a + b for a, b in zip(toks, toks[1:], strict=False)]
    return [name for name, stems in table if any(_fuzzy_hit(st, c) for st in stems for c in cands)]


def is_notice(body: str) -> bool:
    return bool(NOTICE_RE.search((body or "").strip()))


def markers(text: str) -> list[str]:
    """The frustration markers in one text (no repeat check): strong ones (typo-tolerant), and weak
    ones only with another marker or an "!"."""
    t = _fold(text)
    if is_notice(text):
        return []
    strong = [name for name, rx in STRONG if rx.search(t)]
    strong += [n for n in fuzzy_markers(t) if n not in strong]
    weak = [name for name, rx in WEAK if rx.search(t)] + fuzzy_markers(t, FUZZY_WEAK)
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


def is_status_question(text: str) -> bool:
    t = _fold(text)
    return "?" in t and bool(STATUS_Q.search(t)) or bool(STATUS_Q.search(t)) and len(_TOKEN.findall(t)) <= 12


def _subject(text: str) -> set[str]:
    """The subject of a status question: its other words' first four letters ("aplikace", "aplialce")."""
    return {w[:4] for w in words(text) if w not in _STATUS_WORDS and len(w) >= 4}


def repeated_status(conn: sqlite3.Connection, owner: int, message_id: int, body: str,
                    at: str | None = None) -> list[int]:
    """Earlier status questions of the owner (last REPEAT_STATUS_HOURS) about the same subject, or in
    the same conversation when this one names none; [] when this is no status question."""
    if not is_status_question(body):
        return []
    since = ((datetime.fromisoformat(at) if at else datetime.now(timezone.utc))
             - timedelta(hours=REPEAT_STATUS_HOURS)).isoformat(timespec="seconds")
    me = conn.execute("SELECT channel_id FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    subject = _subject(body)
    out = []
    for r in conn.execute("""SELECT id, channel_id, body FROM chat_messages WHERE author_id = ? AND id < ?
                             AND created_at >= ? AND archived_at IS NULL ORDER BY id DESC LIMIT 300""",
                          (owner, message_id, since)):
        if is_notice(r["body"]) or not is_status_question(r["body"] or ""):
            continue
        if (subject & _subject(r["body"] or "")) if subject else (me is not None and r["channel_id"] == me[0]):
            out.append(r["id"])
    return out


def detect(conn: sqlite3.Connection, message_id: int, body: str, owner: int | None = None,
           at: str | None = None) -> dict | None:
    """{markers, repeat_of} when the owner's message is frustrated, else None."""
    if is_notice(body):
        return None
    owner = owner or actors.owner_id(conn)
    found = markers(body)
    rep = repeat_of(conn, owner, message_id, body, at)
    if rep is not None:
        found = ["opakovaný požadavek", *found]
    status = repeated_status(conn, owner, message_id, body, at)
    if len(status) >= REPEAT_STATUS_EARLIER:
        found = [f"opakovaný dotaz na stav ({len(status) + 1}× za {REPEAT_STATUS_HOURS // 24} dní)", *found]
        rep = rep or status[0]
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


def _flag(conn: sqlite3.Connection, ctx: Ctx, ch: sqlite3.Row, message_id: int, body: str,
          found: dict | None = None) -> dict | None:
    from . import comments, notices, tasks, wake

    found = found or detect(conn, message_id, body, ctx.actor_id)
    if not found:
        return None
    ensure_schema(conn)
    now = now_iso()
    have = conn.execute("SELECT markers FROM owner_frustration WHERE message_id = ?", (message_id,)).fetchone()
    if have is not None:
        merged = list(dict.fromkeys([*json.loads(have["markers"] or "[]"), *found["markers"]]))
        conn.execute("UPDATE owner_frustration SET markers = ? WHERE message_id = ?",
                     (json.dumps(merged, ensure_ascii=False), message_id))
    else:
        conn.execute("INSERT INTO owner_frustration (message_id, channel_id, markers, repeat_of, created_at) "
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
    """The owner's own messages (not the platform's notices once sent in his name)."""
    return [m for m in conn.execute("""SELECT m.*, c.kind AS ch_kind, c.name AS ch_name FROM chat_messages m
                                       JOIN channels c ON c.id = m.channel_id
                                       WHERE m.author_id = ? AND m.created_at >= ? AND m.created_at <= ?
                                       AND m.archived_at IS NULL ORDER BY m.id""", (owner, s, u)).fetchall()
            if not is_notice(m["body"])]


def _talks_to_agent(conn: sqlite3.Connection, m: sqlite3.Row, system: int, cache: dict[int, bool]) -> bool:
    """The owner's message is addressed to an agent: a DM with an agent (not a person, not PersonalOS's
    notices) or a mention in a group."""
    if (m["ch_name"] or "").lower() in ("system",):
        return False
    if m["ch_kind"] != "dm":
        return (m["mentions"] or "[]") not in ("[]", "")
    if m["channel_id"] not in cache:
        cache[m["channel_id"]] = conn.execute(
            """SELECT 1 FROM channel_members cm JOIN actors a ON a.id = cm.actor_id WHERE cm.channel_id = ?
               AND a.kind != 'human' AND a.id != ?""", (m["channel_id"], system)).fetchone() is not None
    return cache[m["channel_id"]]


def unanswered_sweep(conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    """The owner's messages to an agent from the last UNANSWERED_LOOKBACK_HOURS that no agent answered
    (no agent message in that conversation after it) in UNANSWERED_HOURS: each flagged once to the
    CEO's frustration task ("bez odpovědi"). Code, no model; in the core routines_overdue loop."""
    from . import notices

    ensure_schema(conn)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    owner, system = actors.owner_id(conn), actors.system_id(conn)
    s = (now - timedelta(hours=UNANSWERED_LOOKBACK_HOURS)).isoformat(timespec="seconds")
    u = (now - timedelta(hours=UNANSWERED_HOURS)).isoformat(timespec="seconds")
    marker = f"bez odpovědi {UNANSWERED_HOURS} h"
    cache: dict[int, bool] = {}
    flagged_now = []
    for m in _owner_messages(conn, owner, s, u):
        if not _talks_to_agent(conn, m, system, cache):
            continue
        have = conn.execute("SELECT markers FROM owner_frustration WHERE message_id = ?", (m["id"],)).fetchone()
        if have is not None and marker in json.loads(have["markers"] or "[]"):
            continue
        answered = conn.execute("""SELECT 1 FROM chat_messages x JOIN actors a ON a.id = x.author_id
                                   WHERE x.channel_id = ? AND x.id > ? AND a.kind != 'human' AND a.id != ?
                                   AND x.archived_at IS NULL LIMIT 1""", (m["channel_id"], m["id"], system)).fetchone()
        if answered:
            continue
        ch = conn.execute("SELECT * FROM channels WHERE id = ?", (m["channel_id"],)).fetchone()
        try:
            _flag(conn, notices.system_ctx(conn), ch, m["id"], m["body"] or "", {"markers": [marker], "repeat_of": None})
            flagged_now.append(m["id"])
        except Exception:  # noqa: BLE001 - one message must not stop the sweep
            log.exception("could not flag unanswered message %s", m["id"])
    conn.commit()
    return flagged_now


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
        if m["ch_kind"] == "dm" and not _talks_to_agent(conn, m, system, talks):
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
