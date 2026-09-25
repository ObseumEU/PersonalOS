"""Files and documents (PLAN 3, item 2): upload, browse, preview, tag, search.

The bytes live on disk under Settings.files_dir as
`<yyyy>/<mm>/<sha256 prefix>-<safe name>`; the metadata and a text extract
live in the `files` table. Identical content is stored once. Search goes
through knowlage (pos.kb_files): every file is pushed there after upload, and
only when knowlage cannot answer does search fall back to file names.
Like tasks, every write is versioned and audited, reads follow the visibility
layers, and files are archived, never deleted (the bytes stay on disk).

Nothing the client says about a file is trusted: the name is sanitised, the
type is sniffed from the bytes, and paths are checked to stay in files_dir.
"""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from typing import BinaryIO

from . import actors, versioning
from .content import check_visibility, norm_tags, norm_topic, tags_json, words
from .core import Ctx, NotFound, now_iso
from .tasks import Invalid
from .visibility import DEFAULT, check_read, visible_sql

ENTITY = "file"
EDITABLE = {"name", "topic", "tags", "visibility"}
CHUNK = 1024 * 1024
MAX_EXTRACT = 200_000  # characters of text kept for search
INLINE_IMAGES = ("image/png", "image/jpeg", "image/gif", "image/webp")
TEXT_EXT = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv",
            ".json": "application/json", ".log": "text/plain", ".tsv": "text/tab-separated-values"}
OFFICE_EXT = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class TooLarge(Invalid):
    pass


# ------------------------------------------------------------------ names and types

def safe_name(name: str | None) -> str:
    """A file name that is safe to store and to send back: no directories, no
    control characters, no leading dots, at most 120 characters."""
    base = re.split(r"[\\/]", unicodedata.normalize("NFKC", name or ""))[-1]
    base = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C")
    base = re.sub(r"[^\w.\- ()+,]", "_", base).strip(" .")
    base = re.sub(r"_+", "_", base)
    if not base or set(base) <= {"_", "."}:
        base = "file"
    stem, ext = os.path.splitext(base)
    if len(base) > 120:
        base = stem[: 120 - len(ext[:20])] + ext[:20]
    return base


def sniff_mime(head: bytes, name: str) -> str:
    """The type from the first bytes; the extension only picks between
    formats the bytes cannot tell apart (text flavours, zip-based office files)."""
    ext = os.path.splitext(name)[1].lower()
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return OFFICE_EXT.get(ext, "application/zip")
    if _looks_like_text(head):
        return TEXT_EXT.get(ext, "text/plain")
    return "application/octet-stream"


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError as e:
        # A multi-byte character cut off at the end of the sample is fine.
        return e.start >= len(head) - 3


def is_text(mime: str | None) -> bool:
    return bool(mime) and (mime.startswith("text/") or mime == "application/json")


def extract_text(path: Path, mime: str) -> str:
    """Text for search (pushed into knowlage): text files directly, PDFs through pypdf
    when it is installed (optional), nothing for other types."""
    try:
        if is_text(mime):
            with path.open("rb") as f:
                return f.read(MAX_EXTRACT * 4).decode("utf-8", errors="replace")[:MAX_EXTRACT]
        if mime == "application/pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""
            parts, size = [], 0
            for page in PdfReader(str(path)).pages[:200]:
                t = page.extract_text() or ""
                parts.append(t)
                size += len(t)
                if size > MAX_EXTRACT:
                    break
            return "\n".join(parts)[:MAX_EXTRACT]
    except Exception:  # noqa: BLE001 - a broken document must not block the upload
        return ""
    return ""


# ------------------------------------------------------------------ rows

def to_dict(row: sqlite3.Row | dict, *, with_text: bool = False) -> dict:
    d = dict(row)
    d["tags"] = json.loads(d.get("tags") or "[]")
    text = d.pop("text_extract", "") or ""
    d["text_chars"] = len(text)
    if with_text:
        d["text_extract"] = text
    d.pop("path", None)  # a server detail; the API serves the bytes by id
    d["preview"] = preview_kind(d.get("mime"))
    return d


def preview_kind(mime: str | None) -> str:
    if mime in INLINE_IMAGES:
        return "image"
    if mime == "application/pdf":
        return "pdf"
    if is_text(mime):
        return "text"
    return "download"


def _row(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise NotFound(f"file {file_id}")
    check_read(conn, ENTITY, row, ctx.actor_id)
    return row


def get(conn: sqlite3.Connection, ctx: Ctx, file_id: int, *, with_text: bool = False) -> dict:
    return to_dict(_row(conn, ctx, file_id), with_text=with_text)


def _filtered(ctx: Ctx, topic: str | None, tag: str | None, archived: bool) -> tuple[str, list]:
    vis, params = visible_sql(ENTITY, ctx.actor_id, "files")
    sql = f"SELECT files.* FROM files WHERE {vis} AND files.archived_at IS {'NOT ' if archived else ''}NULL"
    if topic:
        sql += " AND files.topic = ?"
        params.append(norm_topic(topic))
    if tag:
        sql += " AND EXISTS (SELECT 1 FROM json_each(files.tags) WHERE value = ?)"
        params.append((norm_tags([tag]) or [""])[0])
    return sql, params


def list_files(conn: sqlite3.Connection, ctx: Ctx, *, topic: str | None = None, tag: str | None = None,
               q: str | None = None, archived: bool = False, limit: int = 200) -> list[dict]:
    if q and words(q):
        return search(conn, ctx, q, topic=topic, tag=tag, archived=archived, limit=limit)["files"]
    sql, params = _filtered(ctx, topic, tag, archived)
    rows = conn.execute(f"{sql} ORDER BY files.created_at DESC, files.id DESC LIMIT ?", [*params, limit]).fetchall()
    return [to_dict(r) for r in rows]


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(conn: sqlite3.Connection, ctx: Ctx, q: str, *, topic: str | None = None, tag: str | None = None,
           archived: bool = False, limit: int = 200) -> dict:
    """Files matching `q`, as the caller may see them: {mode, files, error?}.

    mode "knowlage": knowlage searched the files' content (pos.kb_files) and its
    hits are mapped back to local files by document id, best first.
    mode "filename": knowlage is not configured or did not answer, so only file
    names were matched (every word, LIKE)."""
    from . import kb_files

    ws = words(q)
    if not ws:
        return {"mode": "none", "files": list_files(conn, ctx, topic=topic, tag=tag, archived=archived, limit=limit)}
    sql, params = _filtered(ctx, topic, tag, archived)
    try:
        ids = kb_files.search(q, limit)
    except kb_files.Unavailable as e:
        cond = " AND ".join("files.name LIKE ? ESCAPE '\\'" for _ in ws)
        rows = conn.execute(f"{sql} AND {cond} ORDER BY files.created_at DESC, files.id DESC LIMIT ?",
                            [*params, *map(_like, ws), limit]).fetchall()
        return {"mode": "filename", "files": [to_dict(r) for r in rows], "error": str(e)[:300]}
    if not ids:
        return {"mode": "knowlage", "files": []}
    rank = {kid: i for i, kid in enumerate(ids)}
    rows = conn.execute(f"{sql} AND files.kb_doc_id IN ({','.join('?' * len(ids))})", [*params, *ids]).fetchall()
    rows = sorted(rows, key=lambda r: rank[r["kb_doc_id"]])[:limit]
    return {"mode": "knowlage", "files": [to_dict(r) for r in rows]}


def tags(conn: sqlite3.Connection, ctx: Ctx) -> list[dict]:
    vis, params = visible_sql(ENTITY, ctx.actor_id, "files")
    rows = conn.execute(
        f"""SELECT j.value AS tag, COUNT(*) AS n FROM files, json_each(files.tags) j
            WHERE files.archived_at IS NULL AND {vis} GROUP BY j.value ORDER BY n DESC, j.value""", params
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ disk

def resolve(files_dir: Path, rel: str) -> Path:
    """The absolute path of a stored file, refusing anything outside files_dir."""
    root = files_dir.resolve()
    p = (root / rel).resolve()
    if root not in p.parents:
        raise NotFound("file content")
    return p


def _existing_path(conn: sqlite3.Connection, files_dir: Path, sha: str) -> str | None:
    for r in conn.execute("SELECT path FROM files WHERE sha256 = ?", (sha,)):
        try:
            if resolve(files_dir, r["path"]).is_file():
                return r["path"]
        except NotFound:
            continue
    return None


# ------------------------------------------------------------------ writes

def upload(conn: sqlite3.Connection, ctx: Ctx, files_dir: Path, stream: BinaryIO, filename: str | None, *,
           topic: str | None = None, tags=None, visibility: str | None = None,
           max_bytes: int = 200 * 1024 * 1024) -> dict:
    """Store an upload. The same content twice gives back the first file (the
    result says `duplicate`); on disk each content is kept once."""
    name = safe_name(filename)
    topic_v = norm_topic(topic)
    tag_list = norm_tags(tags)
    vis = visibility or DEFAULT
    check_visibility(vis)
    files_dir.mkdir(parents=True, exist_ok=True)
    digest, size, head = hashlib.sha256(), 0, b""
    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", dir=files_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise TooLarge(f"file is larger than {max_bytes // (1024 * 1024)} MB")
                if len(head) < 4096:
                    head += chunk[: 4096 - len(head)]
                digest.update(chunk)
                out.write(chunk)
        sha = digest.hexdigest()
        mime = sniff_mime(head, name)

        vis_sql, vparams = visible_sql(ENTITY, ctx.actor_id)
        dup = conn.execute(f"SELECT * FROM files WHERE sha256 = ? AND {vis_sql} ORDER BY id LIMIT 1",
                           [sha, *vparams]).fetchone()
        if dup is not None:
            if dup["archived_at"]:
                versioning.unarchive(conn, ctx, ENTITY, dup["id"])
            return {**get(conn, ctx, dup["id"]), "duplicate": True}

        rel = _existing_path(conn, files_dir, sha)
        if rel is None:
            now = now_iso()
            rel = f"{now[:4]}/{now[5:7]}/{sha[:12]}-{name}"
            dest = resolve(files_dir, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                tmp.unlink()
            else:
                os.replace(tmp, dest)
        else:
            tmp.unlink()
    finally:
        if tmp.exists():
            tmp.unlink()

    me = actors.get(conn, ctx.actor_id)
    # Agents file things on behalf of the owner, as with tasks.
    owner = ctx.actor_id if me["kind"] == "human" else actors.owner_id(conn)
    now = now_iso()
    row = versioning.insert(conn, ctx, ENTITY, {
        "name": name, "path": rel, "mime": mime, "size": size, "sha256": sha, "topic": topic_v,
        "tags": tags_json(tag_list), "visibility": vis, "owner_id": owner, "created_by": ctx.actor_id,
        "text_extract": extract_text(resolve(files_dir, rel), mime), "created_at": now, "updated_at": now,
        "kb_status": "pending",  # pushed into knowlage once the upload is saved (pos.kb_files)
    })
    return {**get(conn, ctx, row["id"]), "duplicate": False}


def update(conn: sqlite3.Connection, ctx: Ctx, file_id: int, changes: dict) -> dict:
    row = _row(conn, ctx, file_id)
    unknown = set(changes) - EDITABLE
    if unknown:
        raise Invalid(f"unknown fields: {sorted(unknown)}")
    clean: dict = {}
    if "name" in changes:
        clean["name"] = safe_name(changes["name"])
    if "topic" in changes:
        clean["topic"] = norm_topic(changes["topic"])
    if "tags" in changes:
        clean["tags"] = tags_json(norm_tags(changes["tags"]))
    if "visibility" in changes:
        check_visibility(changes["visibility"])
        if changes["visibility"] != row["visibility"]:
            from .integrations import check_visibility_change

            check_visibility_change(conn, ctx, row, changes["visibility"])
        clean["visibility"] = changes["visibility"]
    versioning.update(conn, ctx, ENTITY, file_id, clean)
    if {"name", "topic", "tags"} & set(clean):
        from . import kb_files

        kb_files.mark_changed(conn, file_id)  # the retry job pushes the new name and labels
    return get(conn, ctx, file_id)


def archive(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> dict:
    _row(conn, ctx, file_id)
    versioning.archive(conn, ctx, ENTITY, file_id)
    return get(conn, ctx, file_id)


def unarchive(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> dict:
    _row(conn, ctx, file_id)
    versioning.unarchive(conn, ctx, ENTITY, file_id)
    return get(conn, ctx, file_id)


def history(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> list[dict]:
    _row(conn, ctx, file_id)
    out = versioning.history(conn, ENTITY, file_id)
    for h in out:
        h["data"].pop("text_extract", None)
        h["data"].pop("path", None)
    return out


def content(conn: sqlite3.Connection, ctx: Ctx, files_dir: Path, file_id: int) -> tuple[Path, dict]:
    """Where the bytes are, for serving. Checks visibility and the path."""
    row = _row(conn, ctx, file_id)
    path = resolve(files_dir, row["path"])
    if not path.is_file():
        raise NotFound(f"file {file_id} content is missing on disk")
    return path, to_dict(row)
