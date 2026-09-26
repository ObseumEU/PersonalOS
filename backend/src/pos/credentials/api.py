"""/api/credentials for the web app (reading for members, changing for the owner)
and /api/worker/credentials for the worker's credential runner (the agent's key
plus the run's session token). No endpoint here ever returns a value, except
`resolve` to the worker runner, which injects it into one subprocess."""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from .. import tasks
from ..api_tasks import get_ctx, get_db
from ..api_worker import worker_ctx
from ..auth import require_user
from ..core import Forbidden
from . import service

router = APIRouter(prefix="/api/credentials", tags=["credentials"], dependencies=[Depends(require_user)])
worker = APIRouter(prefix="/api/worker/credentials", tags=["worker"])


def _call(fn):
    try:
        return fn()
    except service.CredentialError as e:
        raise tasks.Invalid(str(e)) from e


class CredentialIn(BaseModel):
    name: str | None = None
    op_ref: str | None = None
    description: str | None = None
    env_var: str | None = None
    header: str | None = None
    allowed_hosts: list[str] | str | None = None
    allowed_tools: list[str] | str | None = None
    allowed_commands: list[str] | str | None = None
    max_uses_hour: int | None = None
    notes: str | None = None


class GrantIn(BaseModel):
    agent_id: int
    name: str
    reason: str
    hours: float | None = None
    scope: str | None = None


class ReasonIn(BaseModel):
    reason: str = ""


class DecideIn(BaseModel):
    decision: str
    note: str = ""
    hours: float | None = None


@router.get("")
def overview(conn=Depends(get_db)):
    return service.overview(conn)


@router.get("/vault")
def vault(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.vault_items(conn, ctx))


@router.post("", status_code=201)
def add(body: CredentialIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.add(conn, ctx, body.model_dump(exclude_none=True)))


@router.get("/agents/{agent_id}")
def agent(agent_id: int, conn=Depends(get_db)):
    return service.agent_view(conn, agent_id)


@router.post("/grants", status_code=201)
def grant(body: GrantIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from ..access.service import AccessError

    try:
        return _call(lambda: service.grant(conn, ctx, body.agent_id, body.name, body.reason, body.hours, body.scope))
    except AccessError as e:
        raise tasks.Invalid(str(e)) from e


@router.post("/grants/{grant_id}/revoke")
def revoke(grant_id: int, body: ReasonIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from ..access import service as access

    row = conn.execute("SELECT capability FROM access_grants WHERE id = ?", (grant_id,)).fetchone()
    if row is None or not row["capability"].startswith(service.PREFIX):
        raise HTTPException(404, "not a credential grant")
    return access.revoke_grant(conn, ctx, grant_id, body.reason or "odebráno majitelem")


@router.post("/grants/{grant_id}/resume")
def resume(grant_id: int, body: ReasonIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.resume(conn, ctx, grant_id, body.reason))


@router.post("/requests/{request_id}/decide")
def decide(request_id: int, body: DecideIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from ..access.service import AccessError

    try:
        return _call(lambda: service.decide_request(conn, ctx, request_id, body.decision, body.note, body.hours))
    except AccessError as e:
        raise tasks.Invalid(str(e)) from e


@router.get("/{cid}")
def detail(cid: int, conn=Depends(get_db)):
    return service.detail(conn, cid)


@router.patch("/{cid}")
def update(cid: int, body: CredentialIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.update(conn, ctx, cid, body.model_dump(exclude_none=True)))


@router.post("/{cid}/archive")
def archive(cid: int, body: ReasonIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.archive(conn, ctx, cid, body.reason))


@router.post("/{cid}/test")
def test(cid: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """OK or the error; never the value."""
    return service.test(conn, ctx, cid)


# ------------------------------------------------------------------ the worker's credential runner

class SessionIn(BaseModel):
    run_id: int


class ResolveIn(BaseModel):
    run_id: int
    names: list[str]
    command: str
    tool: str = "command"


@worker.post("/session")
def open_session(body: SessionIn, conn=Depends(get_db), ctx=Depends(worker_ctx)):
    """The worker asks once per run; the token goes only to its credential runner.
    No credential grant: no token, and the worker mounts no runner."""
    held = sorted({g["credential"] for g in service.grants(conn, agent_id=ctx.actor_id)})
    if not held:
        return {"token": None, "credentials": []}
    try:
        return {"token": service.open_session(conn, ctx, body.run_id), "credentials": held}
    except Forbidden as e:
        raise HTTPException(403, str(e)) from e


@worker.post("/resolve")
def resolve(body: ResolveIn, conn=Depends(get_db), ctx=Depends(worker_ctx),
            x_pos_cred_session: str = Header(default="")):
    """Values for one command run by the worker's runner (tool=command only: HTTP is
    done here in the API, credential_http). Refused and logged without a grant."""
    try:
        s = service.check_session(conn, ctx, body.run_id, x_pos_cred_session)
    except Forbidden as e:
        raise HTTPException(403, str(e)) from e
    if body.tool != "command":
        raise HTTPException(422, "only tool=command resolves on the worker; HTTP goes through credential_http")
    try:
        values = service.resolve_for(conn, ctx, body.names, "command", run_id=s["run_id"], task_id=s["task_id"],
                                     command=body.command)
    except service.CredentialError as e:
        raise HTTPException(403, str(e)) from e
    return {"credentials": {n: {"value": v["value"], "env_var": v["env_var"]} for n, v in values.items()}}
