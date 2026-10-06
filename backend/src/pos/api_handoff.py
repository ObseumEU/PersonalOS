"""REST for the owner handoff in an agent's live browser (pos.handoff).

/api/handoffs/*         the owner: the handoff, its live frames, his input, Hotovo / Zrušit / Pokračovat.
                        Signed-in owner only (require_user + is_owner); nothing here is public.
/api/worker/browser/handoff*  the agent's guard (agent key, its running run): ask, poll, frames, done.
"""

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from . import actors, handoff
from .api_tasks import get_ctx, get_db
from .api_worker import _live_run, worker_ctx
from .auth import require_user
from .core import Ctx

router = APIRouter(prefix="/api/handoffs", tags=["handoff"], dependencies=[Depends(require_user)])
worker = APIRouter(prefix="/api/worker/browser/handoff", tags=["worker"])


def _owner(conn, ctx: Ctx) -> None:
    if not actors.get(conn, ctx.actor_id)["is_owner"]:
        raise HTTPException(403, "only the owner opens an agent's browser")


def _err(e: handoff.HandoffError) -> HTTPException:
    return HTTPException(e.code, str(e))


def _device(request: Request) -> str | None:
    return request.session.get("sid") if hasattr(request, "session") else None


# ------------------------------------------------------------------ the owner

@router.get("/{hid}")
def get_handoff(hid: int, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    _owner(conn, ctx)
    try:
        return handoff.view(conn, handoff.check_expiry(conn, handoff.get(conn, hid)))
    except handoff.HandoffError as e:
        raise _err(e) from e


@router.post("/{hid}/open")
def open_handoff(hid: int, request: Request, body: dict | None = None, conn=Depends(get_db),
                 ctx: Ctx = Depends(get_ctx)):
    """The owner's view mounted: audited (who, when, which device and app)."""
    _owner(conn, ctx)
    via = "m" if (body or {}).get("app") == "m" else "web"
    try:
        return handoff.view(conn, handoff.open_(conn, ctx, hid, device=_device(request), via=via))
    except handoff.HandoffError as e:
        raise _err(e) from e


@router.get("/{hid}/frame")
def frame(hid: int, after: int = 0, wait: float = 8.0, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """The newest picture of the agent's page (JPEG), when newer than `after`; waits up to `wait` s for
    one (204: nothing newer, or the handoff ended: check its status). Kept in memory only."""
    _owner(conn, ctx)
    try:
        h = handoff.get(conn, hid)
    except handoff.HandoffError as e:
        raise _err(e) from e
    if h["status"] not in handoff.OPEN:
        return Response(status_code=204, headers={"X-Handoff-Status": h["status"], "Cache-Control": "no-store"})
    got = handoff.get_frame(hid, after, wait)
    if got is None:
        st = handoff.get(conn, hid)["status"]
        return Response(status_code=204, headers={"X-Handoff-Status": st, "Cache-Control": "no-store"})
    version, data, meta = got
    return Response(content=data, media_type="image/jpeg", headers={
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Version": str(version),
        "X-Frame-Width": str(meta.get("w") or 0), "X-Frame-Height": str(meta.get("h") or 0),
        "X-Handoff-Status": h["status"]})


@router.post("/{hid}/input")
def owner_input(hid: int, body: dict, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """The owner's clicks, taps, scrolls and typing for the agent's page. Never stored or logged."""
    _owner(conn, ctx)
    try:
        h = handoff.check_expiry(conn, handoff.get(conn, hid))
    except handoff.HandoffError as e:
        raise _err(e) from e
    if h["status"] not in handoff.OPEN:
        raise HTTPException(409, f"the handoff is {h['status']}")
    return {"accepted": handoff.push_events(hid, body.get("events") or [])}


@router.post("/{hid}/done")
def done(hid: int, body: dict | None = None, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """Hotovo: the agent continues in the same browser. keep_login (default true): it keeps the login."""
    _owner(conn, ctx)
    try:
        h = handoff.finish(conn, ctx, hid, "done", by="owner", keep_login=bool((body or {}).get("keep_login", True)))
    except handoff.HandoffError as e:
        raise _err(e) from e
    return handoff.view(conn, h)


@router.post("/{hid}/cancel")
def cancel(hid: int, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """Zrušit: an open handoff ends (the agent hears it and continues without it); an expired one is dismissed."""
    _owner(conn, ctx)
    try:
        return handoff.view(conn, handoff.close(conn, ctx, hid, resume=False))
    except handoff.HandoffError as e:
        raise _err(e) from e


@router.post("/{hid}/resume")
def resume(hid: int, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """Pokračovat on an expired handoff: the task goes back to the agent, which prepares the page again."""
    _owner(conn, ctx)
    try:
        return handoff.view(conn, handoff.close(conn, ctx, hid, resume=True))
    except handoff.HandoffError as e:
        raise _err(e) from e


# ------------------------------------------------------------------ the agent's guard

@worker.post("")
def request_handoff(body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    from . import browser

    if not browser.may_browse(conn, ctx.actor_id):
        raise HTTPException(403, "this agent lacks tool:browser")
    try:
        h = handoff.create(conn, ctx, run_id=int(body.get("run_id") or 0), title=str(body.get("title") or ""),
                           reason=str(body.get("reason") or ""), url=body.get("url"),
                           done_hint=body.get("done_hint") if isinstance(body.get("done_hint"), dict) else None,
                           minutes=body.get("minutes"))
    except handoff.HandoffError as e:
        raise _err(e) from e
    except (TypeError, ValueError) as e:
        raise HTTPException(422, str(e)) from e
    return {"id": h["id"], "status": h["status"], "expires_at": h["expires_at"]}


@worker.get("/{hid}/poll")
def poll(hid: int, wait: float = 1.0, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    try:
        return handoff.worker_poll(conn, ctx, hid, wait)
    except handoff.HandoffError as e:
        raise _err(e) from e


def _mine(conn, ctx: Ctx, hid: int) -> dict:
    try:
        h = handoff.get(conn, hid)
    except handoff.HandoffError as e:
        raise _err(e) from e
    if h["actor_id"] != ctx.actor_id:
        raise HTTPException(404, "not this agent's handoff")
    return h


@worker.post("/{hid}/frame")
def put_frame(hid: int, body: dict, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    h = _mine(conn, ctx, hid)
    _live_run(conn, ctx, h["run_id"])
    try:
        data = base64.b64decode(str(body.get("frame") or ""), validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(422, "frame: a base64 JPEG") from e
    if not data.startswith(b"\xff\xd8"):
        raise HTTPException(422, "frame: a JPEG")
    v = handoff.put_frame(hid, data, {"url": body.get("url"), "title": body.get("title"), "w": body.get("w"),
                                      "h": body.get("h")})
    return {"version": v, "watching": handoff.watching(hid)}


@worker.post("/{hid}/done")
def done_by_hint(hid: int, conn=Depends(get_db), ctx: Ctx = Depends(worker_ctx)):
    """The guard saw the handoff's done_hint come true (the owner got where he had to)."""
    h = _mine(conn, ctx, hid)
    try:
        h = handoff.finish(conn, ctx, hid, "done", by="hint")
    except handoff.HandoffError as e:
        raise _err(e) from e
    return {"status": h["status"]}
