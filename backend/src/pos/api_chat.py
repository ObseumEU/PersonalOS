"""REST API and live stream for chat (docs/CHAT.md). The web UI acts as the owner."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import chat, tasks
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .config import Settings, get_settings

router = APIRouter(prefix="/api/chat", tags=["chat"], dependencies=[Depends(require_user)])


class ChannelIn(BaseModel):
    name: str
    members: list[int | str] = []
    topic: str = ""
    visibility: str = "team"


class DmIn(BaseModel):
    to: int | str


class MessageIn(BaseModel):
    body: str
    reply_to: int | None = None
    priority: str | None = None


class EditIn(BaseModel):
    body: str


class ReactIn(BaseModel):
    emoji: str


class MemberIn(BaseModel):
    member: int | str


class ReadIn(BaseModel):
    message_id: int | None = None


def _wrap(fn):
    try:
        return fn()
    except chat.ChatError as e:
        raise tasks.Invalid(str(e)) from e


@router.get("/channels")
def channels(all: bool = False, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return chat.list_channels(conn, ctx.actor_id, include_all=all)


@router.post("/channels", status_code=201)
def create_channel(body: ChannelIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: chat.create_channel(conn, ctx, body.name, body.members, body.topic, body.visibility))


@router.post("/dm")
def open_dm(body: DmIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The DM with a member, created on first use."""
    def go():
        ch = chat.dm_channel(conn, ctx.actor_id, chat.resolve_actor(conn, body.to)["id"], ctx)
        conn.commit()
        return chat.channel_view(conn, ch["id"], ctx.actor_id)
    return _wrap(go)


@router.get("/channels/{channel_id}")
def channel(channel_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    chat._check_read(conn, chat._channel(conn, channel_id), ctx.actor_id)
    return chat.channel_view(conn, channel_id, ctx.actor_id)


@router.post("/channels/{channel_id}/archive")
def archive_channel(channel_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: chat.archive_channel(conn, ctx, channel_id))


@router.get("/channels/{channel_id}/messages")
def messages(channel_id: int, before: int | None = None, after: int | None = None, limit: int = 50,
             conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Cursor paging by message id: `before` for older, `after` for newer."""
    return chat.messages(conn, ctx.actor_id, channel_id, before=before, after=after, limit=limit)


@router.post("/channels/{channel_id}/messages", status_code=201)
def send(channel_id: int, body: MessageIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: chat.send(conn, ctx, channel_id, body.body, reply_to=body.reply_to,
                                   priority=body.priority or None))


@router.get("/channels/{channel_id}/members")
def members(channel_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    chat._check_read(conn, chat._channel(conn, channel_id), ctx.actor_id)
    return chat.channel_view(conn, channel_id, ctx.actor_id)["members"]


@router.post("/channels/{channel_id}/members")
def invite(channel_id: int, body: MemberIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: chat.invite(conn, ctx, channel_id, body.member))


@router.post("/channels/{channel_id}/read")
def mark_read(channel_id: int, body: ReadIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return chat.mark_read(conn, ctx, channel_id, body.message_id)


@router.post("/channels/{channel_id}/typing")
def typing(channel_id: int, ctx=Depends(get_ctx)):
    chat.typing(channel_id, ctx.actor_id)
    return {"ok": True}


@router.patch("/messages/{message_id}")
def edit(message_id: int, body: EditIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: chat.edit(conn, ctx, message_id, body.body))


@router.post("/messages/{message_id}/archive")
def archive(message_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return chat.archive_message(conn, ctx, message_id)


@router.post("/messages/{message_id}/react")
def react(message_id: int, body: ReactIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: chat.react(conn, ctx, message_id, body.emoji))


@router.get("/messages/{message_id}/history")
def history(message_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return chat.history(conn, ctx.actor_id, message_id)


@router.get("/members")
def all_members(conn=Depends(get_db)):
    """Everyone who can chat, with who is working now (a running run)."""
    return chat.members_overview(conn)


@router.get("/stream")
async def stream(request: Request, since: int | None = None, timeout: float | None = None,
                 settings: Settings = Depends(get_settings), ctx=Depends(get_ctx)):
    """Server-Sent Events: message, edit, archive, reaction, channel, presence.
    Resumes from `Last-Event-ID` (or `since`), else from now."""
    last = request.headers.get("last-event-id")
    cursor = int(last) if last and last.isdigit() else since
    gen = chat.stream(settings.db_path, ctx.actor_id, cursor, poll=0.2 if timeout else 1.0,
                      timeout=min(timeout, 30) if timeout else None, disconnected=request.is_disconnected)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
