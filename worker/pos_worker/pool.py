"""The agent pool: one worker process for every agent without a container of its own.

    POOL_KEYS_DIR=/run/pos-keys python -m pos_worker.pool

The core writes each pool agent's key into <POOL_KEYS_DIR>/<slug>/key when the
agent is created and removes the folder when it is archived or paused
(pos.workers). This supervisor scans the folder every POOL_SCAN_S seconds
(default 15): it starts `python -m pos_worker` for a new key (its own work
folder /work/<slug>), restarts one that exited (with a growing pause, at most
5 min), restarts it when the key changes, and stops it when the key goes.
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


class Pool:
    def __init__(self, root: Path, work: Path, spawn=None, clock=time.monotonic):
        self.root, self.work = root, work
        self.spawn = spawn or self._spawn
        self.clock = clock
        self.children: dict[str, tuple[object, str]] = {}
        self.failures: dict[str, int] = {}
        self.not_before: dict[str, float] = {}

    def _spawn(self, slug: str, key: str):
        workdir = self.work / slug
        workdir.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "POS_AGENT_KEY": key, "WORKER_WORKDIR": str(workdir),
               "POS_AGENT_KEY_FILE": str(self.root / slug / "key")}
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
                n = self.failures[slug] = self.failures.get(slug, 0) + 1
                self.not_before[slug] = self.clock() + min(300, 5 * 2 ** min(n, 6))
                log.warning("worker %s exited with %s; restarting later", slug, proc.returncode)
        for slug, key in want.items():
            if slug in self.children or self.not_before.get(slug, 0) > self.clock():
                continue
            self.children[slug] = (self.spawn(slug, key), key)
            started.append(slug)
            log.info("started worker %s", slug)
        return {"started": started, "stopped": stopped, "running": sorted(self.children)}

    def stop_all(self) -> None:
        for proc, _ in self.children.values():
            self._stop(proc)
        self.children.clear()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    pool = Pool(Path(os.environ.get("POOL_KEYS_DIR", "/run/pos-keys")), Path(os.environ.get("WORKER_WORKDIR", "/work")))
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
