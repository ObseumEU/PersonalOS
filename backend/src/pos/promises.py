"""The CEO's promise ledger: what the CEO promised the owner, with a date, is a task with that deadline.

Prod 2026-09/10: the CEO broke 5 dated promises to the owner (#448, #770, #1019, #1066, #1229
"daily report dnes v 16:00", never sent). Nothing remembered them.

Every message from the CEO that reaches the owner (pos.chat.send) is read for dated commitments:

1. cheap rules (`extract`): a time expression that lies ahead ("dnes v 16:00", "zítra", "v pátek",
   "do konce týdne", "5. 10.", "2026-10-05", "za 2 hodiny") in a sentence that is not a question
   and not about the past. Each becomes a task for the CEO ("Slib Ownerovi: …") with that
   deadline, the exact time kept in `owner_promises`;
2. a sentence with a commitment ("pošlu", "připravím", "ozvu se" …) whose time the rules cannot
   read is left for the haiku fallback (`tick`, the scheduler job `promises_tick`), which answers
   in JSON; an unreadable answer records nothing.

`tick` also tells the CEO once (from PersonalOS) when a promise passed its time undone, and the
CEO's 16:00 routine (pos.schedules.fire → `with_missed`) starts with the missed promises.
"""

import json
import logging
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone

from . import actors, audit
from .core import TZ, Ctx, now_iso

log = logging.getLogger("pos.promises")

MODEL = os.environ.get("POS_PROMISES_MODEL", "claude-haiku-4-5")
SOURCE = "promise:"

_SCHEMA = """CREATE TABLE IF NOT EXISTS owner_promises (
    id          INTEGER PRIMARY KEY,
    message_id  INTEGER NOT NULL,
    ceo_id      INTEGER NOT NULL,
    text        TEXT NOT NULL,
    due_at      TEXT,
    task_id     INTEGER,
    status      TEXT NOT NULL DEFAULT 'open',
    how         TEXT NOT NULL DEFAULT 'rules',
    missed_at   TEXT,
    created_at  TEXT NOT NULL
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS owner_promises_msg ON owner_promises (message_id)")


# ------------------------------------------------------------------ the rules

WEEKDAYS = {"pondělí": 0, "pondeli": 0, "pondělka": 0, "úterý": 1, "utery": 1, "úterka": 1, "středu": 2,
            "streda": 2, "středa": 2, "stredu": 2, "středy": 2, "čtvrtek": 3, "ctvrtek": 3, "čtvrtka": 3,
            "pátek": 4, "patek": 4, "pátku": 4, "patku": 4, "sobotu": 5, "sobota": 5, "soboty": 5,
            "neděli": 6, "nedeli": 6, "neděle": 6}
MONTHS = {"ledna": 1, "února": 2, "března": 3, "dubna": 4, "května": 5, "června": 6, "července": 7,
          "srpna": 8, "září": 9, "října": 10, "listopadu": 11, "prosince": 12}
PARTS = {"ráno": time(9, 0), "rano": time(9, 0), "dopoledne": time(11, 0), "v poledne": time(12, 0),
         "odpoledne": time(16, 0), "večer": time(20, 0), "vecer": time(20, 0)}
DEFAULT_TIME = time(17, 0)  # "zítra", "v pátek": by the end of that working day

CUE = re.compile(
    r"(?i)\b(pošlu|pošleme|zašlu|udělám|uděláme|dodám|dodáme|připravím|připravíme|ozvu|ozveme|dám vědět|"
    r"dáme vědět|nasadím|nasadíme|dokončím|dokončíme|dotáhnu|vyřeším|vyřešíme|zjistím|napíšu|napíšeme|"
    r"pustím|spustím|budeš mít|bude(?:me)? hotov\w*|přijde ti|dostaneš|pošle ti|připraví|dodá|report|přehled)\b")
_PAST = re.compile(r"(?i)\b(včera|minul\w*|proběhl\w*|byl[aoiy]?|poslal[aoiy]?|udělal[aoiy]?|hotovo)\b")
_CLOCK = r"(\d{1,2})(?::|\.)(\d{2})|(\d{1,2})\s*h\b"


def _clock(m) -> time | None:
    if m is None:
        return None
    h, mi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), 0)
    return time(h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def _sentences(text: str) -> list[str]:
    # A sentence ends at . ! ? before a capital letter (not inside "7. 10." or "5. října").
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ])|\n+", text or "")
    return [p.strip(" -*•\t") for p in parts if p.strip(" -*•\t")]


def _at(d: date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=TZ)


def _due(sentence: str, now: datetime) -> datetime | None:
    """The time a sentence commits to, in Prague time, or None."""
    s = sentence.lower()
    local = now.astimezone(TZ)
    today = local.date()
    clock = re.search(r"(?:\bve?|\bdo|\bkolem|\bnejpozději|\bpo)\s+(?:" + _CLOCK + ")", s)
    t = _clock(clock)
    part = next((v for k, v in PARTS.items() if re.search(rf"\b{k}\b", s)), None)

    m = re.search(r"\bza\s+(\d{1,3}|hodinu|půl hodiny)\s*(minut|min|hodin\w*|h)?\b", s)
    if m and CUE.search(s):
        n = {"hodinu": 60, "půl hodiny": 30}.get(m.group(1))
        if n is None:
            n = int(m.group(1)) * (60 if (m.group(2) or "").startswith(("hod", "h")) else 1)
        return local + timedelta(minutes=n)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        try:
            return _at(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), t or part or DEFAULT_TIME)
        except ValueError:
            return None
    m = re.search(r"(?<![\d.])(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{4}))?", s)
    if m and not re.match(r"\d{1,2}\.\d{2}\b", m.group(0)):
        try:
            d = date(int(m.group(3) or today.year), int(m.group(2)), int(m.group(1)))
        except ValueError:
            d = None
        if d is not None:
            if d < today and not m.group(3):
                d = d.replace(year=d.year + 1)
            return _at(d, t or part or DEFAULT_TIME)
    m = re.search(r"\b(\d{1,2})\.\s*(" + "|".join(MONTHS) + r")\b", s)
    if m:
        try:
            d = date(today.year, MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
        if d < today:
            d = d.replace(year=d.year + 1)
        return _at(d, t or part or DEFAULT_TIME)
    if re.search(r"\bpozítří\b|\bpozitri\b", s):
        return _at(today + timedelta(days=2), t or part or DEFAULT_TIME)
    if re.search(r"\bzítra\b|\bzitra\b|\bzítřk\w*", s):
        return _at(today + timedelta(days=1), t or part or DEFAULT_TIME)
    if re.search(r"\bdo konce týdne\b|\bdo konce tydne\b|\bkoncem týdne\b", s):
        return _at(today + timedelta(days=(4 - today.weekday()) % 7), t or DEFAULT_TIME)
    m = re.search(r"\b(?:v|ve|do|nejpozději v)\s+(" + "|".join(WEEKDAYS) + r")\b", s)
    if m:
        wd = WEEKDAYS[m.group(1)]
        ahead = (wd - today.weekday()) % 7
        d = today + timedelta(days=ahead)
        due = _at(d, t or part or DEFAULT_TIME)
        return due if due > local else due + timedelta(days=7)
    if re.search(r"\bdnes\b|\bdneska\b|\bdnešk\w*", s):
        return _at(today, t or part or time(18, 0))
    if t and CUE.search(s):  # "pošlu v 16:00": today, or tomorrow when that time has passed
        due = _at(today, t)
        return due if due > local else due + timedelta(days=1)
    return None


def extract(text: str, now: datetime | None = None) -> list[dict]:
    """Dated commitments in the text: [{text, due_at (UTC ISO)}]; future times only."""
    now = now or datetime.now(timezone.utc)
    out = []
    for sent in _sentences(text):
        if sent.endswith("?") or _PAST.search(sent) and not CUE.search(sent):
            continue
        due = _due(sent, now)
        if due is None or due <= now:
            continue
        out.append({"text": sent[:300], "due_at": due.astimezone(timezone.utc).isoformat(timespec="seconds")})
    return out


def unparsed(text: str, found: list[dict]) -> list[str]:
    """Sentences with a commitment cue the rules gave no time to (for the haiku fallback)."""
    have = {f["text"] for f in found}
    return [s[:300] for s in _sentences(text) if CUE.search(s) and not s.endswith("?") and s[:300] not in have
            and re.search(r"(?i)\b(dnes|zítra|týden|týdne|pátek|odpoledne|večer|ráno|brzy|hned|během|do |v \d)", s)]


# ------------------------------------------------------------------ the ledger

def _local(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d. %m. %H:%M")


def _create(conn: sqlite3.Connection, ctx: Ctx, message_id: int, text: str, due_at: str, how: str) -> str | None:
    from . import tasks

    ensure_schema(conn)
    due_local = datetime.fromisoformat(due_at).astimezone(TZ)
    dup = conn.execute("""SELECT p.task_id FROM owner_promises p LEFT JOIN tasks t ON t.id = p.task_id
                          WHERE p.ceo_id = ? AND p.text = ? AND substr(p.due_at, 1, 10) = substr(?, 1, 10)
                          AND (t.id IS NULL OR t.status != 'done')""", (ctx.actor_id, text, due_at)).fetchone()
    if dup:
        return None
    ch = conn.execute("SELECT channel_id FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    link = f"/chat?c={ch['channel_id']}&m={message_id}" if ch else ""
    t = tasks.create(conn, Ctx(ctx.actor_id, via="system"), {
        "title": f"Slib Ownerovi: {text}"[:200],
        "assignee": {"type": "agent", "id": ctx.actor_id}, "status": "next", "priority": 1,
        "deadline": due_local.date().isoformat(), "reviewer": ctx.actor_id, "source": f"{SOURCE}{message_id}",
        "notes": (f"### Proč\nSlíbil jsi Ownerovi: „{text}“ — do **{_local(due_at)}**.\n\n"
                  f"### Odkud\n[Tvoje zpráva Ownerovi]({link}) (zachycená knihou slibů, pos.promises).\n\n"
                  "### Hotovo znamená\nOwner to má (zpráva, soubor nebo odkaz) nejpozději v termínu. Když to "
                  "nestihneš, napiš mu PŘED termínem jednou větou nový termín a proč."),
        "definition_of_done": f"Owner dostal slíbené do {_local(due_at)}, nebo včas nový termín.",
    })
    conn.execute("""INSERT INTO owner_promises (message_id, ceo_id, text, due_at, task_id, how, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (message_id, ctx.actor_id, text, due_at, t["id"], how, now_iso()))
    audit.log(conn, ctx, "promise_recorded", "task", t["id"], message=message_id, due_at=due_at, how=how)
    return t["ref"]


def record(conn: sqlite3.Connection, ctx: Ctx, message_id: int, body: str, now: datetime | None = None) -> list[str]:
    """The CEO's message to the owner: its dated commitments become tasks; commitments without a
    readable time wait for the haiku fallback. Never raises (the message is sent either way)."""
    try:
        ensure_schema(conn)
        found = extract(body, now)
        refs = [r for r in (_create(conn, ctx, message_id, f["text"], f["due_at"], "rules") for f in found) if r]
        for sent in unparsed(body, found)[:3]:
            conn.execute("""INSERT INTO owner_promises (message_id, ceo_id, text, status, how, created_at)
                            VALUES (?, ?, ?, 'pending', 'llm', ?)""", (message_id, ctx.actor_id, sent, now_iso()))
        return refs
    except Exception:  # noqa: BLE001 - the ledger must never block a message
        log.exception("promise ledger failed for message %s", message_id)
        return []


def _ask_model(conn: sqlite3.Connection, actor_id: int, sentence: str, now: datetime) -> dict | None:
    """The haiku fallback: {"promise": bool, "due": "YYYY-MM-DDTHH:MM"} (Prague time) or None."""
    from . import integrations, runner

    integrations.install()
    local = now.astimezone(TZ)
    prompt = ("Is this sentence, written by a CEO to the company owner, a promise to deliver something by a "
              f"certain time? Now it is {local:%A %Y-%m-%d %H:%M} (Europe/Prague). Answer with JSON only: "
              '{"promise": true|false, "due": "YYYY-MM-DDTHH:MM" or null}. Use 17:00 for a day without a time.'
              f"\n\nSentence: {sentence}")
    res = runner.run(conn, runner.RunRequest(actor_id, "promise_parse", prompt, engine="claude", model=MODEL,
                                             timeout_s=60))
    if res.status != "ok":
        return None
    m = re.search(r"\{.*\}", res.output or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


def tick(conn: sqlite3.Connection, now: datetime | None = None, ask=None) -> dict:
    """The haiku fallback for pending sentences, and the CEO hears once about a missed promise."""
    from . import notices, tasks

    ensure_schema(conn)
    now = now or datetime.now(timezone.utc)
    ask = ask or _ask_model
    made, missed = [], []
    for p in conn.execute("SELECT * FROM owner_promises WHERE status = 'pending' ORDER BY id LIMIT 5").fetchall():
        try:
            out = ask(conn, p["ceo_id"], p["text"], now)
        except Exception:  # noqa: BLE001 - the model may be out: try again next tick, then give up
            out = None
        conn.execute("UPDATE owner_promises SET status = 'parsed' WHERE id = ?", (p["id"],))
        if not out or not out.get("promise") or not out.get("due"):
            continue
        try:
            due = datetime.fromisoformat(str(out["due"]))
        except ValueError:
            continue
        due = (due if due.tzinfo else due.replace(tzinfo=TZ)).astimezone(timezone.utc)
        if due <= now:
            continue
        ref = _create(conn, Ctx(p["ceo_id"], via="system"), p["message_id"], p["text"],
                      due.isoformat(timespec="seconds"), "llm")
        if ref:
            made.append(ref)
    for p in _missed_rows(conn, now):
        if p["missed_at"]:
            continue
        conn.execute("UPDATE owner_promises SET missed_at = ? WHERE id = ?", (now_iso(), p["id"]))
        try:
            notices.dm(conn, p["ceo_id"], f"Nesplněný slib Ownerovi: „{p['text']}“ (termín {_local(p['due_at'])}, "
                                          f"{tasks.display_id(p['task_id'])}). Dodej to hned, nebo mu jednou větou "
                                          "napiš nový termín a proč.",
                       attachments=[{"type": "task", "id": p["task_id"]}], priority="change_plan")
        except Exception:  # noqa: BLE001
            pass
        missed.append(tasks.display_id(p["task_id"]))
    conn.commit()
    return {k: v for k, v in (("recorded", made), ("missed", missed)) if v}


def _missed_rows(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.* FROM owner_promises p JOIN tasks t ON t.id = p.task_id
           WHERE p.task_id IS NOT NULL AND p.due_at < ? AND t.status NOT IN ('done', 'review')
             AND t.archived_at IS NULL ORDER BY p.due_at""",
        (now.isoformat(timespec="seconds"),)).fetchall()


def missed(conn: sqlite3.Connection, ceo_id: int | None = None, now: datetime | None = None) -> list[dict]:
    from . import tasks

    ensure_schema(conn)
    rows = _missed_rows(conn, now or datetime.now(timezone.utc))
    return [{"ref": tasks.display_id(r["task_id"]), "text": r["text"], "due": _local(r["due_at"])}
            for r in rows if ceo_id is None or r["ceo_id"] == ceo_id]


def with_missed(conn: sqlite3.Connection, assignee_id: int, schedule: str, template: dict) -> dict:
    """The CEO's 16:00 routine starts with the promises it broke (pos.schedules.fire)."""
    try:
        a = actors.get(conn, assignee_id)
        if a["role"] != "ceo" or "16:00" not in (schedule or ""):
            return template
        rows = missed(conn, assignee_id)
        if not rows:
            return template
        head = ["### Nejdřív: nesplněné sliby Ownerovi",
                "Než uděláš cokoli jiného, každý z nich buď dodej hned, nebo Ownerovi jednou zprávou napiš "
                "nový termín a proč (a uprav termín úkolu). V přehledu je uveď jako první.",
                *[f"- {r['ref']} „{r['text']}“ (termín {r['due']})" for r in rows], ""]
        return {**template, "notes": "\n".join(head) + "\n" + (template.get("notes") or "")}
    except Exception:  # noqa: BLE001 - the routine fires either way
        log.exception("could not add the missed promises")
        return template
