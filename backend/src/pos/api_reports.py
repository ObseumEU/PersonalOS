"""REST API for the Reports page: weekly reports (pos.weekly) and goals (pos.goals)."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import goals, weekly, weekly_packet
from .api_tasks import get_ctx, get_db
from .auth import require_user
from .core import Ctx, NotFound

router = APIRouter(prefix="/api", tags=["reports"], dependencies=[Depends(require_user)])


def _week(week: str) -> str:
    try:
        weekly_packet.parse_week(week)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return week.strip().upper()


@router.get("/reports")
def list_reports(conn=Depends(get_db)):
    return {"reports": weekly.list_reports(conn), "current_week": weekly_packet.current_week(),
            "schedule": _schedule(conn)}


def _schedule(conn) -> str | None:
    row = conn.execute("SELECT schedule, enabled FROM jobs WHERE action = 'weekly_report'").fetchone()
    return (row["schedule"] if row["enabled"] else None) if row else weekly.schedule()


@router.get("/reports/{week}")
def get_report(week: str, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    try:
        return weekly.get_report(conn, _week(week), ctx.actor_id)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e


@router.post("/reports/{week}/build")
def build(week: str, conn=Depends(get_db)):
    """Build (or refresh) the packet of a week that is not published yet: a preview of the numbers."""
    w = _week(week)
    try:
        packet = weekly.packet_for(conn, w, refresh=True)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    conn.commit()
    return {"week": w, "summary": weekly_packet.summary_line(packet)}


class GoalIn(BaseModel):
    title: str | None = None
    why: str | None = None
    target: str | None = None
    owner: Any = None
    due: str | None = None
    status: str | None = None
    progress: int | None = None
    parent_id: int | None = None
    links: list[Any] | None = None
    metric: str | None = None
    baseline: float | None = None
    current: float | None = None
    target_value: float | None = None


class VetoIn(BaseModel):
    note: str = ""


def _goal_call(conn, fn):
    try:
        out = fn()
    except goals.Invalid as e:
        raise HTTPException(422, str(e)) from e
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    conn.commit()
    return out


@router.get("/goals")
def list_goals(status: str = "active", conn=Depends(get_db)):
    return goals.list_goals(conn, status)


@router.post("/goals", status_code=201)
def create_goal(body: GoalIn, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    return _goal_call(conn, lambda: goals.create(conn, ctx, body.model_dump(exclude_unset=True)))


@router.patch("/goals/{goal_id}")
def update_goal(goal_id: int, body: GoalIn, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    return _goal_call(conn, lambda: goals.update(conn, ctx, goal_id, body.model_dump(exclude_unset=True)))


@router.post("/goals/{goal_id}/veto")
def veto_goal(goal_id: int, body: VetoIn, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    """The owner stops a goal the CEO set (or a proposal)."""
    return _goal_call(conn, lambda: goals.veto(conn, ctx, goal_id, body.note))


@router.post("/goals/{goal_id}/archive")
def archive_goal(goal_id: int, conn=Depends(get_db), ctx: Ctx = Depends(get_ctx)):
    return _goal_call(conn, lambda: goals.archive(conn, ctx, goal_id))
