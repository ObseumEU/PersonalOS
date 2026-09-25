"""Wake a waiting worker at once instead of at its next poll.

A worker waits for work in a long poll (`GET /api/worker/next`). While it
waits it listens here; `wake(actor_id)` (after a reassignment, a DM) makes
that wait return right away, so the agent picks the task up in about a
second. The long poll still re-checks the database every few seconds, so a
change made by another process (the CLI, a second API process) is picked up
too, only a little later.

Callers are sync endpoints in FastAPI's thread pool; waiters live on the
event loop, so a wake is handed over with `call_soon_threadsafe`.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_lock = threading.Lock()
_waiters: dict[int, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}


@asynccontextmanager
async def listener(actor_id: int) -> AsyncIterator[asyncio.Event]:
    """Listen for wakes of `actor_id` while inside the block."""
    entry = (asyncio.get_running_loop(), asyncio.Event())
    with _lock:
        _waiters.setdefault(actor_id, set()).add(entry)
    try:
        yield entry[1]
    finally:
        with _lock:
            s = _waiters.get(actor_id)
            if s is not None:
                s.discard(entry)
                if not s:
                    _waiters.pop(actor_id, None)


async def wait(event: asyncio.Event, timeout: float) -> bool:
    """True when woken, False on timeout. Clears the event for the next round."""
    try:
        await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout))
        return True
    except asyncio.TimeoutError:
        return False
    finally:
        event.clear()


def wake(actor_id: int | None) -> int:
    """Wake every worker of `actor_id` waiting right now; returns how many."""
    if actor_id is None:
        return 0
    with _lock:
        entries = list(_waiters.get(actor_id, ()))
    n = 0
    for loop, event in entries:
        try:
            loop.call_soon_threadsafe(event.set)
            n += 1
        except RuntimeError:  # that loop has closed
            pass
    return n


def waiting(actor_id: int) -> int:
    """How many workers of `actor_id` are waiting now (for the UI and tests)."""
    with _lock:
        return len(_waiters.get(actor_id, ()))
