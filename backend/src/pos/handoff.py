"""Owner handoff in the agent's live browser (docs/BROWSER.md, "Předání majiteli").

An agent's browser sometimes needs the owner for one step the agent cannot or must not do: a login, a
2FA code, a CAPTCHA, an "Allow" on a consent screen, a terms checkbox. The agent first gets everything
ready (it navigates to the right page and fills in whatever it can), then calls
`browser_request_owner_handoff` (pos_worker.browser_guard). That:

- creates a handoff here (`waiting`) and moves the task to `waiting` (a pause, not a failure);
- puts one item in "Čeká na tebe" (pos.needs_me kind `handoff`), which pushes to the owner's phone
  ("Přihlas se do LinkedIn – zbytek udělám já");
- keeps the agent's run and its browser alive for a short live window (LIVE_MINUTES): the guard relays the
  page to the owner and the owner's clicks, taps, scrolls and typing back into the same page (this module's
  in-memory relay);
- ends when the owner presses **Hotovo** (or the agent's `done_hint` comes true: a URL, a text, a
  selector) or when he cancels it. The agent then continues in the same browser, logged in.

The ask itself never dies (prod 2026-10-07: the T-957/T-958 handoffs expired after 25-30 minutes and the owner,
with no push device, never saw them). When the live window passes without him, the handoff is **parked**: the
agent's run ends (no pool slot and no run wait for him), the task waits, and the item stays in "Čeká na tebe"
until it is done or cancelled. When he opens a parked handoff it becomes **preparing**: the task goes back to
the agent and wakes it; the agent prepares the page again and asks again with the same task, which re-attaches
to this handoff (now `active`, the owner is there), and his view goes from "Agent připravuje stránku…" to the
live page. If the agent has not attached within PREPARE_MINUTES, the handoff is parked again (opening it wakes
the agent once more).

Notice: the push of "Čeká na tebe" (pos.push); when the owner has no working push subscription, one e-mail to
his work mailbox per handoff (pos.owner_fallback), sent by the minute sweep.

Security:
- Only the owner opens a handoff (signed-in session, owner only: pos.api_handoff); nothing is public.
- Typed text and frames live only in this process's memory (`Relay`), never in the database, a file or
  a log; the audit keeps counts (clicks, keys, characters), never what was typed. The guard redacts the
  owner's typed text from everything the agent reads afterwards and masks password fields in every
  screenshot it stores.
- The audit log records who opened the handoff and when (`handoff_opened`), and how it ended.
- With "keep the login" (default) Hotovo grants the agent `browser:profile`: its cookies are kept
  encrypted server-side (pos.browser.save_profile), so the next run is logged in already.

One api process holds the relay (uvicorn runs one worker). Tables are created on first use.
"""

import json
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import actors, audit
from .core import Ctx, now_iso

LIVE_MINUTES = 5  # how long the agent's run waits for the owner (its browser stays open), then it parks
DEFAULT_MINUTES = LIVE_MINUTES
MAX_MINUTES = 10  # the most an agent may ask to wait: a run never holds a pool slot for long
OWNER_THERE_MINUTES = 20  # re-attached while the owner looks at it ("preparing"): he is there, give him time
PREPARE_MINUTES = 15  # the owner opened a parked one: the agent has this long to prepare it again
REMIND_AFTER_MIN = 60  # one reminder push for a parked handoff the owner has not opened by then
OPEN = ("waiting", "active")  # live: an agent's run holds the browser
PENDING = ("parked", "preparing")  # no run: waits for the owner (parked) or for the agent (preparing)
LISTED = OPEN + PENDING  # in "Čeká na tebe" until done or cancelled
FINAL = ("done", "cancelled", "expired")  # expired: only rows from before parking existed
WATCH_S = 12  # the owner's view asked for a frame this recently: the guard keeps sending frames
MAX_EVENTS = 60  # per input call
MAX_QUEUE = 400
MAX_TEXT = 500
MAX_FRAME = 3 * 1024 * 1024

_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_handoffs (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    task_id INTEGER,
    run_id INTEGER,
    title TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    url TEXT,
    done_hint TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    opened_at TEXT,
    finished_at TEXT,
    finished_by TEXT,
    reminded_at TEXT,
    task_status_before TEXT,
    keep_login INTEGER NOT NULL DEFAULT 0,
    closed_at TEXT,
    parked_at TEXT,
    notified_at TEXT,
    notified_via TEXT,
    reattached INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS browser_handoffs_status ON browser_handoffs(status);
"""
_COLUMNS = {"parked_at": "TEXT", "notified_at": "TEXT", "notified_via": "TEXT",
            "reattached": "INTEGER NOT NULL DEFAULT 0"}
_ready: set[str] = set()


class HandoffError(Exception):
    """A refusal with a reason the caller shows (HTTP 4xx)."""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def ensure_schema(conn: sqlite3.Connection) -> None:
    key = str(conn.execute("PRAGMA database_list").fetchone()[2])
    if key in _ready:
        return
    conn.executescript(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(browser_handoffs)")}
    for col, decl in _COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE browser_handoffs ADD COLUMN {col} {decl}")
    # Before parking existed a handoff expired; one the owner never closed whose task still waits is parked now
    # (it stays in "Čeká na tebe" and wakes the agent when he opens it), the rest is closed.
    conn.execute("""UPDATE browser_handoffs SET status = 'parked', parked_at = COALESCE(finished_at, created_at),
                    finished_at = NULL WHERE status = 'expired' AND closed_at IS NULL AND task_id IN
                    (SELECT id FROM tasks WHERE status NOT IN ('done', 'cancelled') AND archived_at IS NULL)""")
    conn.execute("UPDATE browser_handoffs SET closed_at = COALESCE(finished_at, created_at) "
                 "WHERE status = 'expired' AND closed_at IS NULL")
    conn.commit()
    _ready.add(key)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ the relay (memory only)

class Relay:
    """One handoff's live channel between the owner's view and the guard: the owner's input events
    (typed text included) and the newest frame. Never written anywhere."""

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.events: list[dict] = []
        self.frame: bytes | None = None
        self.version = 0
        self.meta: dict = {}
        self.viewed = 0.0  # monotonic time the owner's view last asked for a frame
        self.counts: Counter = Counter()  # for the audit: how many clicks, keys, characters (never what)
        self.changed = 0.0  # monotonic time of the last status change (wakes the guard's poll)


_relays: dict[int, Relay] = {}
_relays_lock = threading.Lock()


def relay(hid: int) -> Relay:
    with _relays_lock:
        r = _relays.get(int(hid))
        if r is None:
            r = _relays[int(hid)] = Relay()
        return r


def drop_relay(hid: int) -> None:
    with _relays_lock:
        r = _relays.pop(int(hid), None)
    if r is not None:
        with r.cond:
            r.events.clear()
            r.frame = None
            r.cond.notify_all()


def _wake(hid: int) -> None:
    r = relay(hid)
    with r.cond:
        r.changed = time.monotonic()
        r.cond.notify_all()


_KEY = re.compile(r"^(?:(?:Control|Shift|Alt|Meta)\+){0,3}(?:[A-Za-z0-9]|F\d{1,2}|Enter|Tab|Backspace|Delete|Escape|"
                  r"ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|Space|Insert|"
                  r"[`~!@#$%^&*()\-_=+\[\]{};:'\",.<>/?\\|])$")
_BUTTONS = ("left", "right", "middle")


def _unit(v) -> float:
    x = float(v)
    if x != x:  # NaN
        raise ValueError("NaN")
    return min(1.0, max(0.0, x))


def clean_event(e: dict) -> dict | None:
    """One owner input event, validated (coordinates are 0..1 of the page's viewport). None: dropped."""
    if not isinstance(e, dict):
        return None
    t = e.get("t")
    try:
        if t in ("click", "move", "down", "up"):
            out = {"t": t, "x": _unit(e["x"]), "y": _unit(e["y"])}
            if t != "move":
                out["button"] = e.get("button") if e.get("button") in _BUTTONS else "left"
            if t == "click":
                out["n"] = min(3, max(1, int(e.get("n") or 1)))
            return out
        if t == "wheel":
            out = {"t": t, "dx": max(-2000.0, min(2000.0, float(e.get("dx") or 0))),
                   "dy": max(-2000.0, min(2000.0, float(e.get("dy") or 0)))}
            if e.get("x") is not None and e.get("y") is not None:
                out.update(x=_unit(e["x"]), y=_unit(e["y"]))
            return out
        if t == "key":
            key = str(e.get("key") or "")
            return {"t": t, "key": key} if _KEY.match(key) else None
        if t == "text":
            text = str(e.get("text") or "")[:MAX_TEXT]
            return {"t": t, "text": text} if text else None
    except (KeyError, TypeError, ValueError):
        return None
    return None


def push_events(hid: int, events: list) -> int:
    """The owner's input into the queue the guard drains. Returns how many were accepted."""
    clean = [c for c in (clean_event(e) for e in (events or [])[:MAX_EVENTS]) if c]
    if not clean:
        return 0
    r = relay(hid)
    with r.cond:
        room = MAX_QUEUE - len(r.events)
        clean = clean[:max(0, room)]
        r.events.extend(clean)
        for c in clean:
            r.counts["text_chars" if c["t"] == "text" else c["t"]] += len(c["text"]) if c["t"] == "text" else 1
        r.cond.notify_all()
    return len(clean)


def take_events(hid: int, wait: float = 0.0) -> list[dict]:
    """The queued input (and empties it); waits up to `wait` seconds for some or a status change."""
    r = relay(hid)
    deadline = time.monotonic() + max(0.0, min(wait, 25.0))
    since = r.changed
    with r.cond:
        while not r.events and r.changed == since:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r.cond.wait(left)
        out, r.events = r.events, []
    return out


def put_frame(hid: int, jpeg: bytes, meta: dict) -> int:
    if not jpeg or len(jpeg) > MAX_FRAME:
        return 0
    r = relay(hid)
    with r.cond:
        r.frame = jpeg
        r.version += 1
        r.meta = {k: (str(v)[:500] if isinstance(v, str) else v) for k, v in (meta or {}).items()
                  if k in ("url", "title", "w", "h")}
        r.cond.notify_all()
        return r.version


def get_frame(hid: int, after: int = 0, wait: float = 0.0) -> tuple[int, bytes, dict] | None:
    """The newest frame when it is newer than `after` (waits up to `wait` s for one); marks the view."""
    r = relay(hid)
    r.viewed = time.monotonic()
    deadline = time.monotonic() + max(0.0, min(wait, 20.0))
    since = r.changed
    with r.cond:
        while (r.frame is None or r.version <= after) and r.changed == since:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r.cond.wait(left)
        if r.frame is None or r.version <= after:
            return None
        return r.version, r.frame, dict(r.meta)


def watching(hid: int) -> bool:
    return time.monotonic() - relay(hid).viewed < WATCH_S


# ------------------------------------------------------------------ the state machine

def get(conn: sqlite3.Connection, hid: int) -> dict:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM browser_handoffs WHERE id = ?", (int(hid),)).fetchone()
    if row is None:
        raise HandoffError("no such handoff", 404)
    d = dict(row)
    d["done_hint"] = json.loads(d.get("done_hint") or "{}")
    return d


def _clean_hint(hint: dict | None) -> dict:
    out = {}
    for k in ("url_contains", "text", "selector"):
        v = str((hint or {}).get(k) or "").strip()
        if v:
            out[k] = v[:300]
    return out


def create(conn: sqlite3.Connection, ctx: Ctx, *, run_id: int, title: str, reason: str = "", url: str | None = None,
           done_hint: dict | None = None, minutes: float | None = None) -> dict:
    """The agent's request (through its guard): its running run waits; the task waits; the owner gets
    the item and a push. One open handoff per run: asking again returns the open one. Commits."""
    ensure_schema(conn)
    run = conn.execute("SELECT id, actor_id, status, task_id FROM runs WHERE id = ?", (int(run_id or 0),)).fetchone()
    if run is None or run["actor_id"] != ctx.actor_id or run["status"] != "running":
        raise HandoffError("not a running run of this agent", 403)
    title = " ".join(str(title or "").split())[:140]
    if not title:
        raise HandoffError("title: what the owner should do, in one line (e.g. 'Přihlas se do LinkedIn – zbytek "
                           "udělám já')", 422)
    open_ = conn.execute(f"SELECT id FROM browser_handoffs WHERE run_id = ? AND status IN {OPEN}",
                         (run["id"],)).fetchone()
    if open_:
        return get(conn, open_["id"])
    mins = max(2.0, min(float(minutes or DEFAULT_MINUTES), MAX_MINUTES))
    now = _now()
    task_id = run["task_id"]
    if task_id:
        # The same task asked before and the owner was not there: continue that handoff (one item for him).
        for row in conn.execute(f"SELECT id FROM browser_handoffs WHERE task_id = ? AND actor_id = ? AND status IN "
                                f"{LISTED} ORDER BY id DESC", (task_id, ctx.actor_id)).fetchall():
            prev = check_expiry(conn, get(conn, row["id"]), now)
            if prev["status"] in PENDING:
                return _reattach(conn, ctx, prev, run, url, done_hint, reason, mins, now)
    before = None
    if task_id:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        before = t["status"] if t else None
    hid = conn.execute(
        """INSERT INTO browser_handoffs (actor_id, task_id, run_id, title, reason, url, done_hint, status, created_at,
               expires_at, task_status_before) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?, ?)""",
        (ctx.actor_id, task_id, run["id"], title, " ".join(str(reason or "").split())[:600],
         _host_path(url), json.dumps(_clean_hint(done_hint)), _iso(now),
         _iso(now + timedelta(minutes=mins)), before)).lastrowid
    if task_id and before not in (None, "done", "waiting"):
        _task(conn, ctx, task_id, "waiting", f"Čeká na majitele v prohlížeči: {title}", action="wait")
    audit.log(conn, Ctx(ctx.actor_id, via="worker", run_id=run["id"]), "handoff_requested", "task" if task_id else None,
              task_id, handoff_id=hid, title=title, url=_host_path(url), minutes=mins)
    conn.commit()
    relay(hid)
    return get(conn, hid)


def _reattach(conn: sqlite3.Connection, ctx: Ctx, h: dict, run, url: str | None, done_hint: dict | None,
              reason: str, mins: float, now: datetime) -> dict:
    """A new run of the agent prepared the page again for a parked (or preparing) handoff: the same handoff goes
    live with this run. The owner is there when it was 'preparing' (he opened it): active at once. Commits."""
    there = h["status"] == "preparing"
    status = "active" if there else "waiting"
    until = now + timedelta(minutes=OWNER_THERE_MINUTES if there else mins)
    hint = _clean_hint(done_hint) or h["done_hint"]
    conn.execute("""UPDATE browser_handoffs SET run_id = ?, status = ?, expires_at = ?, url = COALESCE(?, url),
                    done_hint = ?, reason = CASE WHEN ? != '' THEN ? ELSE reason END, reattached = reattached + 1,
                    finished_at = NULL, finished_by = NULL WHERE id = ?""",
                 (run["id"], status, _iso(until), _host_path(url), json.dumps(hint),
                  " ".join(str(reason or "").split())[:600], " ".join(str(reason or "").split())[:600], h["id"]))
    t = conn.execute("SELECT status FROM tasks WHERE id = ?", (h["task_id"],)).fetchone()
    if t and t["status"] not in ("done", "waiting", "cancelled"):
        if h["task_status_before"] is None or h["task_status_before"] == "waiting":
            conn.execute("UPDATE browser_handoffs SET task_status_before = ? WHERE id = ?", (t["status"], h["id"]))
        _task(conn, ctx, h["task_id"], "waiting", f"Čeká na majitele v prohlížeči: {h['title']}", action="wait")
    audit.log(conn, Ctx(ctx.actor_id, via="worker", run_id=run["id"]), "handoff_reattached", "task", h["task_id"],
              handoff_id=h["id"], owner_there=there, url=_host_path(url))
    conn.commit()
    relay(h["id"])
    _wake(h["id"])
    return get(conn, h["id"])


def _host_path(url: str | None) -> str | None:
    """Where the page was, for the audit: scheme, host and path only (no query: it can carry tokens)."""
    from urllib.parse import urlparse

    if not url:
        return None
    u = urlparse(str(url))
    return f"{u.scheme}://{u.netloc}{u.path}"[:300] if u.netloc else None


def _task(conn: sqlite3.Connection, ctx: Ctx, task_id: int, status: str, note: str, action: str) -> None:
    from . import tasks, versioning

    versioning.update(conn, ctx, tasks.ENTITY, task_id, {"status": status, "progress_note": note[:500]},
                      action=action)


def _require_owner(conn: sqlite3.Connection, ctx: Ctx) -> None:
    row = actors.get(conn, ctx.actor_id)
    if not row["is_owner"]:
        raise HandoffError("only the owner opens a browser handoff", 403)


def open_(conn: sqlite3.Connection, ctx: Ctx, hid: int, *, device: str | None = None, via: str = "web") -> dict:
    """The owner opened the live view: audited every time (who, when, from which app). Commits."""
    _require_owner(conn, ctx)
    h = check_expiry(conn, get(conn, hid))
    if h["status"] == "waiting":
        conn.execute("UPDATE browser_handoffs SET status = 'active', opened_at = ? WHERE id = ? AND status = 'waiting'",
                     (now_iso(), h["id"]))
        _wake(h["id"])
    elif h["status"] == "parked":
        _reprepare(conn, ctx, h)
    audit.log(conn, Ctx(ctx.actor_id, via=via), "handoff_opened", "task" if h["task_id"] else None, h["task_id"],
              handoff_id=h["id"], agent_id=h["actor_id"], device=(device or "")[:40] or None, status=h["status"])
    conn.commit()
    return get(conn, hid)


def _reprepare(conn: sqlite3.Connection, ctx: Ctx, h: dict) -> None:
    """The owner opened a parked handoff: the agent gets the task back, woken now, to prepare the page again and
    ask with the same task (it re-attaches). The caller commits; the wake happens after the commit."""
    from . import wake

    now = _now()
    conn.execute("""UPDATE browser_handoffs SET status = 'preparing', opened_at = ?, expires_at = ? WHERE id = ?
                    AND status = 'parked'""", (_iso(now), _iso(now + timedelta(minutes=PREPARE_MINUTES)), h["id"]))
    if h["task_id"]:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (h["task_id"],)).fetchone()
        if t and t["status"] not in ("done", "cancelled"):
            _task(conn, ctx, h["task_id"], "next",
                  f"Majitel právě otevřel předání #{h['id']} ({h['title']}) a čeká: připrav stránku znovu (otevři ji, "
                  "vyplň, co jde) a hned zavolej browser_request_owner_handoff se stejným názvem; připojí se k tomuto "
                  "předání a majitel uvidí živou stránku.", action="resume")
            conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (h["task_id"],))
    audit.log(conn, Ctx(ctx.actor_id, via=ctx.via), "handoff_reprepare", "task" if h["task_id"] else None,
              h["task_id"], handoff_id=h["id"], agent_id=h["actor_id"])
    conn.commit()
    try:
        wake.wake(h["actor_id"])
    except Exception:  # noqa: BLE001 - the task is in the agent's queue either way; the scheduler picks it up
        pass


def finish(conn: sqlite3.Connection, ctx: Ctx, hid: int, status: str, *, by: str, keep_login: bool = False) -> dict:
    """done (Hotovo, or the agent's done_hint) or cancelled (the owner). The run continues; the task
    goes back to what it was. With keep_login the agent keeps its logins (browser:profile). Commits."""
    if status not in ("done", "cancelled"):
        raise HandoffError("status: done or cancelled", 422)
    h = check_expiry(conn, get(conn, hid))
    if h["status"] in FINAL:
        if h["status"] == status:
            return h
        raise HandoffError(f"the handoff is already {h['status']}", 409)
    live = h["status"] in OPEN
    conn.execute("""UPDATE browser_handoffs SET status = ?, finished_at = ?, finished_by = ?, keep_login = ?,
                    closed_at = CASE WHEN ? THEN closed_at ELSE ? END WHERE id = ?""",
                 (status, now_iso(), by, int(bool(keep_login)), live, now_iso(), h["id"]))
    if h["task_id"]:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (h["task_id"],)).fetchone()
        if live and t and t["status"] == "waiting":
            back = h["task_status_before"] if h["task_status_before"] in ("next", "working") else "working"
            note = ("Majitel dokončil krok v prohlížeči, agent pokračuje." if status == "done"
                    else "Majitel předání v prohlížeči zrušil.")
            _task(conn, ctx, h["task_id"], back, note, action="resume")
        elif not live and t and t["status"] not in ("done", "cancelled"):
            # No run holds it: the task goes back to the agent's queue with what happened.
            note = (f"Majitel krok „{h['title']}“ označil za hotový (udělal ho sám): pokračuj bez předání."
                    if status == "done" else
                    f"Majitel předání zrušil ({h['title']}): nežádej o ně znovu; dokonči bez něj, nebo úkol vrať.")
            _task(conn, ctx, h["task_id"], "next", note, action="resume")
            conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (h["task_id"],))
    granted = False
    if status == "done" and keep_login:
        granted = grant_profile(conn, ctx, h["actor_id"], h["id"])
    counts = dict(relay(h["id"]).counts)
    audit.log(conn, ctx, f"handoff_{status}", "task" if h["task_id"] else None, h["task_id"], handoff_id=h["id"],
              agent_id=h["actor_id"], by=by, keep_login=bool(keep_login), profile_granted=granted, **counts)
    conn.commit()
    _wake(h["id"])
    if not live:
        from . import wake

        try:
            wake.wake(h["actor_id"])
        except Exception:  # noqa: BLE001 - the task is in its queue either way
            pass
    return get(conn, hid)


def grant_profile(conn: sqlite3.Connection, ctx: Ctx, agent_id: int, hid: int) -> bool:
    """The owner's Hotovo with "keep the login": browser:profile for the agent (owner only). The caller commits."""
    from . import browser
    from .access import service as access
    from .access import store

    if browser.may_keep_profile(conn, agent_id) or not actors.get(conn, ctx.actor_id)["is_owner"]:
        return False
    store.ensure_schema(conn)
    if not store.seeded(conn, agent_id):
        access.seed_agent(conn, agent_id, ctx.actor_id)
    access._insert_grant(conn, agent_id, browser.PROFILE_GRANT, ctx.actor_id, "owner",
                         f"Majitel se přihlásil v předání #{hid} a nechal agentovi přihlášení.")
    access.refresh_cache(conn, agent_id)
    return True


def check_expiry(conn: sqlite3.Connection, h: dict, now: datetime | None = None) -> dict:
    """A live handoff past its window, or whose run ended, is parked: the run ends, the task keeps waiting, the
    owner's item stays (opening it wakes the agent). A 'preparing' one the agent did not pick up in time is parked
    again. Commits when it changes."""
    if h["status"] == "preparing":
        now = now or _now()
        if h["expires_at"] > _iso(now):
            return h
        conn.execute("UPDATE browser_handoffs SET status = 'parked' WHERE id = ? AND status = 'preparing'", (h["id"],))
        audit.log(conn, Ctx(h["actor_id"], via="system"), "handoff_parked", "task" if h["task_id"] else None,
                  h["task_id"], handoff_id=h["id"], why="not_prepared")
        conn.commit()
        return get(conn, h["id"])
    if h["status"] not in OPEN:
        return h
    now = now or _now()
    run = conn.execute("SELECT status FROM runs WHERE id = ?", (h["run_id"],)).fetchone() if h["run_id"] else None
    why = None
    if h["expires_at"] <= _iso(now):
        why = "timeout"
    elif run is None or run["status"] != "running":
        why = "run_ended"
    if why is None:
        return h
    conn.execute("UPDATE browser_handoffs SET status = 'parked', parked_at = ?, finished_by = ? WHERE id = ? "
                 f"AND status IN {OPEN}", (_iso(now), why, h["id"]))
    if h["task_id"]:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (h["task_id"],)).fetchone()
        if t and t["status"] in ("waiting", "working", "next"):
            from .business import system_ctx

            _task(conn, system_ctx(conn), h["task_id"], "waiting",
                  f"Čeká na majitele: {h['title']} (až předání otevře, agent stránku připraví znovu).", action="wait")
    audit.log(conn, Ctx(h["actor_id"], via="system", run_id=h["run_id"]), "handoff_parked",
              "task" if h["task_id"] else None, h["task_id"], handoff_id=h["id"], why=why,
              **dict(relay(h["id"]).counts))
    conn.commit()
    _wake(h["id"])
    drop_relay(h["id"])
    return get(conn, h["id"])


def close(conn: sqlite3.Connection, ctx: Ctx, hid: int, *, resume: bool) -> dict:
    """An expired handoff: "Pokračovat" puts the task back in the agent's queue (it prepares the page
    again and asks once more when it needs to); "Zrušit" too, with a note not to ask again. Commits."""
    from . import wake

    _require_owner(conn, ctx)
    h = check_expiry(conn, get(conn, hid))
    if h["status"] in OPEN:
        if resume:
            raise HandoffError("the handoff is still open: open it and press Hotovo", 409)
        return finish(conn, ctx, hid, "cancelled", by="owner")
    if h["status"] in PENDING:
        if not resume:
            return finish(conn, ctx, hid, "cancelled", by="owner")
        if h["status"] == "parked":
            _reprepare(conn, ctx, h)
        return get(conn, hid)
    if h["closed_at"]:
        return h
    conn.execute("UPDATE browser_handoffs SET closed_at = ? WHERE id = ?", (now_iso(), h["id"]))
    if h["status"] == "expired" and h["task_id"]:
        t = conn.execute("SELECT status FROM tasks WHERE id = ?", (h["task_id"],)).fetchone()
        if t and t["status"] == "waiting":
            note = (f"Majitel je připraven: připrav stránku znovu a požádej o předání ({h['title']})." if resume
                    else f"Majitel předání zrušil ({h['title']}): nežádej o ně znovu; dokonči bez něj, nebo úkol vrať.")
            _task(conn, ctx, h["task_id"], "next", note, action="resume")
            conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (h["task_id"],))
    audit.log(conn, ctx, "handoff_resumed" if resume else "handoff_dismissed", "task" if h["task_id"] else None,
              h["task_id"], handoff_id=h["id"], agent_id=h["actor_id"])
    conn.commit()
    wake.wake(h["actor_id"])
    return get(conn, hid)


def sweep(conn: sqlite3.Connection, settings=None, now: datetime | None = None) -> dict:
    """Every minute (pos.scheduler.reap_runs): park what is past its live window, one reminder push for a parked
    one he has not opened, and the fallback notice (once per handoff) when push does not reach him."""
    ensure_schema(conn)
    now = now or _now()
    expired, reminded, notified = [], [], []
    for row in conn.execute(f"SELECT id FROM browser_handoffs WHERE status IN {LISTED}").fetchall():
        h0 = get(conn, row["id"])
        h = check_expiry(conn, h0, now)
        if h0["status"] in OPEN and h["status"] == "parked":
            expired.append(h["id"])  # its live window passed: parked (the key keeps its name for the scheduler log)
        due = datetime.fromisoformat(h["created_at"]) + timedelta(minutes=REMIND_AFTER_MIN)
        if h["status"] == "parked" and not h["opened_at"] and not h["reminded_at"] and now >= due:
            conn.execute("UPDATE browser_handoffs SET reminded_at = ? WHERE id = ?", (_iso(now), h["id"]))
            conn.commit()
            reminded.append(h["id"])
            _remind(conn, settings, h, now)
        if h["status"] in LISTED and not h.get("notified_at"):
            via = _notify_fallback(conn, h, now)
            if via:
                notified.append({"id": h["id"], "via": via})
    # A finished handoff's relay (its last frame) is dropped once the guard has seen the end.
    cutoff = _iso(now - timedelta(minutes=2))
    for hid in list(_relays):
        row = conn.execute("SELECT status, finished_at FROM browser_handoffs WHERE id = ?", (hid,)).fetchone()
        if row is None or (row["status"] in FINAL and (row["finished_at"] or "") < cutoff):
            drop_relay(hid)
    return {"expired": expired, "reminded": reminded, **({"notified": notified} if notified else {})}


def _notify_fallback(conn: sqlite3.Connection, h: dict, now: datetime) -> str | None:
    """At most once per handoff: push reaches the owner (the "Čeká na tebe" push covers it), or one e-mail to his
    work mailbox (pos.owner_fallback). Marked before the network call, so a crash never sends it twice."""
    from . import owner_fallback

    if owner_fallback.push_reaches_owner(conn):
        conn.execute("UPDATE browser_handoffs SET notified_at = ?, notified_via = 'push' WHERE id = ?",
                     (_iso(now), h["id"]))
        conn.commit()
        return None
    conn.execute("UPDATE browser_handoffs SET notified_at = ?, notified_via = 'email…' WHERE id = ? "
                 "AND notified_at IS NULL", (_iso(now), h["id"]))
    conn.commit()
    agent = actors.get(conn, h["actor_id"])["name"]
    from . import tasks

    ref = f" ({tasks.display_id(h['task_id'])})" if h["task_id"] else ""
    link = owner_fallback.public_url(f"/m/handoff/{h['id']}")
    text = (f"{agent} potřebuje jeden tvůj krok v prohlížeči{ref}:\n\n{h['title']}\n"
            + (f"\n{h['reason']}\n" if h["reason"] else "")
            + f"\nOtevři: {link}\n\nAgent stránku připraví, ty se jen přihlásíš nebo klikneš. Položka zůstává v "
              "„Čeká na tebe“, dokud ji nedokončíš nebo nezrušíš.\n\n"
              "Tento e-mail dostáváš, protože v telefonu nemáš zapnuté notifikace PersonalOS. Zapneš je v aplikaci: "
            + owner_fallback.public_url("/m/settings") + "\n")
    ok, detail = owner_fallback.email_owner(f"Čeká na tebe: {h['title']}", text)
    conn.execute("UPDATE browser_handoffs SET notified_via = ? WHERE id = ?",
                 ("email" if ok else f"failed: {detail}"[:200], h["id"]))
    audit.log(conn, Ctx(h["actor_id"], via="system"), "handoff_notified" if ok else "handoff_notify_failed",
              "task" if h["task_id"] else None, h["task_id"], handoff_id=h["id"], via="email",
              **({} if ok else {"error": detail}))
    conn.commit()
    return "email" if ok else None


def _remind(conn: sqlite3.Connection, settings, h: dict, now: datetime) -> None:
    from . import push

    try:
        if settings is None:
            from .config import get_settings

            settings = get_settings()
        agent = actors.get(conn, h["actor_id"])["name"]
        push.send(conn, settings, actors.owner_id(conn), {
            "title": f"Připomínka: {h['title']}",
            "body": f"{agent} potřebuje tvůj krok v prohlížeči. Otevři ho a agent stránku hned připraví.",
            "tag": f"handoff-{h['id']}", "url": f"/m/handoff/{h['id']}", "kind": "needs"})
        conn.commit()
    except Exception:  # noqa: BLE001 - a reminder is a courtesy; the item is in the list either way
        conn.rollback()


# ------------------------------------------------------------------ what the guard and the owner see

def worker_poll(conn: sqlite3.Connection, ctx: Ctx, hid: int, wait: float) -> dict:
    """The guard's loop: the owner's input since the last poll, whether he watches, and the status."""
    from . import browser

    h = get(conn, hid)
    if h["actor_id"] != ctx.actor_id:
        raise HandoffError("not this agent's handoff", 404)
    h = check_expiry(conn, h)
    events = take_events(h["id"], wait) if h["status"] in OPEN else []
    if events:  # the status may have changed while it waited
        h = get(conn, hid)
    elif h["status"] in OPEN:
        h = check_expiry(conn, get(conn, hid))
    return {"status": h["status"], "events": events, "watching": watching(h["id"]),
            "opened": bool(h["opened_at"]), "finished_by": h["finished_by"], "keep_login": bool(h["keep_login"]),
            "profile": browser.may_keep_profile(conn, h["actor_id"]), "expires_at": h["expires_at"]}


def view(conn: sqlite3.Connection, h: dict) -> dict:
    """The owner's view of one handoff (never the frames' content: those come from /frame)."""
    from . import tasks

    agent = actors.get(conn, h["actor_id"])
    r = relay(h["id"])
    return {"id": h["id"], "status": h["status"], "title": h["title"], "reason": h["reason"], "url": h["url"],
            "agent": {"id": agent["id"], "name": agent["name"]},
            "task_ref": tasks.display_id(h["task_id"]) if h["task_id"] else None,
            "created_at": h["created_at"], "expires_at": h["expires_at"], "opened_at": h["opened_at"],
            "finished_at": h["finished_at"], "finished_by": h["finished_by"], "closed": bool(h["closed_at"]),
            "preparing": h["status"] == "preparing", "parked": h["status"] == "parked",
            "done_hint": h["done_hint"], "frame_version": r.version, "page": dict(r.meta)}


def needs_items(conn: sqlite3.Connection) -> list[dict]:
    """"Čeká na tebe" items (owner): every handoff until it is done or cancelled (live, parked or being prepared
    again). Opening one wakes the agent when it is parked: there are no dead "expired" items."""
    from . import tasks

    ensure_schema(conn)
    rows = conn.execute(
        f"""SELECT h.*, a.name AS agent_name, a.kind AS agent_kind FROM browser_handoffs h
            JOIN actors a ON a.id = h.actor_id
            WHERE h.status IN {LISTED} ORDER BY h.id DESC LIMIT 20""").fetchall()
    out = []
    for r in rows:
        out.append({
            "kind": "handoff", "key": f"handoff:{r['id']}", "id": r["id"], "ref": None,
            "title": r["title"], "detail": r["reason"] or "", "status": r["status"], "expired": False,
            "parked": r["status"] in PENDING, "expires_at": r["expires_at"], "blocking": True,
            "task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None,
            "from_name": r["agent_name"], "from_kind": r["agent_kind"], "at": r["created_at"],
            "link": f"/handoff/{r['id']}", "m_link": f"/m/handoff/{r['id']}",
        })
    return out
