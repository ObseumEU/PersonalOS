"""REST API for Web Push (pos.push, docs/MOBILE.md): this device's subscription and the member's settings."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import push
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .config import Settings, get_settings

router = APIRouter(prefix="/api/push", tags=["push"], dependencies=[Depends(require_user)])


class Keys(BaseModel):
    p256dh: str
    auth: str


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: Keys
    expirationTime: int | None = None  # noqa: N815 - the browser's PushSubscription.toJSON() field


class UnsubscribeIn(BaseModel):
    endpoint: str


@router.get("/config")
def config(request: Request, conn=Depends(get_db), ctx=Depends(get_ctx), settings: Settings = Depends(get_settings)):
    """The VAPID public key (the browser subscribes with it), whether push is on, the member's
    settings and how many devices get notifications."""
    push.ensure_schema(conn)
    return {"enabled": push.configured(settings), "public_key": settings.vapid_public_key or None,
            "prefs": push.prefs(conn, ctx.actor_id), "devices": len(push.subscriptions(conn, ctx.actor_id))}


@router.post("/subscribe", status_code=201)
def subscribe(body: SubscriptionIn, request: Request, conn=Depends(get_db), ctx=Depends(get_ctx),
              settings: Settings = Depends(get_settings)):
    if not push.configured(settings):
        raise HTTPException(503, "push is not configured on the server")
    try:
        out = push.subscribe(conn, ctx.actor_id, body.model_dump(), device_id=request.session.get("sid"),
                             user_agent=request.headers.get("user-agent", ""))
    except push.PushError as e:
        raise HTTPException(422, str(e)) from e
    conn.commit()
    return out


@router.post("/unsubscribe")
def unsubscribe(body: UnsubscribeIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    removed = push.unsubscribe(conn, ctx.actor_id, body.endpoint)
    conn.commit()
    return {"ok": True, "removed": removed}


@router.get("/prefs")
def get_prefs(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return push.prefs(conn, ctx.actor_id)


@router.put("/prefs")
def put_prefs(body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    try:
        out = push.set_prefs(conn, ctx, body)
    except push.PushError as e:
        raise HTTPException(422, str(e)) from e
    conn.commit()
    return out


@router.post("/test")
def test(request: Request, conn=Depends(get_db), ctx=Depends(get_ctx), settings: Settings = Depends(get_settings)):
    """A test notification to this device (every device of the member when the session has no device)."""
    if not push.configured(settings):
        raise HTTPException(503, "push is not configured on the server")
    sid = request.session.get("sid")
    has_device_sub = sid and any(s["device_id"] == sid for s in push.subscriptions(conn, ctx.actor_id))
    n = push.send(conn, settings, ctx.actor_id,
                  {"title": "PersonalOS", "body": "Oznámení fungují.", "tag": "test", "url": "/m", "kind": "test"},
                  device_id=sid if has_device_sub else None)
    conn.commit()
    return {"sent": n}
