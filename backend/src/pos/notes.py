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
    from .visibility import check_write

    check_write(conn, ENTITY, row, ctx.actor_id)
    clean = _clean(changes)
    if "visibility" in clean and clean["visibility"] != row["visibility"]:
        from .integrations import check_visibility_change

        check_visibility_change(conn, ctx, row, clean["visibility"])
    versioning.update(conn, ctx, ENTITY, note_id, clean)
    return get(conn, ctx, note_id)


# ------------------------------------------------------------------ for agents: full reads, edits in place

PAGE = 20_000  # characters per note_get page; a longer note is read in pages, never cut off silently
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)


def outline(body: str) -> list[dict]:
    """The note's headings: [{level, title, at}] (at = character offset)."""
    return [{"level": len(m.group(1)), "title": m.group(2).strip(), "at": m.start()} for m in _HEADING.finditer(body)]


def read(conn: sqlite3.Connection, ctx: Ctx, note_id: int, offset: int = 0, limit: int = PAGE) -> dict:
    """The note with its body from `offset`, at most `limit` characters, and where the next page starts.
    With the outline, an agent sees the whole structure even when the body takes several pages."""
    n = get(conn, ctx, note_id)
    body = n.pop("body") or ""
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or PAGE), 100_000))
    part = body[offset:offset + limit]
    end = offset + len(part)
    return {**n, "body": part, "offset": offset, "next_offset": end if end < len(body) else None,
            "total_chars": len(body), "complete": offset == 0 and end >= len(body), "outline": outline(body)}


def _section_span(body: str, section: str) -> tuple[int, int, int] | None:
    """(heading start, content start, end) of the section whose heading matches `section`
    (case-insensitive, with or without the #s). It runs to the next heading of the same or a higher level."""
    want = section.strip().lstrip("#").strip().lower()
    heads = outline(body)
    for i, h in enumerate(heads):
        if h["title"].lower() == want:
            nl = body.find("\n", h["at"])
            content = nl + 1 if nl >= 0 else len(body)
            end = next((x["at"] for x in heads[i + 1:] if x["level"] <= h["level"]), len(body))
            return h["at"], content, end
    return None


MODES = ("replace", "append", "section", "patch")


def edit(conn: sqlite3.Connection, ctx: Ctx, note_id: int, mode: str, *, body: str | None = None,
         section: str | None = None, find: str | None = None, replace: str | None = None) -> dict:
    """Change the note's body in place:
    - replace: the whole body becomes `body`;
    - append: `body` is added at the end;
    - section: the content under the heading `section` becomes `body` (the heading stays); a missing
      section is added at the end as "## <section>";
    - patch: the exact text `find` (it must occur exactly once) becomes `replace`.
    """
    if mode not in MODES:
        raise Invalid(f"mode must be one of {MODES}")
    old = _row(conn, ctx, note_id)["body"] or ""
    if mode == "replace":
        if body is None:
            raise Invalid("replace needs body")
        new = body
    elif mode == "append":
        if not body:
            raise Invalid("append needs body")
        new = old.rstrip("\n") + "\n\n" + body.strip("\n") + "\n"
    elif mode == "section":
        if not section or body is None:
            raise Invalid("section mode needs section (the heading) and body (its new content)")
        span = _section_span(old, section)
        if span is None:
            title = section.strip().lstrip("#").strip()
            new = old.rstrip("\n") + f"\n\n## {title}\n\n" + body.strip("\n") + "\n"
        else:
            _, start, end = span
            tail = old[end:]
            new = old[:start] + "\n" + body.strip("\n") + "\n" + ("\n" + tail.lstrip("\n") if tail.strip() else "")
    else:
        if not find:
            raise Invalid("patch needs find (the exact text to change) and replace")
        n = old.count(find)
        if n != 1:
            raise Invalid(f"find must occur exactly once in the note; it occurs {n} times "
                          "(read it with note_get and quote more of the text)")
        new = old.replace(find, replace or "", 1)
    return update(conn, ctx, note_id, {"body": new})


def archive(conn: sqlite3.Connection, ctx: Ctx, note_id: int) -> dict:
    from .visibility import check_write

    check_write(conn, ENTITY, _row(conn, ctx, note_id), ctx.actor_id)
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
