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
EDITABLE = {"name", "topic", "tags", "visibility", "description"}
CHUNK = 1024 * 1024
MAX_EXTRACT = 200_000  # characters of text kept for search
INLINE_IMAGES = ("image/png", "image/jpeg", "image/gif", "image/webp")
TEXT_EXT = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv",
            ".json": "application/json", ".log": "text/plain", ".tsv": "text/tab-separated-values",
            # visuals (pos.file_render): diagrams and pages rendered for the reader, never served as HTML
            ".mmd": "text/vnd.mermaid", ".mermaid": "text/vnd.mermaid", ".dot": "text/vnd.graphviz",
            ".gv": "text/vnd.graphviz", ".html": "text/html", ".htm": "text/html"}
VEGALITE = "application/vnd.vegalite+json"
SVG = "image/svg+xml"
# Source code and config show as highlighted text (the language comes from the extension).
CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".yaml", ".yml", ".toml", ".ini", ".conf", ".sql",
            ".xml", ".css", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".rb", ".php", ".ps1", ".env.example",
            ".dockerfile", ".tf", ".nginx", ".diff", ".patch"}
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
        from .file_render import looks_like_svg

        if looks_like_svg(head.decode("utf-8", errors="replace")):
            return SVG
        if name.lower().endswith((".vl.json", ".vegalite.json")):
            return VEGALITE
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
    return bool(mime) and (mime.startswith("text/") or mime == "application/json" or mime.endswith("+json")
                           or mime == SVG)


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
    d["preview"] = preview_kind(d.get("mime"), d.get("name"))
    return d


# How the web app shows a file (docs/FILES.md, "Visuals").
PREVIEWS = {SVG: "svg", "application/pdf": "pdf", "text/markdown": "markdown", "text/csv": "csv",
            "text/tab-separated-values": "csv", "text/vnd.mermaid": "mermaid", "text/vnd.graphviz": "dot",
            VEGALITE: "vegalite", "text/html": "html"}


def preview_kind(mime: str | None, name: str | None = None) -> str:
    """image | svg | pdf | markdown | csv | mermaid | dot | vegalite | html | code | text | download."""
    if mime in INLINE_IMAGES:
        return "image"
    if mime in PREVIEWS:
        return PREVIEWS[mime]
    if is_text(mime):
        ext = os.path.splitext((name or "").lower())[1]
        return "code" if ext in CODE_EXT or mime == "application/json" else "text"
    return "download"


def version_of(conn: sqlite3.Connection, file_ids: list[int]) -> dict[int, int]:
    """The current version number of each file (its newest history entry)."""
    if not file_ids:
        return {}
    rows = conn.execute(f"SELECT entity_id, MAX(version) FROM history WHERE entity = 'file' AND entity_id IN "
                        f"({','.join('?' * len(file_ids))}) GROUP BY entity_id", file_ids)
    return {r[0]: r[1] for r in rows}


def _with_versions(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    v = version_of(conn, [d["id"] for d in items])
    for d in items:
        d["version"] = v.get(d["id"], 1)
    return items


def _row(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise NotFound(f"file {file_id}")
    check_read(conn, ENTITY, row, ctx.actor_id)
    return row


def get(conn: sqlite3.Connection, ctx: Ctx, file_id: int, *, with_text: bool = False) -> dict:
    return _with_versions(conn, [to_dict(_row(conn, ctx, file_id), with_text=with_text)])[0]


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
    return _with_versions(conn, [to_dict(r) for r in rows])


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
    rows = []
    if ids:
        rank = {kid: i for i, kid in enumerate(ids)}
        rows = conn.execute(f"{sql} AND files.kb_doc_id IN ({','.join('?' * len(ids))})", [*params, *ids]).fetchall()
        rows = sorted(rows, key=lambda r: rank[r["kb_doc_id"]])
    # Private files stay out of the shared knowledge base (pos.kb_files): their names are matched here.
    cond = " AND ".join("files.name LIKE ? ESCAPE '\\'" for _ in ws)
    seen = {r["id"] for r in rows}
    rows += [r for r in conn.execute(f"{sql} AND files.visibility = 'private' AND {cond} ORDER BY files.id DESC LIMIT ?",
                                     [*params, *map(_like, ws), limit]).fetchall() if r["id"] not in seen]
    return {"mode": "knowlage", "files": _with_versions(conn, [to_dict(r) for r in rows[:limit]])}


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

def _store(conn: sqlite3.Connection, files_dir: Path, stream: BinaryIO, name: str,
           max_bytes: int) -> tuple[str, str, int, str]:
    """Write the bytes into files_dir (each content once): (relative path, sha256, size, mime).
    SVG is sanitised on the way in (pos.file_render), so what is stored is what is served."""
    files_dir.mkdir(parents=True, exist_ok=True)
    digest, size, head = hashlib.sha256(), 0, b""
    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", dir=files_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise TooLarge(f"file is larger than {_mb(max_bytes)}")
                if len(head) < 4096:
                    head += chunk[: 4096 - len(head)]
                digest.update(chunk)
                out.write(chunk)
        mime = sniff_mime(head, name)
        if mime == SVG:
            from .file_render import RenderError, sanitize_svg

            try:
                clean = sanitize_svg(tmp.read_bytes())
            except RenderError as e:
                raise Invalid(str(e)) from e
            tmp.write_bytes(clean)
            size, digest = len(clean), hashlib.sha256(clean)
        sha = digest.hexdigest()
        rel = _existing_path(conn, files_dir, sha)
        if rel is None:
            now = now_iso()
            rel = f"{now[:4]}/{now[5:7]}/{sha[:12]}-{name}"
            dest = resolve(files_dir, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                os.replace(tmp, dest)
        return rel, sha, size, mime
    finally:
        if tmp.exists():
            tmp.unlink()


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):g} MB"


def upload(conn: sqlite3.Connection, ctx: Ctx, files_dir: Path, stream: BinaryIO, filename: str | None, *,
           topic: str | None = None, tags=None, visibility: str | None = None, description: str | None = None,
           max_bytes: int = 200 * 1024 * 1024, dedupe: bool = True, origin: str | None = None) -> dict:
    """Store an upload. The same content twice gives back the first file (the result says
    `duplicate`) unless `dedupe` is off; on disk each content is kept once."""
    name = safe_name(filename)
    topic_v = norm_topic(topic)
    tag_list = norm_tags(tags)
    vis = visibility or DEFAULT
    check_visibility(vis)
    rel, sha, size, mime = _store(conn, files_dir, stream, name, max_bytes)

    if dedupe:
        vis_sql, vparams = visible_sql(ENTITY, ctx.actor_id)
        dup = conn.execute(f"SELECT * FROM files WHERE sha256 = ? AND {vis_sql} ORDER BY id LIMIT 1",
                           [sha, *vparams]).fetchone()
        if dup is not None:
            if dup["archived_at"]:
                versioning.unarchive(conn, ctx, ENTITY, dup["id"])
            return {**get(conn, ctx, dup["id"]), "duplicate": True}

    me = actors.get(conn, ctx.actor_id)
    # Agents file things on behalf of the owner, as with tasks.
    owner = ctx.actor_id if me["kind"] == "human" else actors.owner_id(conn)
    now = now_iso()
    row = versioning.insert(conn, ctx, ENTITY, {
        "name": name, "path": rel, "mime": mime, "size": size, "sha256": sha, "topic": topic_v,
        "tags": tags_json(tag_list), "visibility": vis, "owner_id": owner, "created_by": ctx.actor_id,
        "description": (description or "").strip()[:500], "origin": origin,
        "text_extract": extract_text(resolve(files_dir, rel), mime), "created_at": now, "updated_at": now,
        "kb_status": "pending",  # pushed into knowlage once the upload is saved (pos.kb_files)
    })
    if vis == "private" and me["kind"] != "human":
        from .visibility import share

        share(conn, ENTITY, row["id"], ctx.actor_id)  # the agent keeps seeing what it made for the owner
    return {**get(conn, ctx, row["id"]), "duplicate": False}


def update_content(conn: sqlite3.Connection, ctx: Ctx, files_dir: Path, file_id: int, stream: BinaryIO, *,
                   name: str | None = None, description: str | None = None,
                   max_bytes: int = 200 * 1024 * 1024) -> dict:
    """New content for a file: a new version (the earlier bytes stay on disk, so every version
    can be viewed and restored). Who may change the file decides (visibility.check_write)."""
    from .visibility import check_write

    row = _row(conn, ctx, file_id)
    check_write(conn, ENTITY, row, ctx.actor_id)
    new_name = safe_name(name) if name else row["name"]
    rel, sha, size, mime = _store(conn, files_dir, stream, new_name, max_bytes)
    changes = {"name": new_name, "path": rel, "sha256": sha, "size": size, "mime": mime,
               "text_extract": extract_text(resolve(files_dir, rel), mime)}
    if description is not None:
        changes["description"] = description.strip()[:500]
    if row["archived_at"]:
        changes["archived_at"] = None
    versioning.update(conn, ctx, ENTITY, file_id, changes, action="update:content")
    from . import kb_files

    kb_files.mark_changed(conn, file_id)
    return get(conn, ctx, file_id)


def update(conn: sqlite3.Connection, ctx: Ctx, file_id: int, changes: dict) -> dict:
    row = _row(conn, ctx, file_id)
    from .visibility import check_write

    check_write(conn, ENTITY, row, ctx.actor_id)
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
    if "description" in changes:
        clean["description"] = str(changes["description"] or "").strip()[:500]
    if "visibility" in changes:
        check_visibility(changes["visibility"])
        if changes["visibility"] != row["visibility"]:
            from .integrations import check_visibility_change

            check_visibility_change(conn, ctx, row, changes["visibility"])
        clean["visibility"] = changes["visibility"]
    versioning.update(conn, ctx, ENTITY, file_id, clean)
    if {"name", "topic", "tags", "description", "visibility"} & set(clean):
        from . import kb_files

        kb_files.mark_changed(conn, file_id)  # the retry job pushes the new name and labels
    return get(conn, ctx, file_id)


def archive(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> dict:
    from .visibility import check_write

    check_write(conn, ENTITY, _row(conn, ctx, file_id), ctx.actor_id)
    versioning.archive(conn, ctx, ENTITY, file_id)
    return get(conn, ctx, file_id)


def unarchive(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> dict:
    _row(conn, ctx, file_id)
    versioning.unarchive(conn, ctx, ENTITY, file_id)
    return get(conn, ctx, file_id)


def restore_version(conn: sqlite3.Connection, ctx: Ctx, file_id: int, version: int) -> dict:
    """Put the file's name, topic, tags and visibility back to an earlier version."""
    from .visibility import check_write

    check_write(conn, ENTITY, _row(conn, ctx, file_id), ctx.actor_id)
    versioning.restore(conn, ctx, ENTITY, file_id, version)
    from . import kb_files

    kb_files.mark_changed(conn, file_id)
    return get(conn, ctx, file_id)


def history(conn: sqlite3.Connection, ctx: Ctx, file_id: int) -> list[dict]:
    _row(conn, ctx, file_id)
    out = versioning.history(conn, ENTITY, file_id)
    for h in out:
        h["data"].pop("text_extract", None)
        h["data"].pop("path", None)
    return out


def content(conn: sqlite3.Connection, ctx: Ctx, files_dir: Path, file_id: int,
            version: int | None = None) -> tuple[Path, dict]:
    """Where the bytes are, for serving: the current content, or that of an earlier version
    (a chat message shows the version that was shared). Checks visibility and the path."""
    row = _row(conn, ctx, file_id)
    data = dict(row)
    if version is not None:
        snap = conn.execute("SELECT data FROM history WHERE entity = 'file' AND entity_id = ? AND version = ?",
                            (file_id, version)).fetchone()
        if snap is None:
            raise NotFound(f"file {file_id} v{version}")
        old = json.loads(snap["data"])
        data.update({k: old[k] for k in ("path", "mime", "name", "sha256", "size") if old.get(k)})
    path = resolve(files_dir, data["path"])
    if not path.is_file():
        raise NotFound(f"file {file_id} content is missing on disk")
    meta = to_dict(data)
    meta["sha256"] = data.get("sha256")
    return path, meta


def read_text(conn: sqlite3.Connection, ctx: Ctx, files_dir: Path, file_id: int, version: int | None = None,
              limit: int = 200_000) -> tuple[str, dict]:
    """A text file's content (any version), for an agent editing it."""
    path, meta = content(conn, ctx, files_dir, file_id, version)
    if not is_text(meta.get("mime")):
        raise Invalid(f"file {file_id} is {meta.get('mime')}, not text: file_get gives its extracted text")
    with path.open("rb") as f:
        return f.read(limit * 4).decode("utf-8", errors="replace")[:limit], meta


def attachment(f: dict, version: int | None = None) -> dict:
    """A file as a chat message attachment: what the card shows without asking again."""
    out = {"type": "file", "id": f["id"], "name": f["name"], "mime": f.get("mime"), "size": f.get("size"),
           "preview": f.get("preview") or preview_kind(f.get("mime"), f.get("name")),
           "version": version or f.get("version")}
    if f.get("description"):
        out["description"] = f["description"]
    return out


def chat_attachments(conn: sqlite3.Connection, ctx: Ctx, ids: list[int]) -> tuple[list[dict], list[str]]:
    """Files the sender may read, as message attachments, and a line per file for the text
    (agents read the text: they see which file to open)."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    if len(ids) > 10:
        raise Invalid("at most 10 attachments")
    out, lines = [], []
    for fid in ids:
        f = get(conn, ctx, fid)  # NotFound / Forbidden when the sender cannot read it
        out.append(attachment(f))
        lines.append(f"📎 {f['name']} (soubor #{f['id']})")
    return out, lines
