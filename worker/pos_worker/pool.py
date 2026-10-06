"""The agent pool: one worker process for every agent without a container of its own.

    POOL_KEYS_DIR=/run/pos-keys python -m pos_worker.pool

The core writes each pool agent's key into <POOL_KEYS_DIR>/<slug>/key when the
agent is created and removes the folder when it is archived or paused
(pos.workers). This supervisor scans the folder every POOL_SCAN_S seconds
(default 15) and runs `python -m pos_worker` per agent (its own work folder
/work/<slug>); it restarts it when the key changes and stops it when the key
goes.

Lazy mode (POOL_LAZY=1, the default): an idle agent has no process. For each
key the supervisor asks PersonalOS `GET /api/worker/next?wait=0` (no side
effects; it also keeps the agent's last-seen time, so the core knows its
worker is alive); only when the agent has a task or unread messages does it
start the worker,
which ends itself after WORKER_EXIT_IDLE_S (default 120) without a task. The
supervisor itself is one small process; memory grows only with the agents
that are working right now, and POOL_MAX_RUNNING (default 4) caps how many
work at once (a run's CLI takes ~200 MB; the others wait for the next scan,
the owner's messages first, then round robin). A worker whose run is refused
(budget, usage limit) ends at once and its agent waits BLOCKED_BACKOFF_S, so
blocked agents do not hold the slots.
POOL_LAZY=0 keeps one worker per agent running
(a crash restarts it with a growing pause, at most 5 min).

Every other setting (engines, caps, the pos MCP) is the pool container's
environment, shared by all its agents; each agent's own grants and budget
still apply in the core.
"""

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("pos_worker.pool")


def scan(root: Path) -> dict[str, str]:
    """{slug: key} for every non-empty key file."""
    out = {}
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*/key")):
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if key:
            out[path.parent.name] = key
    return out


# Who starts first when the pool is full (POOL_MAX_RUNNING): lower goes first.
RANK_OWNER = 1      # the owner's chat message waits for an answer
RANK_CHAT = 2       # someone else's chat message
RANK_MESSAGES = 3   # unread messages: a DM, "handed in for your review", an @mention
RANK_TASK = 4       # ordinary queued work
BLOCKED_EXIT = 75   # a worker ends with this when its run was refused (budget, usage limit): try later
BLOCKED_BACKOFF_S = 300


def work_rank(work: dict) -> int | None:
    """How urgent the agent's work is (RANK_*), None when it has none (or may not run)."""
    state = work.get("state") or {}
    if state.get("frozen") or state.get("paused") or state.get("archived"):
        return None
    task = work.get("task") or None
    if task and task.get("topic") == "chat":
        return RANK_OWNER if task.get("priority") == 1 else RANK_CHAT
    if not task:
        # Unread messages alone start nobody: a worker without a task cannot deliver them to a run
        # and ended idle after two minutes, again and again. PersonalOS offers an agent with unread
        # messages and no work a task for them (pos.chat.ensure_inbox_task), which starts it here.
        return None
    if work.get("unread_messages"):
        return RANK_MESSAGES  # messages wait as well: ahead of ordinary work
    return RANK_TASK


def has_work(url: str, key: str) -> int | None:
    """Does this agent have work now, a task or unread messages? Its rank (RANK_*), else None.
    (GET /api/worker/next?wait=0; no side effects.)"""
    import httpx

    try:
        r = httpx.get(url.rstrip("/") + "/api/worker/next", params={"wait": 0},
                      headers={"Authorization": f"Bearer {key}"}, timeout=15)
        r.raise_for_status()
        work = r.json()
    except Exception as e:  # noqa: BLE001 - PersonalOS restarting: ask again next scan
        log.info("probe failed: %s", str(e)[:200])
        return None
    return work_rank(work)


class Pool:
    def __init__(self, root: Path, work: Path, spawn=None, clock=time.monotonic, lazy: bool = False, probe=None,
                 max_running: int = 0):
        self.root, self.work = root, work
        self.max_running = max_running  # 0 = no cap
        self.spawn = spawn or self._spawn
        self.clock = clock
        self.lazy = lazy
        self.probe = probe or (lambda slug, key: has_work(os.environ.get("POS_URL", "http://pos-api:8000"), key))
        self.children: dict[str, tuple[object, str]] = {}
        self.failures: dict[str, int] = {}
        self.not_before: dict[str, float] = {}
        self.last_started: dict[str, float] = {}  # round robin among agents of the same rank

    def _spawn(self, slug: str, key: str):
        workdir = self.work / slug
        workdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "POS_AGENT_KEY": key, "WORKER_WORKDIR": str(workdir),
               "POS_AGENT_KEY_FILE": str(self.root / slug / "key")}
        if self.lazy:
            env.setdefault("WORKER_EXIT_IDLE_S", "120")
            env.setdefault("WORKER_POLL", "30")
        return subprocess.Popen([sys.executable, "-m", "pos_worker"], env=env)

    @staticmethod
    def _stop(proc) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()

    def tick(self) -> dict:
        want = scan(self.root)
        started, stopped = [], []
        for slug, (proc, key) in list(self.children.items()):
            if want.get(slug) != key:
                self._stop(proc)
                del self.children[slug]
                stopped.append(slug)
            elif proc.poll() is not None:
                del self.children[slug]
                if proc.returncode == 0 and self.lazy:  # it ended itself when idle: started again on work
                    self.failures.pop(slug, None)
                    continue
                if proc.returncode == BLOCKED_EXIT:  # its run was refused: the slot goes to someone else
                    self.failures.pop(slug, None)
                    self.not_before[slug] = self.clock() + BLOCKED_BACKOFF_S
                    log.info("worker %s: run refused (budget or limit); trying again later", slug)
                    continue
                n = self.failures[slug] = self.failures.get(slug, 0) + 1
                self.not_before[slug] = self.clock() + min(300, 5 * 2 ** min(n, 6))
                log.warning("worker %s exited with %s; restarting later", slug, proc.returncode)
        waiting = []
        for slug, key in want.items():
            if slug in self.children or self.not_before.get(slug, 0) > self.clock():
                continue
            rank = RANK_TASK
            if self.lazy:
                found = self.probe(slug, key)
                if not found:
                    continue
                rank = RANK_TASK if found is True else int(found)
            waiting.append((rank, self.last_started.get(slug, float("-inf")), slug, key))
        # Fair: the owner's messages first, then chat, messages, tasks; within a rank, whoever
        # started longest ago (round robin), so a few agents cannot hold every slot.
        for _rank, _, slug, key in sorted(waiting):
            if self.max_running and len(self.children) >= self.max_running:
                # Memory: it starts when a running one ends. The probe above still ran, so the core
                # sees the agent alive and does not tell the owner its worker is down (2026-09-26).
                continue
            self.children[slug] = (self.spawn(slug, key), key)
            self.last_started[slug] = self.clock()
            started.append(slug)
            log.info("started worker %s", slug)
        return {"started": started, "stopped": stopped, "running": sorted(self.children)}

    def stop_all(self) -> None:
        for proc, _ in self.children.values():
            self._stop(proc)
        self.children.clear()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # a probe per agent every 15 s: not a log line each
    pool = Pool(Path(os.environ.get("POOL_KEYS_DIR", "/run/pos-keys")), Path(os.environ.get("WORKER_WORKDIR", "/work")),
                lazy=os.environ.get("POOL_LAZY", "1") != "0", max_running=int(os.environ.get("POOL_MAX_RUNNING", "4")))
    every = float(os.environ.get("POOL_SCAN_S", "15"))

    def bye(*_):
        pool.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    while True:
        pool.tick()
        time.sleep(every)


if __name__ == "__main__":
    main()
