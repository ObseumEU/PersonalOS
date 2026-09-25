"""REST API for agents, the work board, the approval queue and the kill switch."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import agents, approvals, killswitch, tasks
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .config import Settings, get_settings

router = APIRouter(prefix="/api", tags=["agents"], dependencies=[Depends(require_user)])


class AgentIn(BaseModel):
    name: str
    purpose: str
    lifetime: str = "long_lived"
    instructions: str = ""
    permissions: list[str] | None = None
    budget_class: str = "normal"
    expires_at: str | None = None
    runtime: str = "codex_worker"
    a2a_url: str | None = None


class PermissionsIn(BaseModel):
    permissions: list[str]


class MessageIn(BaseModel):
    body: str
    task_id: str | None = None


class DecideIn(BaseModel):
    approve: bool
    comment: str | None = None


class FreezeIn(BaseModel):
    reason: str = ""


def _wrap(fn):
    try:
        return fn()
    except agents.AgentError as e:
        raise tasks.Invalid(str(e)) from e


@router.get("/agents")
def list_agents(conn=Depends(get_db)):
    return {"agents": agents.overview(conn), "permissions": agents.PERMISSIONS}


@router.post("/agents", status_code=201)
def create_agent(body: AgentIn, conn=Depends(get_db), ctx=Depends(get_ctx),
                 settings: Settings = Depends(get_settings)):
    return _wrap(lambda: agents.create_agent(conn, ctx, data_dir=settings.data_dir, **body.model_dump()))


@router.get("/agents/{agent_id}")
def agent_detail(agent_id: int, conn=Depends(get_db)):
    return agents.detail(conn, agent_id)


@router.put("/agents/{agent_id}/permissions")
def set_permissions(agent_id: int, body: PermissionsIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: agents.set_permissions(conn, ctx, agent_id, body.permissions))


@router.post("/agents/{agent_id}/pause")
def pause(agent_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return agents.pause(conn, ctx, agent_id, True)


@router.post("/agents/{agent_id}/resume")
def resume(agent_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return agents.pause(conn, ctx, agent_id, False)


@router.post("/agents/{agent_id}/stop")
def stop(agent_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return agents.stop(conn, ctx, agent_id)


@router.post("/agents/{agent_id}/archive")
def archive(agent_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return agents.archive(conn, ctx, agent_id, "archived by the owner")


@router.post("/agents/{agent_id}/restore")
def restore(agent_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return agents.restore(conn, ctx, agent_id)


@router.post("/agents/{agent_id}/message")
def message(agent_id: int, body: MessageIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: agents.send_message(conn, ctx, agent_id, body.body,
                                             tasks.parse_id(body.task_id) if body.task_id else None))


@router.get("/board")
def board(conn=Depends(get_db)):
    return agents.board(conn)


@router.get("/approvals")
def list_approvals(status: str = "pending", conn=Depends(get_db)):
    if status == "pending":
        items = approvals.pending(conn)
    else:
        rows = conn.execute("SELECT id FROM approvals ORDER BY id DESC LIMIT 100").fetchall()
        items = [approvals.get(conn, r["id"]) for r in rows]
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM actors")}
    kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM actors")}
    for a in items:
        a["requested_by_name"] = names.get(a["requested_by"])
        a["requested_by_kind"] = kinds.get(a["requested_by"])
        a["task_ref"] = tasks.display_id(a["task_id"]) if a["task_id"] else None
    return items


@router.post("/approvals/{approval_id}/decide")
def decide(approval_id: int, body: DecideIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = approvals.decide(conn, ctx, approval_id, body.approve, body.comment)
    conn.commit()
    return out


@router.get("/system/freeze")
def freeze_state(conn=Depends(get_db)):
    return killswitch.state(conn)


@router.post("/system/freeze")
def freeze(body: FreezeIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return killswitch.freeze(conn, ctx, body.reason)


@router.post("/system/unfreeze")
def unfreeze(conn=Depends(get_db), ctx=Depends(get_ctx)):
    return killswitch.unfreeze(conn, ctx)
