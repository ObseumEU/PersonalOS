"""REST API for the Firma page: the company scorecard (pos.scorecard)."""

from fastapi import APIRouter, Depends

from . import scorecard
from .api_tasks import get_db
from .auth import require_user

router = APIRouter(prefix="/api", tags=["scorecard"], dependencies=[Depends(require_user)])


@router.get("/scorecard")
def get_scorecard(conn=Depends(get_db)):
    """Today's snapshot (goals, the owner's requests, spend) with the live counters, deltas and problems."""
    return scorecard.view(conn)


@router.post("/scorecard/refresh")
def refresh(conn=Depends(get_db)):
    """Rebuild today's snapshot now (the measured goals first)."""
    scorecard.update_goals(conn)
    conn.commit()
    return scorecard.view(conn, refresh=True)
