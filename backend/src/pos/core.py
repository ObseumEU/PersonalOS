"""Small shared primitives: who is acting, and the clock."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Ctx:
    """Who performs a change and through which channel.

    Every write in PersonalOS takes a Ctx so it can be versioned and audited.
    `run_id` links the change to an agent run, which makes "undo what run X
    did" possible.
    """

    actor_id: int
    via: str = "api"  # api | mcp | runner | system
    run_id: int | None = None


class Forbidden(Exception):
    """The actor may not do this (visibility, ownership, policy)."""


class NotFound(Exception):
    pass


TZ = ZoneInfo("Europe/Prague")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> date:
    return datetime.now(TZ).date()
