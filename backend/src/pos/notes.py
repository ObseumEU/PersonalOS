"""Notes (PLAN 3, item 6): markdown documents that belong to topics.

Every write is versioned and audited, reads follow the visibility layers, and
notes are archived, never deleted.
"""

import json
import re
import sqlite3

from . import actors, versioning
from .content import check_visibility, match_sql, norm_tags, norm_topic, tags_json, words
from .core import Ctx, NotFound, now_iso
from .tasks import Invalid
from .visibility import DEFAULT, check_read, visible_sql

ENTITY = "note"
EDITABLE = {"title", "body", "topic", "tags", "visibility"}
MAX_BODY = 1_000_000


def to_dict(row: sqlite3.Row | dict, *, with_body: bool = True) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    if not with_body:
        body = d.pop("body", "") or ""
        # Plain text for list rows: links keep their label, markdown marks go.
        plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", body)
        plain = re.sub(r"^\s*[-*+]\s+\[[ xX]\]\s+|[#*_`>]+|^\s*[-*+]\s+", " ", plain, flags=re.M)
        d["excerpt"] = " ".join(plain.split())[:160]
    return d


def _row(conn: sqlite3.Connection, ctx: Ctx, note_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise NotFound(f"note {note_id}")
    check_read(conn, ENTITY, row, ctx.actor_id)
    return row


def _clean(fields: dict) -> dict:
    unknown = set(fields) - EDITABLE
    if unknown:
        raise Invalid(f"unknown fields: {sorted(unknown)}")
    out = dict(fields)
    if "title" in out:
        out["title"] = str(out["title"] or "").strip()[:200]
        if not out["title"]:
            raise Invalid("title is empty")
    if "body" in out:
        out["body"] = str(out["body"] or "")
        if len(out["body"]) > MAX_BODY:
            raise Invalid("the note is too long (1 MB at most)")
    if "topic" in out:
        out["topic"] = norm_topic(out["topic"])
    if "tags" in out:
        out["tags"] = tags_json(norm_tags(out["tags"]))
    if "visibility" in out:
        check_visibility(out["visibility"])
    return out


def get(conn: sqlite3.Connection, ctx: Ctx, note_id: int) -> dict:
    return to_dict(_row(conn, ctx, note_id))


def list_notes(conn: sqlite3.Connection, ctx: Ctx, *, topic: str | None = None, tag: str | None = None,
               q: str | None = None, archived: bool = False, limit: int = 200) -> list[dict]:
    vis, params = visible_sql(ENTITY, ctx.actor_id, "notes")
    sql = f"SELECT notes.* FROM notes WHERE {vis} AND notes.archived_at IS {'NOT ' if archived else ''}NULL"
    if topic:
        sql += " AND notes.topic = ?"
        params.append(norm_topic(topic))
    if tag:
        sql += " AND EXISTS (SELECT 1 FROM json_each(notes.tags) WHERE value = ?)"
        params.append((norm_tags([tag]) or [""])[0])
    if q and words(q):
        cond, qp = match_sql(conn, "notes", "notes_fts", ("title", "body", "tags"), q)
        sql += f" AND {cond}"
        params += qp
    rows = conn.execute(f"{sql} ORDER BY notes.updated_at DESC, notes.id DESC LIMIT ?", [*params, limit]).fetchall()
    return [to_dict(r, with_body=False) for r in rows]


def create(conn: sqlite3.Connection, ctx: Ctx, fields: dict) -> dict:
    values = _clean({"body": "", "visibility": DEFAULT, **fields})
    if not values.get("title"):
        raise Invalid("title is empty")
    me = actors.get(conn, ctx.actor_id)
    # Agents write notes on behalf of the owner, as with tasks.
    owner = ctx.actor_id if me["kind"] == "human" else actors.owner_id(conn)
    now = now_iso()
    row = versioning.insert(conn, ctx, ENTITY, {
        **values, "owner_id": owner, "created_by": ctx.actor_id, "created_at": now, "updated_at": now,
    })
    return get(conn, ctx, row["id"])


def update(conn: sqlite3.Connection, ctx: Ctx, note_id: int, changes: dict) -> dict:
    row = _row(conn, ctx, note_id)
    clean = _clean(changes)
    if "visibility" in clean and clean["visibility"] != row["visibility"]:
        from .integrations import check_visibility_change

        check_visibility_change(conn, ctx, row, clean["visibility"])
    versioning.update(conn, ctx, ENTITY, note_id, clean)
    return get(conn, ctx, note_id)


def archive(conn: sqlite3.Connection, ctx: Ctx, note_id: int) -> dict:
    _row(conn, ctx, note_id)
    versioning.archive(conn, ctx, ENTITY, note_id)
    return get(conn, ctx, note_id)


def unarchive(conn: sqlite3.Connection, ctx: Ctx, note_id: int) -> dict:
    _row(conn, ctx, note_id)
    versioning.unarchive(conn, ctx, ENTITY, note_id)
    return get(conn, ctx, note_id)


def history(conn: sqlite3.Connection, ctx: Ctx, note_id: int) -> list[dict]:
    _row(conn, ctx, note_id)
    return versioning.history(conn, ENTITY, note_id)


def restore(conn: sqlite3.Connection, ctx: Ctx, note_id: int, version: int) -> dict:
    row = _row(conn, ctx, note_id)
    old = next((h["data"] for h in versioning.history(conn, ENTITY, note_id) if h["version"] == version), None)
    if old and old.get("visibility") != row["visibility"]:
        from .integrations import check_visibility_change

        check_visibility_change(conn, ctx, row, old["visibility"])
    versioning.restore(conn, ctx, ENTITY, note_id, version)
    return get(conn, ctx, note_id)
