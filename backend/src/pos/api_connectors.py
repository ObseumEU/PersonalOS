"""REST for connectors: routing rules, events, outbound status, GitHub webhook."""

import hashlib
import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .config import Settings, get_settings

from . import a2a, actors, knowledge, outbound, routing, scheduler, schedules, versioning
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .core import Ctx

router = APIRouter(prefix="/api", tags=["connectors"], dependencies=[Depends(require_user)])
hooks = APIRouter(prefix="/api/hooks", tags=["connectors"])
# POST /api/events also takes machine clients (knowlage pushes new Gmail mail):
# a logged-in session or "Authorization: Bearer <token>" from POS_EVENTS_TOKENS.
machine = APIRouter(prefix="/api", tags=["connectors"])


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
    headers: dict[str, str] | list[dict] | None = None


def event_tokens() -> dict[str, str]:
    """POS_EVENTS_TOKENS="knowlage:<token>,other:<token>": token → source name."""
    out = {}
    for item in os.environ.get("POS_EVENTS_TOKENS", "").split(","):
        name, _, token = item.strip().partition(":")
        if name and len(token) >= 16:
            out[token] = name
    return out


def event_sender(request: Request, settings: Settings = Depends(get_settings)) -> str | None:
    """The machine client's name (bearer token), None for a logged-in session."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip().encode()
        name = None
        for token, sender in event_tokens().items():  # constant time, every token compared
            if hmac.compare_digest(given, token.encode()):
                name = sender
        if name is None:
            raise HTTPException(401, "bad event token")
        return name
    require_user(request, settings)
    return None


@router.get("/connectors")
def connectors(conn=Depends(get_db)):
    """What is set up. Secrets are never returned, only whether they exist."""
    out = outbound.configured()
    return {
        "outbound": out,
        "github_webhook": bool(os.environ.get("POS_GITHUB_WEBHOOK_SECRET")),
        "knowlage_ingest": "knowlage ingests e-mail and GitHub itself (its own connectors)",
        "mail_prefilter": routing.skipped_mail(conn),
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


@machine.post("/events", status_code=201)
def post_event(body: EventIn, conn=Depends(get_db), ctx=Depends(get_ctx), sender=Depends(event_sender)):
    """Add an event: by hand (for testing a rule) or from a machine client with
    its token (knowlage: {source:"gmail", kind:"email", labels:[workspaces…,
    "channel:<domain>"], headers?}). Labels and headers feed routing and the
    mail prefilter; the content stays untrusted."""
    data = body.model_dump()
    meta = {"labels": data.pop("labels")}
    headers = data.pop("headers")
    if headers:
        meta["headers"] = headers
    if sender:
        meta["sender"] = sender
    data["meta"] = meta
    actor = actors.find_by_name(conn, sender) if sender else None
    via = f"events:{sender}" if sender else "api"
    out = routing.ingest(conn, Ctx(actor["id"] if actor else ctx.actor_id, via=via), data)
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


@router.get("/jobs")
def list_jobs(conn=Depends(get_db)):
    return scheduler.list_jobs(conn)


@router.patch("/jobs/{job_id}")
def update_job(job_id: int, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    try:
        return scheduler.update_job(conn, ctx, job_id, body)
    except ValueError as e:
        from .tasks import Invalid

        raise Invalid(str(e)) from e


@router.post("/jobs/{job_id}/run")
def run_job(job_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "no such job")
    return scheduler.run_job(conn, job, by=ctx)


class ScheduleIn(BaseModel):
    name: str
    schedule: str
    title: str | None = None
    notes: str | None = None
    definition_of_done: str | None = None
    priority: int | None = None
    topic: str | None = None
    estimate_min: int | None = None
    assignee: str | dict | None = None
    visibility: str = "personal"


@router.get("/schedules")
def list_schedules(actor_id: int | None = None, archived: bool = False, conn=Depends(get_db)):
    return schedules.list_schedules(conn, actor_id=actor_id, archived=archived)


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    s = schedules.create(conn, ctx, body.model_dump())
    conn.commit()
    return s


@router.patch("/schedules/{schedule_id}")
def update_schedule(schedule_id: int, body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    s = schedules.update(conn, ctx, schedule_id, body)
    conn.commit()
    return s


@router.post("/schedules/{schedule_id}/run")
def run_schedule(schedule_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return schedules.fire(conn, schedule_id, manual_by=ctx)


@router.post("/schedules/{schedule_id}/archive")
def archive_schedule(schedule_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    s = schedules.archive(conn, ctx, schedule_id)
    conn.commit()
    return s


@router.post("/schedules/{schedule_id}/restore")
def restore_schedule(schedule_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    versioning.unarchive(conn, ctx, schedules.ENTITY, schedule_id)
    conn.commit()
    return schedules.get(conn, schedule_id)


@router.get("/schedules/{schedule_id}/history")
def schedule_history(schedule_id: int, conn=Depends(get_db)):
    return versioning.history(conn, schedules.ENTITY, schedule_id)


@router.get("/knowledge/graph")
def knowledge_graph(docs: int = 3, collections: int = 40, refresh: bool = False):
    """Our knowlage as a graph: workspace → source → collection → documents."""
    return knowledge.graph(docs_per_collection=max(0, min(docs, 10)), max_collections=max(1, min(collections, 120)),
                           refresh=refresh)


@router.get("/system/subsystems")
def system_subsystems(conn=Depends(get_db)):
    return knowledge.subsystems(conn)


class AskIn(BaseModel):
    question: str
    workspace: str | None = None


@router.post("/knowledge/ask")
def knowledge_ask(body: AskIn):
    """Ask knowlage; the answer carries verified citations."""
    if not body.question.strip():
        raise HTTPException(422, "empty question")
    return knowledge.ask(body.question.strip(), body.workspace)


@router.get("/a2a/links")
def a2a_links(conn=Depends(get_db)):
    members = [{"id": m["id"], "name": m["name"], "a2a_url": m["a2a_url"]} for m in a2a.remote_members(conn).values()]
    return {"members": members, "links": a2a.links(conn)}
