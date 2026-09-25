"""REST API for the tool library (docs/TOOLS.md), shown on the Tools page."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from . import tools
from .api_tasks import get_ctx, get_db
from .auth import require_user

router = APIRouter(prefix="/api/tools", tags=["tools"], dependencies=[Depends(require_user)])


class DecideIn(BaseModel):
    approve: bool


@router.get("")
def list_tools(conn=Depends(get_db)):
    out = {"tools": tools.overview(conn), "publications": tools.publications(conn)}
    conn.commit()
    return out


@router.get("/{name}")
def get_tool(name: str, scope: str | None = None, conn=Depends(get_db)):
    out = tools.detail(conn, name, scope)
    conn.commit()
    return out


@router.post("/publications/{pub_id}/decide")
def decide(pub_id: int, body: DecideIn, conn=Depends(get_db), ctx=Depends(get_ctx)):
    out = tools.decide(conn, ctx, pub_id, body.approve)
    conn.commit()
    return out
