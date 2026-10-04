"""Goals: what the company is trying to reach, and how far it is.

A goal has a title, why it matters, a measurable target, an owner (a person
or an agent), a due date, a status (active, paused, done, dropped), progress
0-100 and an optional parent goal. Tasks, topics and projects link to it.

A goal that steers has a number: `metric` (what is counted), `baseline` (where
it started), `current` (where it is now, with `current_at`) and `target_value`
(where it must get by `due`); `target` stays the sentence a person reads.
Progress is then (current - baseline) / (target_value - baseline).

Who sets them: the CEO sets and updates company goals itself (active at once);
a goal another agent proposes is `proposed` until the CEO (or the owner)
confirms it (status active). The owner may veto any goal (`veto`: dropped, with
his note); nobody needs his answer for a goal to steer (prod 2026-10: the goals
table was empty, both weekly meetings closed without him).

Progress is what someone set (`progress`); without it, from the numbers above;
without those, the share of linked tasks that are done. The weekly report (pos.weekly) shows every active
goal with its progress and the change since the last report; the Chief of
Staff proposes goals and links next week's tasks to them.

The tables are created on first use (no numbered migration, so this cannot
collide with a migration added elsewhere). Nothing is deleted: dropping a goal
is a status, archiving sets archived_at.
"""

import sqlite3
from datetime import date

from . import actors, audit
from .core import Ctx, NotFound, now_iso

STATUSES = ("proposed", "active", "paused", "done", "dropped")
LINK_KINDS = ("task", "topic", "project")
EDITABLE = ("title", "why", "target", "owner", "due", "status", "progress", "parent_id",
            "metric", "baseline", "current", "target_value")
NUMBERS = ("baseline", "current", "target_value")
# Added after the table: ALTERed in on first use.
_COLUMNS = {"metric": "TEXT", "baseline": "REAL", "current": "REAL", "target_value": "REAL", "current_at": "TEXT",
            "confirmed_by": "INTEGER", "confirmed_at": "TEXT", "veto_note": "TEXT"}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS goals (
        id          INTEGER PRIMARY KEY,
        title       TEXT NOT NULL,
        why         TEXT NOT NULL DEFAULT '',
        target      TEXT NOT NULL DEFAULT '',
        owner_id    INTEGER REFERENCES actors(id),
        due         TEXT,
        status      TEXT NOT NULL DEFAULT 'active',
        progress    INTEGER,
        parent_id   INTEGER REFERENCES goals(id),
        created_by  INTEGER REFERENCES actors(id),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        archived_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS goal_links (
        goal_id    INTEGER NOT NULL REFERENCES goals(id),
        kind       TEXT NOT NULL,
        ref        TEXT NOT NULL,
        created_by INTEGER REFERENCES actors(id),
        created_at TEXT NOT NULL,
        PRIMARY KEY (goal_id, kind, ref)
    )""",
    "CREATE INDEX IF NOT EXISTS goal_links_ref ON goal_links (kind, ref)",
)


class Invalid(ValueError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in _SCHEMA:
        conn.execute(sql)
    have = {r[1] for r in conn.execute("PRAGMA table_info(goals)")}
    for col, typ in _COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE goals ADD COLUMN {col} {typ}")


def _sets_goals(conn: sqlite3.Connection, ctx: Ctx) -> bool:
    """The CEO and the owner set company goals; others propose them."""
    me = actors.get(conn, ctx.actor_id)
    return bool(me["is_owner"]) or me["role"] == "ceo"


def _owner(conn: sqlite3.Connection, ctx: Ctx, value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        return actors.get(conn, int(value))["id"]
    v = str(value).strip().lstrip("@")
    if v.lower() in ("me", "já", "ja", "owner"):
        me = actors.get(conn, ctx.actor_id)
        return me["id"] if me["kind"] == "human" and v.lower() != "owner" else actors.owner_id(conn)
    row = actors.find_by_name(conn, v)
    if row is None:
        raise Invalid(f"no member called {v!r}: a goal's owner is a person or an agent in PersonalOS")
    return row["id"]


def _validate(fields: dict) -> None:
    if "title" in fields and not str(fields["title"] or "").strip():
        raise Invalid("a goal needs a title")
    if fields.get("status") is not None and fields["status"] not in STATUSES:
        raise Invalid(f"status must be one of {STATUSES}")
    if fields.get("progress") is not None:
        try:
            p = int(fields["progress"])
        except (TypeError, ValueError) as e:
            raise Invalid("progress is a number 0-100") from e
        if not 0 <= p <= 100:
            raise Invalid("progress is a number 0-100")
        fields["progress"] = p
    if fields.get("due"):
        try:
            date.fromisoformat(fields["due"])
        except ValueError as e:
            raise Invalid("due must be YYYY-MM-DD") from e
    for k in NUMBERS:
        if fields.get(k) not in (None, ""):
            try:
                fields[k] = float(fields[k])
            except (TypeError, ValueError) as e:
                raise Invalid(f"{k} is a number") from e
        elif k in fields:
            fields[k] = None


def _task_stats(conn: sqlite3.Connection, goal_id: int) -> dict:
    ids = [int(r["ref"]) for r in conn.execute(
        "SELECT ref FROM goal_links WHERE goal_id = ? AND kind = 'task'", (goal_id,)) if str(r["ref"]).isdigit()]
    if not ids:
        return {"total": 0, "done": 0, "open": 0}
    marks = ",".join("?" for _ in ids)
    row = conn.execute(
        f"""SELECT COUNT(*) AS total, COALESCE(SUM(status = 'done'), 0) AS done FROM tasks
            WHERE id IN ({marks}) AND archived_at IS NULL""", ids).fetchone()
    return {"total": row["total"], "done": row["done"], "open": row["total"] - row["done"]}


def to_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    d = dict(row)
    owner = actors.get(conn, d["owner_id"]) if d.get("owner_id") else None
    d["owner_name"] = owner["name"] if owner else None
    d["owner_kind"] = owner["kind"] if owner else None
    links = conn.execute("SELECT kind, ref FROM goal_links WHERE goal_id = ? ORDER BY kind, ref",
                         (d["id"],)).fetchall()
    d["links"] = [{"kind": r["kind"], "ref": (f"T-{int(r['ref']):03d}" if r["kind"] == "task" else r["ref"])}
                  for r in links]
    d["tasks"] = _task_stats(conn, d["id"])
    measured = _measured(d)
    if d["progress"] is not None:
        d["progress_effective"] = d["progress"]
    elif measured is not None:
        d["progress_effective"] = measured
    elif d["tasks"]["total"]:
        d["progress_effective"] = round(100 * d["tasks"]["done"] / d["tasks"]["total"])
    else:
        d["progress_effective"] = 0
    d["parent_title"] = None
    if d.get("parent_id"):
        p = conn.execute("SELECT title FROM goals WHERE id = ?", (d["parent_id"],)).fetchone()
        d["parent_title"] = p["title"] if p else None
    return d


def _measured(d: dict) -> int | None:
    """Progress from the numbers: how far current got from baseline towards target_value."""
    b, c, t = d.get("baseline"), d.get("current"), d.get("target_value")
    if c is None or t is None:
        return None
    b = 0.0 if b is None else b
    if t == b:
        return 100 if c == t else 0
    return max(0, min(100, round(100 * (c - b) / (t - b))))


def get(conn: sqlite3.Connection, goal_id: int) -> dict:
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM goals WHERE id = ?", (int(goal_id),)).fetchone()
    if row is None:
        raise NotFound(f"no goal {goal_id}")
    return to_dict(conn, row)


def list_goals(conn: sqlite3.Connection, status: str | None = "active", *,
               include_archived: bool = False) -> list[dict]:
    """Goals by status ('all' or None for every status), top-level first, then by due date."""
    ensure_schema(conn)
    sql, args = "SELECT * FROM goals WHERE 1 = 1", []
    if not include_archived:
        sql += " AND archived_at IS NULL"
    if status and status != "all":
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY parent_id IS NOT NULL, COALESCE(due, '9999'), id"
    return [to_dict(conn, r) for r in conn.execute(sql, args)]


def create(conn: sqlite3.Connection, ctx: Ctx, fields: dict) -> dict:
    ensure_schema(conn)
    fields = {k: v for k, v in fields.items() if v is not None}
    unknown = set(fields) - set(EDITABLE) - {"links"}
    if unknown:
        raise Invalid(f"unknown goal fields: {sorted(unknown)}; use {list(EDITABLE)}")
    # The CEO (or the owner) sets a company goal; anyone else's goal is a proposal the CEO confirms.
    if not _sets_goals(conn, ctx):
        fields["status"] = "proposed"
    fields.setdefault("status", "active")
    _validate(fields)
    title = str(fields.get("title") or "").strip()
    if not title:
        raise Invalid("a goal needs a title")
    parent = fields.get("parent_id")
    if parent not in (None, ""):
        get(conn, int(parent))
    now = now_iso()
    confirmed = fields["status"] != "proposed" and _sets_goals(conn, ctx)
    cur = conn.execute(
        """INSERT INTO goals (title, why, target, owner_id, due, status, progress, parent_id, created_by,
                              created_at, updated_at, metric, baseline, current, target_value, current_at,
                              confirmed_by, confirmed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (title[:200], str(fields.get("why") or "").strip(), str(fields.get("target") or "").strip(),
         _owner(conn, ctx, fields.get("owner")), fields.get("due") or None, fields["status"],
         fields.get("progress"), int(parent) if parent not in (None, "") else None, ctx.actor_id, now, now,
         str(fields.get("metric") or "").strip() or None, fields.get("baseline"), fields.get("current"),
         fields.get("target_value"), now if fields.get("current") is not None else None,
         ctx.actor_id if confirmed else None, now if confirmed else None))
    gid = cur.lastrowid
    audit.log(conn, ctx, "goal_create", "goal", gid, title=title, status=fields["status"])
    if fields["status"] == "proposed":  # the CEO confirms it (or drops it)
        try:
            from . import notices

            ceo = notices.ceo(conn)
            if ceo and ceo != ctx.actor_id:
                notices.dm(conn, ceo, f"{actors.get(conn, ctx.actor_id)['name']} navrhuje firemní cíl #{gid} "
                                      f"„{title[:160]}“. Potvrď ho (goal_upsert goal_id={gid} status=active), "
                                      "uprav, nebo zahoď (status=dropped).")
        except Exception:  # noqa: BLE001 - the proposal stands either way
            pass
    for link in fields.get("links") or []:
        link_to(conn, ctx, gid, link)
    return get(conn, gid)


def update(conn: sqlite3.Connection, ctx: Ctx, goal_id: int, changes: dict) -> dict:
    before = get(conn, goal_id)
    unknown = set(changes) - set(EDITABLE) - {"links"}
    if unknown:
        raise Invalid(f"unknown goal fields: {sorted(unknown)}; use {list(EDITABLE)}")
    changes = dict(changes)
    _validate(changes)
    sets: dict = {}
    for k in ("title", "why", "target", "due", "status", "progress", "metric", *NUMBERS):
        if k in changes:
            sets[k] = changes[k].strip() if isinstance(changes[k], str) else changes[k]
    if "current" in sets:
        sets["current_at"] = now_iso()
    if sets.get("status") and sets["status"] != before["status"] and before["status"] == "proposed":
        if sets["status"] == "active" and not _sets_goals(conn, ctx):
            raise Invalid("a proposed goal becomes active when the CEO (or the owner) confirms it")
        if sets["status"] == "active":
            sets.update(confirmed_by=ctx.actor_id, confirmed_at=now_iso())
    if "due" in sets and not sets["due"]:
        sets["due"] = None
    if "owner" in changes:
        sets["owner_id"] = _owner(conn, ctx, changes["owner"])
    if "parent_id" in changes:
        p = changes["parent_id"]
        if p not in (None, ""):
            if int(p) == before["id"]:
                raise Invalid("a goal is not its own parent")
            get(conn, int(p))
        sets["parent_id"] = int(p) if p not in (None, "") else None
    if sets:
        sets["updated_at"] = now_iso()
        conn.execute(f"UPDATE goals SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     [*sets.values(), before["id"]])
        audit.log(conn, ctx, "goal_update", "goal", before["id"],
                  changes={k: v for k, v in sets.items() if k != "updated_at"},
                  before={k: before.get(k) for k in sets if k in before and k != "updated_at"})
    for link in changes.get("links") or []:
        link_to(conn, ctx, before["id"], link)
    return get(conn, before["id"])


def veto(conn: sqlite3.Connection, ctx: Ctx, goal_id: int, note: str = "") -> dict:
    """The owner stops a goal (the CEO's or a proposal): dropped, with his reason. The CEO hears it."""
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise Invalid("only the owner vetoes a goal")
    g = get(conn, goal_id)
    now = now_iso()
    conn.execute("UPDATE goals SET status = 'dropped', veto_note = ?, updated_at = ? WHERE id = ?",
                 ((note or "").strip() or "veto", now, g["id"]))
    audit.log(conn, ctx, "goal_veto", "goal", g["id"], note=(note or "")[:300])
    try:
        from . import notices

        notices.dm(conn, notices.ceo(conn), f"Owner vetoval cíl „{g['title']}“" + (f": {note.strip()}" if
                                                                                  (note or "").strip() else ".")
                   + " Uprav plán a cíle bez něj (goal_upsert).")
    except Exception:  # noqa: BLE001 - the veto stands either way
        pass
    return get(conn, g["id"])


def archive(conn: sqlite3.Connection, ctx: Ctx, goal_id: int) -> dict:
    g = get(conn, goal_id)
    now = now_iso()
    conn.execute("UPDATE goals SET archived_at = ?, updated_at = ? WHERE id = ?", (now, now, g["id"]))
    audit.log(conn, ctx, "goal_archive", "goal", g["id"])
    return get(conn, g["id"])


def _parse_link(link) -> tuple[str, str]:
    """'T-12' / 12 -> task; {'kind': 'topic', 'ref': 'acme'}; 'topic:acme'; 'project:web'."""
    from . import tasks

    if isinstance(link, dict):
        kind, ref = str(link.get("kind") or "task"), str(link.get("ref") or link.get("id") or "").strip()
    elif isinstance(link, int):
        kind, ref = "task", str(link)
    else:
        s = str(link).strip()
        if ":" in s and s.split(":", 1)[0] in LINK_KINDS:
            kind, ref = s.split(":", 1)
        else:
            kind, ref = "task", s
    if kind not in LINK_KINDS:
        raise Invalid(f"a link is a {', '.join(LINK_KINDS)}")
    if kind == "task":
        try:
            ref = str(tasks.parse_id(ref))
        except tasks.Invalid as e:
            raise Invalid(str(e)) from e
    else:
        ref = ref.lower().lstrip("#").strip()
    if not ref:
        raise Invalid("a link needs a reference")
    return kind, ref


def link_to(conn: sqlite3.Connection, ctx: Ctx, goal_id: int, link) -> dict:
    """Link a task (T-12), a topic ('topic:acme') or a project ('project:web') to a goal."""
    ensure_schema(conn)
    get(conn, goal_id)
    kind, ref = _parse_link(link)
    if kind == "task":
        from . import tasks

        tasks.get(conn, ctx, int(ref))  # exists and the caller may see it
    conn.execute("INSERT OR IGNORE INTO goal_links (goal_id, kind, ref, created_by, created_at) "
                 "VALUES (?, ?, ?, ?, ?)", (int(goal_id), kind, ref, ctx.actor_id, now_iso()))
    conn.execute("UPDATE goals SET updated_at = ? WHERE id = ?", (now_iso(), int(goal_id)))
    audit.log(conn, ctx, "goal_link", "goal", int(goal_id), kind=kind, ref=ref)
    return {"goal_id": int(goal_id), "kind": kind, "ref": ref}


def unlink(conn: sqlite3.Connection, ctx: Ctx, goal_id: int, link) -> None:
    ensure_schema(conn)
    kind, ref = _parse_link(link)
    conn.execute("DELETE FROM goal_links WHERE goal_id = ? AND kind = ? AND ref = ?", (int(goal_id), kind, ref))
    audit.log(conn, ctx, "goal_unlink", "goal", int(goal_id), kind=kind, ref=ref)


def brief(g: dict) -> dict:
    """What the weekly packet keeps of a goal."""
    return {"id": g["id"], "title": g["title"], "target": g["target"], "owner": g["owner_name"], "due": g["due"],
            "status": g["status"], "progress": g["progress_effective"], "parent_id": g["parent_id"],
            "tasks_done": g["tasks"]["done"], "tasks_total": g["tasks"]["total"], "metric": g.get("metric"),
            "baseline": g.get("baseline"), "current": g.get("current"), "target_value": g.get("target_value")}
