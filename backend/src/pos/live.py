"""One live stream per browser tab: GET /api/stream (Server-Sent Events).

Replaces the web app's polling (health, the kill switch, subsystems, "Čeká na
tebe", typing and working state, page reloads). Cheap on the server: one hub
per process does the work for every open connection.

- The hub ticks once a second (sooner when poked: `poke()`, called in-process
  when the typing state or the kill switch changes). Each tick reads the audit
  log after its cursor (one indexed range scan) to learn which kinds of things
  changed (task, approval, chat_message, ...), and the live runs.
- Per viewer it keeps the last snapshot of each part and sends a part only when
  it changed. "Čeká na tebe" is recomputed when something relevant changed (at
  most every NEEDS_MIN_S) and every NEEDS_MAX_S anyway, once per viewer however
  many tabs they have. Subsystems (HTTP probes) are refreshed every
  SUBSYSTEMS_S in a thread, once for everybody.

Events (`event:` name, JSON data):
- hello       {"at"}                      on connect: the API answers (the "API online" dot)
- freeze      the kill switch state       (GET /api/system/freeze)
- needs       the viewer's "Čeká na tebe" (GET /api/needs-me)
- subsystems  the sidebar's list          (GET /api/system/subsystems)
- presence    {"working": [ids], "typing": {channel: [...]}}  (as /api/chat/stream)
- changed     {"domains": ["task", "run", ...]}  pages reload what they show
A comment line every 15 s keeps proxies from closing an idle stream.
"""

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .core import Ctx, now_iso

TICK_S = 1.0
NEEDS_MIN_S = 2.0
NEEDS_MAX_S = 60.0
FREEZE_MAX_S = 15.0
SUBSYSTEMS_S = 60.0
PING_S = 15.0
QUEUE_MAX = 200
# What can change "Čeká na tebe": approvals, asks, reviews (tasks), @mentions.
NEEDS_DOMAINS = {"approval", "task", "chat_message", "owner_ask", "ask", "comment"}

log = logging.getLogger(__name__)


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@dataclass(eq=False)
class Sub:
    viewer: int
    queue: asyncio.Queue
    last: dict = field(default_factory=dict)  # part -> json sent
    dead: bool = False


@dataclass
class _Viewer:
    needs: dict | None = None
    needs_at: float = 0.0
    needs_dirty: bool = True


class Hub:
    def __init__(self, db_path: Path, loop: asyncio.AbstractEventLoop):
        self.db_path = db_path
        self.loop = loop
        self.subs: set[Sub] = set()
        self.viewers: dict[int, _Viewer] = {}
        self.wake = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.cursor: int | None = None
        self.freeze: dict | None = None
        self.freeze_at = 0.0
        self.working: list[int] | None = None
        self.subsystems: list | None = None
        self.subsystems_at = -SUBSYSTEMS_S
        self.subsystems_task: asyncio.Task | None = None

    # -------------------------------------------------------------- subscribers

    def subscribe(self, viewer: int) -> Sub:
        sub = Sub(viewer, asyncio.Queue(QUEUE_MAX))
        self.subs.add(sub)
        self.viewers.setdefault(viewer, _Viewer())
        if self.task is None or self.task.done():
            self.task = self.loop.create_task(self._run())
        self.poke()
        return sub

    def unsubscribe(self, sub: Sub) -> None:
        self.subs.discard(sub)
        if not any(s.viewer == sub.viewer for s in self.subs):
            self.viewers.pop(sub.viewer, None)

    def poke(self) -> None:
        self.wake.set()

    # -------------------------------------------------------------- the loop

    async def _run(self) -> None:
        while self.subs:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 - the stream outlives one bad tick
                log.exception("live stream tick failed")
            try:
                await asyncio.wait_for(self.wake.wait(), TICK_S)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    def _compute(self, viewers: dict[int, _Viewer], fresh: set[int]) -> dict:
        """Blocking part of a tick (in a thread): what changed, and the parts to send."""
        from . import chat, killswitch, needs_me
        from .db import connect

        now = time.monotonic()
        conn = connect(self.db_path)
        try:
            domains: set[str] = set()
            actions: set[str] = set()
            if self.cursor is None:
                self.cursor = conn.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log").fetchone()[0]
            else:
                rows = conn.execute("SELECT id, action, entity FROM audit_log WHERE id > ? ORDER BY id LIMIT 2000",
                                    (self.cursor,)).fetchall()
                for r in rows:
                    self.cursor = r["id"]
                    if r["entity"]:
                        domains.add(r["entity"])
                    actions.add(r["action"])
            if self.freeze is None or actions & {"freeze", "unfreeze"} or now - self.freeze_at >= FREEZE_MAX_S:
                self.freeze, self.freeze_at = killswitch.state(conn), now
            working = chat.working_ids(conn)
            if self.working is not None and working != self.working:
                domains.add("run")
            self.working = working
            typing_any = bool(chat.typing_now())
            per: dict[int, dict] = {}
            for vid, v in viewers.items():
                if domains & NEEDS_DOMAINS:
                    v.needs_dirty = True
                due = v.needs is None or vid in fresh or now - v.needs_at >= NEEDS_MAX_S or (
                    v.needs_dirty and now - v.needs_at >= NEEDS_MIN_S)
                if due:
                    try:
                        v.needs = needs_me.collect(conn, Ctx(vid, via="api"))
                    except Exception:  # noqa: BLE001 - e.g. a disabled account: no list, the rest still flows
                        log.exception("live stream: needs-me for %s failed", vid)
                        v.needs = v.needs or {"count": 0, "counts": {}, "items": []}
                    v.needs_at, v.needs_dirty = now, False
                per[vid] = {"needs": v.needs,
                            "typing": chat.typing_view(conn, vid) if typing_any else {}}
        finally:
            conn.close()
        return {"domains": sorted(domains), "per": per}

    def _subsystems(self) -> list:
        from . import knowledge
        from .db import connect

        conn = connect(self.db_path)
        try:
            return knowledge.subsystems(conn)
        finally:
            conn.close()

    async def _refresh_subsystems(self) -> None:
        try:
            self.subsystems = await asyncio.to_thread(self._subsystems)
        except Exception:  # noqa: BLE001
            log.exception("live stream: subsystems failed")
        self.poke()

    async def _tick(self) -> None:
        if not self.subs:
            return
        now = time.monotonic()
        if now - self.subsystems_at >= SUBSYSTEMS_S and (self.subsystems_task is None or self.subsystems_task.done()):
            self.subsystems_at = now
            self.subsystems_task = self.loop.create_task(self._refresh_subsystems())
        fresh = {s.viewer for s in self.subs if not s.last}
        viewers = {s.viewer: self.viewers.setdefault(s.viewer, _Viewer()) for s in self.subs}
        out = await asyncio.to_thread(self._compute, viewers, fresh)
        for sub in list(self.subs):
            p = out["per"].get(sub.viewer)
            if p is None:
                continue
            parts = {"freeze": self.freeze, "needs": p["needs"],
                     "presence": {"working": self.working or [], "typing": p["typing"]}}
            if self.subsystems is not None:
                parts["subsystems"] = self.subsystems
            first = not sub.last
            for name, data in parts.items():
                blob = json.dumps(data, sort_keys=True, default=str)
                if sub.last.get(name) != blob:
                    sub.last[name] = blob
                    self._put(sub, _sse(name, data))
            if out["domains"] and not first:
                self._put(sub, _sse("changed", {"domains": out["domains"]}))

    def _put(self, sub: Sub, chunk: str) -> None:
        try:
            sub.queue.put_nowait(chunk)
        except asyncio.QueueFull:  # a stuck client: drop it, the browser reconnects and gets a fresh snapshot
            sub.dead = True
            self.unsubscribe(sub)


_hubs: dict[tuple[str, int], Hub] = {}
_hubs_lock = threading.Lock()


def hub(db_path: Path) -> Hub:
    loop = asyncio.get_running_loop()
    key = (str(db_path), id(loop))
    with _hubs_lock:
        h = _hubs.get(key)
        if h is None or h.loop is not loop or h.loop.is_closed():
            for k in [k for k, v in _hubs.items() if v.loop.is_closed()]:
                del _hubs[k]
            h = _hubs[key] = Hub(db_path, loop)
        return h


def poke() -> None:
    """Something changed in this process (typing, the kill switch): tick now, from any thread."""
    for h in list(_hubs.values()):
        if not h.loop.is_closed() and h.subs:
            h.loop.call_soon_threadsafe(h.wake.set)


def subscriber_count() -> int:
    return sum(len(h.subs) for h in list(_hubs.values()))


async def stream(db_path: Path, viewer: int, *, timeout: float | None = None, disconnected=None):
    """The SSE body for one connection."""
    h = hub(db_path)
    sub = h.subscribe(viewer)
    start = last_ping = time.monotonic()
    try:
        yield "retry: 3000\n\n"
        yield _sse("hello", {"at": now_iso()})
        while True:
            try:
                chunk = await asyncio.wait_for(sub.queue.get(), 1.0)
            except asyncio.TimeoutError:
                chunk = ""
            if sub.dead:
                return
            if chunk:
                yield chunk
            now = time.monotonic()
            if now - last_ping >= PING_S:
                last_ping = now
                yield ": ping\n\n"
            if timeout is not None and now - start >= timeout:
                return
            if disconnected is not None and not chunk and await disconnected():
                return
    finally:
        h.unsubscribe(sub)
