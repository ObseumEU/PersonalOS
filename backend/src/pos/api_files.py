"""REST API for files, notes, topics and search (PLAN 3, items 2, 3 and 6)."""

from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile

from . import files, kb_files, notes, topics, versioning
from .api_tasks import RestoreIn, get_ctx, get_db
from .auth import require_user
from .config import Settings, get_settings
from .core import Ctx
from .db import connect

router = APIRouter(prefix="/api", tags=["files"], dependencies=[Depends(require_user)])


class NoteIn(BaseModel):
    title: str
    body: str | None = None
    topic: str | None = None
    tags: list[str] | None = None
    visibility: str | None = None


class TopicIn(BaseModel):
    name: str
    slug: str | None = None
    description: str | None = None
    color: str | None = None


def _fields(body: BaseModel) -> dict:
    return body.model_dump(exclude_unset=True)


# ------------------------------------------------------------------ files

@router.get("/files")
def list_files(topic: str | None = None, tag: str | None = None, q: str | None = None, archived: bool = False,
               conn=Depends(get_db), ctx=Depends(get_ctx)):
    return files.list_files(conn, ctx, topic=topic, tag=tag, q=q, archived=archived)


@router.get("/files/tags")
def file_tags(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return files.tags(conn, ctx)


@router.get("/files/search")
def search_files(q: str, topic: str | None = None, tag: str | None = None, archived: bool = False,
                 conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Search files through knowlage: {mode: "knowlage" | "filename", files, error?}.
    "filename" means knowlage could not answer and only names were matched."""
    return files.search(conn, ctx, q, topic=topic, tag=tag, archived=archived)


@router.post("/files", status_code=201)
async def upload(request: Request, background: BackgroundTasks, settings: Settings = Depends(get_settings)):
    """Multipart upload: `file`, plus optional `topic`, `tags` (comma separated)
    and `visibility`. Larger than POS_MAX_UPLOAD_MB gives 413. Once saved, the
    file is pushed into knowlage in the background (never failing the upload)."""
    max_bytes = settings.max_upload_mb * 1024 * 1024
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes + 64 * 1024:
        raise HTTPException(413, f"file is larger than {settings.max_upload_mb} MB")
    form = await request.form(max_files=1, max_fields=10)
    upload_file = form.get("file")
    if not isinstance(upload_file, UploadFile):
        raise HTTPException(422, "send the file as multipart field 'file'")

    def store():
        conn = connect(settings.db_path)
        try:
            ctx = get_ctx(request, conn)
            out = files.upload(
                conn, ctx, settings.files_dir, upload_file.file, upload_file.filename,
                topic=_text(form.get("topic")), tags=_text(form.get("tags")),
                visibility=_text(form.get("visibility")), max_bytes=max_bytes,
            )
            conn.commit()
            return out
        finally:
            conn.close()

    try:
        out = await run_in_threadpool(store)
        if not out.get("duplicate") or out.get("kb_status") != "ok":
            background.add_task(kb_files.ingest_in_background, settings.db_path, out["id"])
        return out
    except files.TooLarge as e:
        raise HTTPException(413, str(e)) from e
    finally:
        await form.close()


def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


@router.get("/files/{file_id}")
def get_file(file_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return files.get(conn, ctx, file_id)


@router.get("/files/{file_id}/content")
def file_content(file_id: int, download: bool = False, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx),
                 settings: Settings = Depends(get_settings)):
    """The bytes: inline for images, PDFs and text, a download otherwise."""
    path, meta = files.content(conn, ctx, settings.files_dir, file_id)
    kind = meta["preview"]
    inline = kind != "download" and not download
    # Text of any flavour is shown as plain text, never rendered as HTML.
    media = "text/plain; charset=utf-8" if kind == "text" else meta["mime"] or "application/octet-stream"
    if not inline:
        media = "application/octet-stream"
    ascii_name = "".join(ch if ch.isascii() and ch not in '"\\' else "_" for ch in meta["name"])
    headers = {
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{ascii_name}\"; "
                               f"filename*=UTF-8''{quote(meta['name'], safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
    }
    if kind != "pdf":
        # Browsers' PDF viewers do not run in a sandbox; everything else does.
        headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    return FileResponse(path, media_type=media, headers=headers)


@router.patch("/files/{file_id}")
def patch_file(file_id: int, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = files.update(conn, ctx, file_id, body)
    conn.commit()
    return out


@router.post("/files/{file_id}/archive")
def archive_file(file_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = files.archive(conn, ctx, file_id)
    conn.commit()
    return out


@router.post("/files/{file_id}/unarchive")
def unarchive_file(file_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Bring an archived file back."""
    out = files.unarchive(conn, ctx, file_id)
    conn.commit()
    return out


@router.post("/files/{file_id}/restore")
def restore_file(file_id: int, body: dict | None = None, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """With {"version": n}: back to that version. Without (old clients): unarchive."""
    if not body or "version" not in body:
        out = files.unarchive(conn, ctx, file_id)
    else:
        out = files.restore_version(conn, ctx, file_id, int(body["version"]))
    conn.commit()
    return out


class ShareIn(BaseModel):
    entity: str
    id: int
    with_: str | int = Field(alias="with")


@router.post("/share")
def share(body: ShareIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Share a private task, note, file or project with a member or with a project ("project:<slug>")."""
    from .visibility import share_item, shared_with

    share_item(conn, ctx, body.entity, body.id, body.with_)
    conn.commit()
    return shared_with(conn, body.entity, body.id)


@router.get("/share/{entity}/{entity_id}")
def list_shares(entity: str, entity_id: int, conn=Depends(get_db)):
    from .visibility import shared_with

    return shared_with(conn, entity, entity_id)


@router.delete("/share/{entity}/{entity_id}/{actor_id}")
def unshare(entity: str, entity_id: int, actor_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from .visibility import shared_with, unshare as stop

    stop(conn, ctx, entity, entity_id, actor_id)
    conn.commit()
    return shared_with(conn, entity, entity_id)


@router.get("/files/{file_id}/history")
def file_history(file_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return files.history(conn, ctx, file_id)


# ------------------------------------------------------------------ notes

@router.get("/notes")
def list_notes(topic: str | None = None, tag: str | None = None, q: str | None = None, archived: bool = False,
               conn=Depends(get_db), ctx=Depends(get_ctx)):
    return notes.list_notes(conn, ctx, topic=topic, tag=tag, q=q, archived=archived)


@router.post("/notes", status_code=201)
def create_note(body: NoteIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = notes.create(conn, ctx, _fields(body))
    conn.commit()
    return out


@router.get("/notes/{note_id}")
def get_note(note_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return notes.get(conn, ctx, note_id)


@router.patch("/notes/{note_id}")
def patch_note(note_id: int, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = notes.update(conn, ctx, note_id, body)
    conn.commit()
    return out


@router.post("/notes/{note_id}/archive")
def archive_note(note_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = notes.archive(conn, ctx, note_id)
    conn.commit()
    return out


@router.post("/notes/{note_id}/restore")
def restore_note(note_id: int, body: RestoreIn | None = None, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Without a body: bring an archived note back. With {version}: put the
    note back to that version."""
    out = notes.restore(conn, ctx, note_id, body.version) if body else notes.unarchive(conn, ctx, note_id)
    conn.commit()
    return out


@router.get("/notes/{note_id}/history")
def note_history(note_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return notes.history(conn, ctx, note_id)


# ------------------------------------------------------------------ topics and search

@router.get("/topics")
def list_topics(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return topics.list_topics(conn, ctx)


@router.post("/topics", status_code=201)
def create_topic(body: TopicIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = topics.create(conn, ctx, _fields(body))
    conn.commit()
    return out


@router.get("/topics/{slug}")
def get_topic(slug: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return topics.get(conn, ctx, slug)


@router.patch("/topics/{slug}")
def patch_topic(slug: str, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = topics.update(conn, ctx, slug, body)
    conn.commit()
    return out


@router.post("/topics/{slug}/rename")
def rename_topic(slug: str, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """{"to": "new-name"}: renames the label everywhere; into an existing one it merges."""
    out = topics.rename(conn, ctx, slug, str(body.get("to") or ""))
    conn.commit()
    return out


@router.post("/topics/{slug}/archive")
def archive_topic(slug: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = topics.archive(conn, ctx, slug)
    conn.commit()
    return out


@router.post("/topics/{slug}/restore")
def restore_topic(slug: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = topics.unarchive(conn, ctx, slug)
    conn.commit()
    return out


@router.get("/topics/{slug}/history")
def topic_history(slug: str, conn=Depends(get_db), ctx=Depends(get_ctx)):
    row = conn.execute("SELECT id FROM topics WHERE slug = ?", (slug.lower(),)).fetchone()
    return versioning.history(conn, "topic", row["id"]) if row else []


@router.get("/search")
def search(q: str, limit: int = 20, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return topics.search(conn, ctx, q, max(1, min(limit, 50)))
