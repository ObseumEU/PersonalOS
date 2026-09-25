"""HTTP API for the HR agent: /api/hr (web UI, acting as the owner)."""

import sqlite3
from collections.abc import Iterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import actors
from ..auth import require_user
from ..config import Settings, get_settings
from ..core import Ctx, Forbidden, NotFound
from ..db import connect
from . import service

router = APIRouter(prefix="/api/hr", tags=["hr"], dependencies=[Depends(require_user)])


def _conn(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def _owner(conn: sqlite3.Connection) -> Ctx:
    return Ctx(actors.owner_id(conn), via="api")


class Profile(BaseModel):
    purpose: str
    lifetime: Literal["one_shot", "long_lived"] = "long_lived"
    expires_at: str | None = None


class Admit(BaseModel):
    name: str
    purpose: str
    lifetime: Literal["one_shot", "long_lived"] = "one_shot"


@router.get("")
def overview(conn: sqlite3.Connection = Depends(_conn)) -> dict:
    """Agents with effectiveness scores and what HR would do now (nothing is applied)."""
    return service.agents_overview(conn)


@router.post("/review")
def review(apply: bool = True, conn: sqlite3.Connection = Depends(_conn)) -> dict:
    return service.daily_review(conn, _owner(conn), apply=apply)


@router.post("/weekly")
def weekly(conn: sqlite3.Connection = Depends(_conn)) -> dict:
    return service.weekly_report(conn, _owner(conn))


@router.put("/agents/{agent_id}/profile")
def put_profile(agent_id: int, body: Profile, conn: sqlite3.Connection = Depends(_conn)) -> dict:
    try:
        service.register_agent(conn, agent_id, purpose=body.purpose, lifetime=body.lifetime,
                               expires_at=body.expires_at, created_by=actors.owner_id(conn), ctx=_owner(conn))
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    conn.commit()
    return {"ok": True}


@router.post("/agents/{agent_id}/restore")
def restore(agent_id: int, conn: sqlite3.Connection = Depends(_conn)) -> dict:
    try:
        key = service.restore(conn, _owner(conn), agent_id)
    except (NotFound, Forbidden) as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True, "api_key": key}


@router.post("/admit")
def admit(body: Admit, conn: sqlite3.Connection = Depends(_conn)) -> dict:
    """Would a new agent fit the limits? The same check create_agent runs."""
    return service.admit_agent(conn, _owner(conn), name=body.name, purpose=body.purpose, lifetime=body.lifetime)
