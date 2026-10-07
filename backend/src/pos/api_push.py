"""REST API for Web Push (pos.push, docs/MOBILE.md): this device's subscription and the member's settings,
and the approve/reject buttons on an approval notification."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import approvals, push
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .config import Settings, get_settings
from .core import Ctx, Forbidden

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
    sid = request.session.get("sid")
    subs = push.subscriptions(conn, ctx.actor_id)
    return {"enabled": push.configured(settings), "public_key": settings.vapid_public_key or None,
            "prefs": push.prefs(conn, ctx.actor_id), "devices": len(subs),
            # whether the server has a subscription of this very device (the app re-registers when not)
            "this_device": bool(sid) and any(s["device_id"] == sid for s in subs)}


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


class TestIn(BaseModel):
    all: bool = False


@router.post("/test")
def test(request: Request, body: TestIn | None = None, conn=Depends(get_db), ctx=Depends(get_ctx),
         settings: Settings = Depends(get_settings)):
    """A test notification. `{"all": true}` (Settings → "Poslat testovací notifikaci"): one labelled test to
    each of the member's subscribed devices, with the result per device. Without it: this device (every
    device of the member when the session has no subscription of its own)."""
    if not push.configured(settings):
        raise HTTPException(503, "push is not configured on the server")
    if body is not None and body.all:
        out = push.send_test(conn, settings, ctx.actor_id)
        conn.commit()
        return out
    sid = request.session.get("sid")
    has_device_sub = sid and any(s["device_id"] == sid for s in push.subscriptions(conn, ctx.actor_id))
    n = push.send(conn, settings, ctx.actor_id,
                  {"title": "PersonalOS", "body": "Oznámení fungují.", "tag": "test", "url": "/m", "kind": "test"},
                  device_id=sid if has_device_sub else None)
    conn.commit()
    return {"sent": n}


class ClientErrorIn(BaseModel):
    step: str = Field(max_length=40)
    error: str = Field(max_length=300)
    standalone: bool | None = None


@router.post("/client-error", status_code=204)
def client_error(body: ClientErrorIn, request: Request, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """The browser could not turn notifications on (permission, service worker, the push service): recorded,
    so a phone that never subscribes is visible in the push health signal instead of failing silently."""
    push.client_error(conn, ctx.actor_id, request.session.get("sid"), step=body.step, error=body.error,
                      user_agent=request.headers.get("user-agent", ""), standalone=body.standalone)
    conn.commit()


class ActionIn(BaseModel):
    approval_id: int
    decision: Literal["approve", "reject"]
    token: str
    endpoint: str


@router.post("/action")
def action(body: ActionIn, request: Request, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """"Schválit" / "Zamítnout" pressed on an approval notification (the service worker calls this). Only with
    that device's one-time token for this approval, its subscription endpoint and the same device's session
    (pos.push.use_action); otherwise 403, and the service worker opens the app at the approval instead."""
    try:
        push.use_action(conn, token=body.token, approval_id=body.approval_id, endpoint=body.endpoint,
                        actor_id=ctx.actor_id, device_id=request.session.get("sid"))
    except push.PushError as e:
        conn.commit()  # the token is spent either way
        raise HTTPException(403, str(e)) from e
    conn.commit()
    try:
        out = approvals.decide(conn, Ctx(ctx.actor_id, via="push"), body.approval_id, body.decision == "approve")
    except Forbidden as e:  # decided meanwhile elsewhere, or not the owner
        raise HTTPException(409, str(e)) from e
    conn.commit()
    return {"ok": True, "status": out["status"]}
