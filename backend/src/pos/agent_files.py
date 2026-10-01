"""Files agents make, edit and share with people (docs/FILES.md, "Agents' files and visuals").

An agent writes a file (file_create), changes it (file_update: every change is a new version,
viewable and restorable in Files) and shares it (file_share): a chat message, a DM to the owner
by default, with the file attached, which the web app and the phone app render inline (images,
SVG, Mermaid, Graphviz DOT, Vega-Lite charts, HTML in a sandboxed frame, Markdown, CSV, PDF, code).
Results from an agent's sandbox (pos.sandbox) arrive the same way (sandbox_share).

Limits: one file at most Settings.agent_file_max_mb, and one agent keeps at most
Settings.agent_files_quota_mb in the current versions of its files (archived ones do not count).
Names are sanitised (pos.files.safe_name: no directories, no traversal), the type is sniffed
from the bytes, SVG is sanitised, and the visuals are checked at once, so a broken diagram
comes back to the agent as `render_error` while it can still fix it.
"""

import base64 as b64
import binascii
import io
import os
import re
import sqlite3
from pathlib import Path

from . import actors, files
from .config import Settings
from .core import Ctx, NotFound
from .tasks import Invalid

# A name without an extension gets one from the type the agent says it wrote.
MIME_EXT = {
    "text/vnd.mermaid": ".mmd", "text/x-mermaid": ".mmd", "mermaid": ".mmd",
    "text/vnd.graphviz": ".dot", "text/x-dot": ".dot", "dot": ".dot", "graphviz": ".dot",
    "application/vnd.vegalite+json": ".vl.json", "vega-lite": ".vl.json", "vegalite": ".vl.json",
    "text/html": ".html", "html": ".html", "text/markdown": ".md", "markdown": ".md", "text/csv": ".csv",
    "csv": ".csv", "image/svg+xml": ".svg", "svg": ".svg", "image/png": ".png", "image/jpeg": ".jpg",
    "image/webp": ".webp", "image/gif": ".gif", "application/pdf": ".pdf", "application/json": ".json",
    "text/plain": ".txt",
}


def _with_ext(name: str, mime: str | None) -> str:
    stem, ext = os.path.splitext(name)
    if ext or not mime:
        return name
    return name + MIME_EXT.get(mime.strip().lower(), "")


def decode(content: str | None, encoding: str = "text") -> bytes:
    if content is None:
        raise Invalid("content is required")
    if encoding == "base64":
        try:
            return b64.b64decode(content, validate=False)
        except (binascii.Error, ValueError) as e:
            raise Invalid(f"content is not valid base64: {e}") from e
    if encoding != "text":
        raise Invalid("encoding is text or base64")
    return content.encode("utf-8")


def _check_size(settings: Settings, conn: sqlite3.Connection, ctx: Ctx, size: int, replacing: int = 0) -> None:
    me = actors.get(conn, ctx.actor_id)
    limit = settings.agent_file_max_mb * 1024 * 1024
    if me["kind"] == "human":
        return
    if size > limit:
        raise files.TooLarge(f"a file from an agent is at most {settings.agent_file_max_mb} MB "
                             f"(this one is {size / 1048576:.1f} MB)")
    used = conn.execute("SELECT COALESCE(SUM(size), 0) FROM files WHERE created_by = ? AND archived_at IS NULL",
                        (ctx.actor_id,)).fetchone()[0]
    quota = settings.agent_files_quota_mb * 1024 * 1024
    if used - replacing + size > quota:
        raise files.TooLarge(f"your files would take {(used - replacing + size) / 1048576:.0f} MB, over your "
                             f"{settings.agent_files_quota_mb} MB quota: archive files you no longer need")


def check_visual(files_dir: Path, f: dict, data: bytes) -> str | None:
    """Why the file will not render (a line the agent can act on), or None."""
    from . import file_render as fr

    kind = f.get("preview")
    try:
        if kind == "dot":
            if not fr.dot_available():
                return None
            fr.render_dot_cached(files_dir, f["sha256"], files.resolve(files_dir, f["path"]))
        elif kind == "vegalite":
            fr.check_vegalite(data)
        elif kind == "mermaid":
            return fr.check_mermaid(data.decode("utf-8", errors="replace"))
    except fr.RenderError as e:
        return str(e)
    return None


def brief(f: dict, settings_url: str | None = None) -> dict:
    keep = ("id", "name", "mime", "size", "preview", "version", "description", "topic", "visibility")
    out = {k: f.get(k) for k in keep if f.get(k) not in (None, "")}
    if public := (settings_url or os.environ.get("POS_PUBLIC_URL")):
        out["url"] = f"{public.rstrip('/')}/files?file={f['id']}"
    return out


def _row_for_check(conn: sqlite3.Connection, file_id: int) -> dict:
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    return {**files.to_dict(row), "path": row["path"], "sha256": row["sha256"]}


def create(conn: sqlite3.Connection, ctx: Ctx, settings: Settings, *, name: str, data: bytes,
           mime: str | None = None, topic: str | None = None, project: str | None = None,
           description: str | None = None, tags: list[str] | None = None, visibility: str | None = None,
           origin: str | None = None) -> dict:
    """A new file (never merged with an existing one of the same content). Returns brief + checks."""
    if not (name or "").strip():
        raise Invalid("give the file a name, with its extension (lan.dot, costs.vl.json, report.md)")
    _check_size(settings, conn, ctx, len(data))
    tag_list = list(tags or [])
    if project:
        from .projects import _row as project_row

        p = project_row(conn, ctx, project)
        tag_list.append(f"project:{p['slug']}")
        topic = topic or p["slug"]
    f = files.upload(conn, ctx, settings.files_dir, io.BytesIO(data), _with_ext(name.strip(), mime), topic=topic,
                     tags=tag_list, visibility=visibility, description=description, dedupe=False,
                     max_bytes=max(len(data), 1), origin=origin)
    out = brief(f)
    if err := check_visual(settings.files_dir, _row_for_check(conn, f["id"]), data):
        out["render_error"] = err + " — fix it with file_update (a new version)"
    return out


def update(conn: sqlite3.Connection, ctx: Ctx, settings: Settings, file_id: int, data: bytes, *,
           name: str | None = None, description: str | None = None) -> dict:
    """New content for a file: a new version (earlier ones stay viewable and restorable)."""
    row = conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise NotFound(f"file {file_id}")
    _check_size(settings, conn, ctx, len(data), replacing=(row["size"] or 0) if not row["archived_at"] else 0)
    f = files.update_content(conn, ctx, settings.files_dir, file_id, io.BytesIO(data),
                             name=_with_ext(name, row["mime"]) if name else None, description=description,
                             max_bytes=max(len(data), 1))
    out = brief(f)
    if err := check_visual(settings.files_dir, _row_for_check(conn, file_id), data):
        out["render_error"] = err + " — fix it with file_update"
    return out


def list_mine(conn: sqlite3.Connection, ctx: Ctx, *, limit: int = 50) -> list[dict]:
    """Files this member made, or that were shared with them, newest first."""
    rows = conn.execute(
        """SELECT * FROM files WHERE archived_at IS NULL AND (created_by = ? OR owner_id = ? OR EXISTS (
               SELECT 1 FROM shares s WHERE s.entity = 'file' AND s.entity_id = files.id AND s.actor_id = ?))
           ORDER BY updated_at DESC, id DESC LIMIT ?""", (ctx.actor_id, ctx.actor_id, ctx.actor_id, limit)).fetchall()
    return files._with_versions(conn, [files.to_dict(r) for r in rows])


# ------------------------------------------------------------------ sharing in chat

_TASK = re.compile(r"^[Tt]-?\d+$")


def share(conn: sqlite3.Connection, ctx: Ctx, file_ids: list[int], *, to: str | None = None,
          ref: str | int | None = None, message: str | None = None) -> dict:
    """Post the files in chat: to "owner" (the default, a DM), a member (a DM), a channel
    ("#name"), or into the conversation of a message (ref = its id: its thread in a channel, the
    DM itself) or about a task (ref = T-123: the task is linked). Private files become readable
    for whoever gets the message. Chat's own rules apply (who may write where, rate limits)."""
    from . import chat, tasks
    from .visibility import share as share_row

    atts, lines = files.chat_attachments(conn, ctx, file_ids)
    reply_to, task_id = None, None
    if ref not in (None, ""):
        r = str(ref).strip()
        if _TASK.match(r):
            task_id = tasks.parse_id(r)
            tasks.get(conn, ctx, task_id)  # exists and is readable
            atts.append({"type": "task", "id": task_id})
        elif r.lstrip("#").isdigit():
            reply_to = int(r.lstrip("#"))
        else:
            raise Invalid("thread_or_task_ref is a message id (e.g. 1234) or a task (T-123)")
    text = "\n".join(x for x in [(message or "").strip(), *lines] if x)
    target = (to or "").strip()
    try:
        if reply_to and not target:
            parent = conn.execute("SELECT channel_id FROM chat_messages WHERE id = ?", (reply_to,)).fetchone()
            if parent is None:
                raise NotFound(f"message {reply_to}")
            channel_id = parent["channel_id"]
        elif target.lower() in ("", "owner", "majitel", "@owner"):
            channel_id = chat.dm_channel(conn, ctx.actor_id, actors.owner_id(conn), ctx)["id"]
        elif target.startswith("#") or target.lower().startswith("channel:"):
            channel_id = chat.resolve_channel(conn, target.split(":", 1)[-1] if ":" in target else target)["id"]
        else:
            try:
                member = chat.resolve_actor(conn, target.lstrip("@"))
                channel_id = chat.dm_channel(conn, ctx.actor_id, member["id"], ctx)["id"]
            except NotFound:
                channel_id = chat.resolve_channel(conn, target)["id"]
        for fid in {a["id"] for a in atts if a["type"] == "file"}:
            row = conn.execute("SELECT visibility FROM files WHERE id = ?", (fid,)).fetchone()
            if row["visibility"] == "private":
                for m in chat.member_ids(conn, channel_id):
                    share_row(conn, "file", fid, m)
        msg = chat.send(conn, ctx, channel_id, text, reply_to=reply_to, attachments=atts)
    except chat.ChatError as e:
        raise Invalid(str(e)) from e
    if task_id and not msg.get("duplicate"):
        from . import comments

        names = ", ".join(a["name"] for a in atts if a["type"] == "file")
        comments.add(conn, ctx, task_id, f"📎 Sdíleno v chatu: {names}")
    return {"message_id": msg["id"], "channel_id": channel_id, **({"already_shared": True} if msg.get("duplicate") else {}),
            "files": [{k: a.get(k) for k in ("id", "name", "preview", "version")} for a in atts if a["type"] == "file"]}
