"""REST API for agents, the work board, the approval queue and the kill switch."""

from fastapi import HTTPException, APIRouter, Depends
from pydantic import BaseModel

from . import agents, approvals, killswitch, network, org, tasks
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
    priority: str = "fyi"


class OrgIn(BaseModel):
    role: str | None = None
    team: str | None = None
    reports_to: int | None = None


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


def _hr(conn) -> dict:
    """HR's read-only view (scores, proposals). Missing HR data never breaks the screen."""
    from .hr import service as hr

    try:
        report = hr.agents_overview(conn)
    except Exception as e:  # noqa: BLE001 - HR is optional for this view
        return {"error": str(e), "ratings": {}}
    return {**report, "ratings": {str(r["agent_id"]): r for r in report.get("ratings", [])}}


@router.get("/agents")
def list_agents(conn=Depends(get_db)):
    return {"agents": agents.overview(conn), "permissions": agents.PERMISSIONS,
            "frozen": killswitch.is_frozen(conn), "hr": _hr(conn)}


@router.post("/agents", status_code=201)
def create_agent(body: AgentIn, conn=Depends(get_db), ctx=Depends(get_ctx),
                 settings: Settings = Depends(get_settings)):
    return _wrap(lambda: agents.create_agent(conn, ctx, data_dir=settings.data_dir, **body.model_dump()))


@router.get("/agents/{agent_id}")
def agent_detail(agent_id: int, conn=Depends(get_db)):
    out = agents.detail(conn, agent_id)
    hr = _hr(conn)
    out["hr"] = {"rating": hr["ratings"].get(str(agent_id)),
                 "proposals": [p for p in hr.get("proposals", []) if str(p.get("agent_id")) == str(agent_id)]}
    return out


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


class InstructionsIn(BaseModel):
    text: str
    reason: str = ""


@router.post("/agents/{agent_id}/instructions")
def propose_instructions(agent_id: int, body: InstructionsIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """A new version of the agent's instructions, committed through the Dev agent and the deployer."""
    return _wrap(lambda: agents.propose_instructions(conn, ctx, agent_id, body.text, body.reason))


class HireIn(BaseModel):
    name: str
    purpose: str
    role: str | None = None
    lead: str | int | None = None
    permissions: list[str] | None = None
    budget_class: str = "normal"
    lifetime: str = "long_lived"
    instructions: str = ""
    reason: str = ""


class DecideIn(BaseModel):
    approve: bool
    note: str = ""


@router.get("/hires")
def list_hires(status: str | None = None, conn=Depends(get_db)):
    from . import hiring

    return hiring.list_requests(conn, status)


@router.post("/hires", status_code=201)
def request_hire(body: HireIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    from . import hiring

    out = hiring.request(conn, ctx, **body.model_dump())
    conn.commit()
    return out


@router.post("/hires/{hire_id}/decide")
def decide_hire(hire_id: int, body: DecideIn, conn=Depends(get_db), ctx=Depends(get_ctx),
                settings: Settings = Depends(get_settings)):
    from . import hiring

    out = _wrap(lambda: hiring.decide(conn, ctx, hire_id, body.approve, body.note, data_dir=settings.data_dir))
    conn.commit()
    return out


@router.post("/agents/{agent_id}/restore")
def restore(agent_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return agents.restore(conn, ctx, agent_id)


class EngineIn(BaseModel):
    engine: str | None = None
    model: str | None = None


@router.put("/agents/{agent_id}/engine")
def set_engine(agent_id: int, body: EngineIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: agents.set_engine(conn, ctx, agent_id, body.engine, body.model))


@router.get("/engines")
def engine_status(conn=Depends(get_db)):
    from . import engines

    return engines.status(conn)


@router.post("/agents/{agent_id}/key")
def rotate_key(agent_id: int, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return {"api_key": _wrap(lambda: agents.rotate_key(conn, ctx, agent_id))}


@router.post("/agents/{agent_id}/message")
def message(agent_id: int, body: MessageIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    return _wrap(lambda: agents.send_message(conn, ctx, agent_id, body.body,
                                             tasks.parse_id(body.task_id) if body.task_id else None, body.priority))


@router.get("/agents/{agent_id}/messages")
def messages(agent_id: int, conn=Depends(get_db)):
    return agents.conversation(conn, agent_id)


@router.get("/agents/{agent_id}/status")
def agent_status(agent_id: int, conn=Depends(get_db)):
    return agents.status(conn, agent_id)


@router.get("/org")
def org_chart(conn=Depends(get_db)):
    return {"members": org.chart(conn), "roles": list(org.ROLES), "project_manager": org.pm_id(conn)}


@router.put("/agents/{agent_id}/org")
def set_org(agent_id: int, body: OrgIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    """Role, team and manager (owner only); only the fields sent change."""
    return org.set_org(conn, ctx, agent_id, body.model_dump(include=body.model_fields_set))


@router.get("/runs/active")
def active_runs(conn=Depends(get_db)):
    return agents.active_runs(conn)


@router.get("/network")
def get_network(window: str = "24h", conn=Depends(get_db)):
    try:
        return network.build(conn, window)
    except ValueError as e:
        raise tasks.Invalid(str(e)) from e


@router.get("/board")
def board(conn=Depends(get_db)):
    return agents.board(conn)


@router.get("/browser/screenshots/{rel:path}")
def browser_screenshot(rel: str, settings: Settings = Depends(get_settings)):
    """A screenshot from an agent's browser session (audit log, approvals)."""
    from fastapi.responses import FileResponse

    from . import browser

    path = browser.screenshot_path(settings.data_dir, rel)
    if path is None:
        raise HTTPException(404, "no such screenshot")
    return FileResponse(path, media_type="image/png", headers={"X-Content-Type-Options": "nosniff"})


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
