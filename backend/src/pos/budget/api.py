"""HTTP API for the budget agent: /api/budget."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..auth import require_user
from ..config import Settings, get_settings
from ..db import connect
from . import service

router = APIRouter(prefix="/api/budget", tags=["budget"], dependencies=[Depends(require_user)])


def _conn(settings: Settings = Depends(get_settings)) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


@router.get("")
def get_status(conn: sqlite3.Connection = Depends(_conn)) -> dict:
    """Latest check; runs one if there is none yet."""
    return service.status(conn) or service.run_check(conn).__dict__


@router.post("/check")
def post_check(conn: sqlite3.Connection = Depends(_conn)) -> dict:
    return service.run_check(conn).__dict__


@router.get("/gate/{agent_id}")
def get_gate(agent_id: str, conn: sqlite3.Connection = Depends(_conn)) -> dict:
    return service.can_run(conn, agent_id).__dict__


class AgentClass(BaseModel):
    budget_class: Literal["system", "normal", "low"]


@router.put("/agents/{agent_id}")
def put_agent_class(agent_id: str, body: AgentClass, conn: sqlite3.Connection = Depends(_conn)) -> dict:
    service.set_agent_class(conn, agent_id, body.budget_class)
    return {"agent_id": agent_id, "budget_class": body.budget_class}
