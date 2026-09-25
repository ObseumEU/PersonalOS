"""REST for connectors: routing rules, events, outbound status, GitHub webhook."""

import hashlib
import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import actors, outbound, routing
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .core import Ctx

router = APIRouter(prefix="/api", tags=["connectors"], dependencies=[Depends(require_user)])
hooks = APIRouter(prefix="/api/hooks", tags=["connectors"])


class RuleIn(BaseModel):
    name: str
    source: str
    match: dict = {}
    assignee: str | None = None
    priority: int | None = None
    topic: str | None = None
    enabled: bool = True
    position: int = 100


class EventIn(BaseModel):
    source: str
    title: str
    body: str = ""
    kind: str | None = None
    ref: str | None = None
    url: str | None = None
    author: str | None = None
    labels: list[str] = []


@router.get("/connectors")
def connectors():
    """What is set up. Secrets are never returned, only whether they exist."""
    out = outbound.configured()
    return {
        "outbound": out,
        "github_webhook": bool(os.environ.get("POS_GITHUB_WEBHOOK_SECRET")),
        "knowlage_ingest": "agents push with their own KB key (worker WORKER_CODEX_CONFIG)",
    }


@router.get("/routes")
def list_routes(conn=Depends(get_db)):
    return routing.list_rules(conn)


@router.post("/routes", status_code=201)
def create_route(body: RuleIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    r = routing.create_rule(conn, ctx, body.model_dump())
    conn.commit()
    return r


@router.patch("/routes/{rule_id}")
def update_route(rule_id: int, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    r = routing.update_rule(conn, ctx, rule_id, body)
    conn.commit()
    return r


@router.post("/routes/{rule_id}/archive")
def archive_route(rule_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    r = routing.archive_rule(conn, ctx, rule_id)
    conn.commit()
    return r


@router.get("/events")
def list_events(conn=Depends(get_db)):
    return routing.list_events(conn)


@router.post("/events", status_code=201)
def post_event(body: EventIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Add an event by hand (for testing a rule)."""
    data = body.model_dump()
    data["meta"] = {"labels": data.pop("labels")}
    out = routing.ingest(conn, Ctx(ctx.actor_id, via="api"), data)
    conn.commit()
    return out


@hooks.post("/github")
async def github_webhook(request: Request, conn=Depends(get_db)):
    """GitHub webhook (issues, pull_request, issue_comment). Signed with
    POS_GITHUB_WEBHOOK_SECRET; off while the secret is not set."""
    secret = os.environ.get("POS_GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(404, "GitHub webhook is not set up")
    raw = await request.body()
    sig = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(401, "bad signature")
    kind = request.headers.get("x-github-event", "")
    payload = await request.json()
    ctx = Ctx(actors.owner_id(conn), via="webhook")
    results = [routing.ingest(conn, ctx, ev) for ev in routing.github_events(kind, payload)]
    conn.commit()
    return {"events": results}
