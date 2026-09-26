"""/api/access: grants, budgets, requests and history for the web app.

Reading is for every logged-in member; changing is checked in the service
(the owner, or the Access manager's hard limits)."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import tasks
from ..api_tasks import get_ctx, get_db
from ..auth import require_user
from . import service

router = APIRouter(prefix="/api/access", tags=["access"], dependencies=[Depends(require_user)])


class GrantIn(BaseModel):
    agent_id: int
    capability: str
    reason: str
    hours: float | None = None


class RevokeIn(BaseModel):
    reason: str


class BudgetIn(BaseModel):
    agent_id: int | None = None  # None: the company-wide cap
    metric: str
    amount: float | None = None
    reason: str
    hours: float | None = None


class DecideIn(BaseModel):
    decision: str
    note: str
    amount: float | None = None
    hours: float | None = None


def _call(fn):
    try:
        return fn()
    except service.AccessError as e:
        raise tasks.Invalid(str(e)) from e


@router.get("")
def company(conn=Depends(get_db)):
    return service.company_view(conn)


@router.get("/agents/{agent_id}")
def agent(agent_id: int, conn=Depends(get_db)):
    return service.agent_view(conn, agent_id)


@router.get("/usage")
def usage(agent_id: int | None = None, days: int = 7, conn=Depends(get_db)):
    return service.usage(conn, agent_id, days)


@router.post("/grants", status_code=201)
def grant(body: GrantIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.grant(conn, ctx, body.agent_id, body.capability, body.reason, body.hours))


@router.post("/grants/{grant_id}/revoke")
def revoke(grant_id: int, body: RevokeIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.revoke_grant(conn, ctx, grant_id, body.reason))


@router.post("/budgets", status_code=201)
def budget(body: BudgetIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.set_budget(conn, ctx, body.agent_id, body.metric, body.amount, body.reason,
                                            body.hours))


@router.post("/requests/{request_id}/decide")
def decide(request_id: int, body: DecideIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.decide(conn, ctx, request_id, body.decision, body.note, body.amount, body.hours))


@router.post("/agents/{agent_id}/resume")
def resume(agent_id: int, body: RevokeIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return {"resumed": _call(lambda: service.resume_agent(conn, ctx, agent_id, body.reason))}


@router.put("/settings")
def put_settings(body: dict, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _call(lambda: service.set_settings(conn, ctx, body))
