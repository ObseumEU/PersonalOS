"""Topics (PLAN 3, item 3): one place per area of life or work.

A topic is a slug ("acme", "health", "house") that tasks, files and notes carry
in their `topic` column. The `topics` table adds a name, a description and a
colour; a slug that only appears on tasks, files or notes is still a topic
(`id` is None until someone describes it). Topic rows are versioned and
archived, never deleted. Everything a topic shows is filtered by the caller's
visibility layer.
"""

import sqlite3
from datetime import timedelta

from . import files, notes, tasks, versioning
from .content import norm_topic, words
from .core import Ctx, NotFound, now_iso, today
from .tasks import Invalid
from .visibility import visible_sql

ENTITY = "topic"
EDITABLE = {"name", "description", "color"}


def _slug(value: str) -> str:
    slug = norm_topic(value)
    if not slug:
        raise Invalid("a topic needs a name")
    return slug


def _clean(fields: dict) -> dict:
    unknown = set(fields) - EDITABLE - {"slug"}
    if unknown:
        raise Invalid(f"unknown fields: {sorted(unknown)}")
    out = {k: v for k, v in fields.items() if k in EDITABLE}
    if "name" in out:
        out["name"] = str(out["name"] or "").strip()[:80]
        if not out["name"]:
            raise Invalid("a topic needs a name")
    if "description" in out:
        out["description"] = str(out["description"] or "")[:2000]
    if out.get("color") and not str(out["color"]).startswith("#"):
        raise Invalid("color is a hex value like #6cc4dc")
    return out


def _counts(conn: sqlite3.Connection, ctx: Ctx) -> dict[str, dict]:
    """Per topic slug: open, done and upcoming tasks, files and notes."""
    out: dict[str, dict] = {}

    def bump(slug, key, n):
        c = out.setdefault(slug, {"open_tasks": 0, "done_tasks": 0, "upcoming": 0, "file_count": 0, "note_count": 0})
        c[key] += n

    vis, vp = visible_sql("task", ctx.actor_id)
    t = today().isoformat()
    for r in conn.execute(
        f"""SELECT topic, SUM(status != 'done') AS open, SUM(status = 'done') AS done,
                   SUM(status != 'done' AND (do_date >= ? OR deadline >= ?)) AS upcoming
            FROM tasks WHERE topic IS NOT NULL AND archived_at IS NULL AND {vis} GROUP BY topic""", [t, t, *vp]
    ):
        bump(r["topic"], "open_tasks", r["open"] or 0)
        bump(r["topic"], "done_tasks", r["done"] or 0)
        bump(r["topic"], "upcoming", r["upcoming"] or 0)
    for entity, table in (("file", "files"), ("note", "notes")):
        vis, vp = visible_sql(entity, ctx.actor_id)
        for r in conn.execute(
            f"SELECT topic, COUNT(*) AS n FROM {table} WHERE topic IS NOT NULL AND archived_at IS NULL AND {vis} "
            "GROUP BY topic", vp
        ):
            bump(r["topic"], f"{entity}_count", r["n"])
    return out


def _describe(row: sqlite3.Row | dict | None, slug: str) -> dict:
    if row is None:
        return {"id": None, "slug": slug, "name": slug, "description": "", "color": None,
                "created_at": None, "updated_at": None, "archived_at": None}
    return dict(row)


def list_topics(conn: sqlite3.Connection, ctx: Ctx) -> list[dict]:
    counts = _counts(conn, ctx)
    rows = {r["slug"]: r for r in conn.execute("SELECT * FROM topics")}
    out = []
    for slug in sorted(set(counts) | set(rows)):
        row = rows.get(slug)
        if row is not None and row["archived_at"]:
            continue
        c = counts.get(slug, {"open_tasks": 0, "done_tasks": 0, "upcoming": 0, "file_count": 0, "note_count": 0})
        out.append({**_describe(row, slug), **c})
    out.sort(key=lambda t: (-(t["open_tasks"] + t["file_count"] + t["note_count"]), t["slug"]))
    return out


def _events(slug: str, name: str) -> list[dict]:
    """Calendar events of the next 30 days whose title or location mentions the
    topic. Optional: empty when no calendar is connected or a feed fails."""
    try:
        from . import agenda
    except ImportError:
        return []
    if not agenda.feeds():
        return []
    needles = {slug.lower(), name.lower(), slug.replace("-", " ").lower()}
    start = today()
    out = []
    for cal, url in agenda.feeds():
        try:
            events = agenda.parse(agenda._fetch(url), cal, start, start + timedelta(days=30))
        except Exception:  # noqa: BLE001 - one broken feed must not hide the topic
            continue
        for e in events:
            text = f"{e.get('title') or ''} {e.get('location') or ''}".lower()
            if any(n and n in text for n in needles):
                out.append(e)
    return sorted(out, key=lambda e: e["start"])


def get(conn: sqlite3.Connection, ctx: Ctx, slug: str, *, with_events: bool = True) -> dict:
    """Everything in one topic: files, notes, open and done tasks, events."""
    slug = _slug(slug)
    row = conn.execute("SELECT * FROM topics WHERE slug = ?", (slug,)).fetchone()
    counts = _counts(conn, ctx).get(slug)
    if row is None and counts is None:
        raise NotFound(f"topic {slug}")
    topic = _describe(row, slug)
    vis, vp = visible_sql("task", ctx.actor_id)
    open_rows = conn.execute(
        f"""SELECT * FROM tasks WHERE topic = ? AND archived_at IS NULL AND status != 'done' AND {vis}
            ORDER BY COALESCE(priority, 4), COALESCE(do_date, deadline, '9999'), id""", [slug, *vp]
    ).fetchall()
    done_rows = conn.execute(
        f"""SELECT * FROM tasks WHERE topic = ? AND archived_at IS NULL AND status = 'done' AND {vis}
            ORDER BY completed_at DESC LIMIT 30""", [slug, *vp]
    ).fetchall()
    return {
        **topic,
        **(counts or {"open_tasks": 0, "done_tasks": 0, "upcoming": 0, "file_count": 0, "note_count": 0}),
        "files": files.list_files(conn, ctx, topic=slug),
        "notes": notes.list_notes(conn, ctx, topic=slug),
        "open": [tasks.to_dict(r) for r in open_rows],
        "done": [tasks.to_dict(r) for r in done_rows],
        "events": _events(slug, topic["name"]) if with_events else [],
    }


# ------------------------------------------------------------------ writes

def create(conn: sqlite3.Connection, ctx: Ctx, fields: dict) -> dict:
    fields = dict(fields)
    slug = _slug(fields.pop("slug", None) or fields.get("name") or "")
    values = _clean({"name": fields.get("name") or slug, **fields})
    if conn.execute("SELECT 1 FROM topics WHERE slug = ?", (slug,)).fetchone():
        raise Invalid(f"topic {slug} already exists")
    now = now_iso()
    versioning.insert(conn, ctx, ENTITY, {"description": "", **values, "slug": slug, "created_by": ctx.actor_id,
                                          "created_at": now, "updated_at": now})
    return get(conn, ctx, slug, with_events=False)


def _row_or_create(conn: sqlite3.Connection, ctx: Ctx, slug: str) -> sqlite3.Row:
    """The topic row; a topic known only from tasks, files or notes gets one."""
    row = conn.execute("SELECT * FROM topics WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        if _counts(conn, ctx).get(slug) is None:
            raise NotFound(f"topic {slug}")
        create(conn, ctx, {"slug": slug, "name": slug})
        row = conn.execute("SELECT * FROM topics WHERE slug = ?", (slug,)).fetchone()
    return row


def update(conn: sqlite3.Connection, ctx: Ctx, slug: str, changes: dict) -> dict:
    slug = _slug(slug)
    row = _row_or_create(conn, ctx, slug)
    versioning.update(conn, ctx, ENTITY, row["id"], _clean(changes))
    return get(conn, ctx, slug, with_events=False)


def archive(conn: sqlite3.Connection, ctx: Ctx, slug: str) -> dict:
    """Hide a topic. Its tasks, files and notes stay as they are."""
    slug = _slug(slug)
    row = _row_or_create(conn, ctx, slug)
    versioning.archive(conn, ctx, ENTITY, row["id"])
    return dict(conn.execute("SELECT * FROM topics WHERE id = ?", (row["id"],)).fetchone())


def unarchive(conn: sqlite3.Connection, ctx: Ctx, slug: str) -> dict:
    slug = _slug(slug)
    row = conn.execute("SELECT * FROM topics WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise NotFound(f"topic {slug}")
    versioning.unarchive(conn, ctx, ENTITY, row["id"])
    return get(conn, ctx, slug, with_events=False)


# ------------------------------------------------------------------ search

def search(conn: sqlite3.Connection, ctx: Ctx, q: str, limit: int = 20) -> dict:
    """Tasks, files and notes matching the text, as the caller may see them."""
    ws = words(q)
    if not ws:
        return {"q": q, "tasks": [], "files": [], "notes": []}
    vis, vp = visible_sql("task", ctx.actor_id)
    conds, params = [], []
    for w in ws:
        conds.append("(title LIKE ? OR notes LIKE ? OR topic LIKE ?)")
        params += [f"%{w}%"] * 3
    rows = conn.execute(
        f"""SELECT * FROM tasks WHERE archived_at IS NULL AND {' AND '.join(conds)} AND {vis}
            ORDER BY status = 'done', updated_at DESC LIMIT ?""", [*params, *vp, limit]
    ).fetchall()
    return {
        "q": q,
        "tasks": [tasks.to_dict(r) for r in rows],
        "files": files.list_files(conn, ctx, q=q, limit=limit),
        "notes": notes.list_notes(conn, ctx, q=q, limit=limit),
    }
