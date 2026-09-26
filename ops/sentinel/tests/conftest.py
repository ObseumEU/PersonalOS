import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sentinel import config  # noqa: E402
from sentinel.core import Sentinel  # noqa: E402
from sentinel.store import Store  # noqa: E402

T0 = 1_790_000_000.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeDocker:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.lines: dict[str, list[tuple[float, str]]] = {}
        self.restarted: list[str] = []

    def add(self, name, status="running", health=None, restarts=0, started="2026-09-26T08:00:00Z", oom=False,
            exit_code=0):
        self.items[name] = {"name": name, "status": status, "health": health, "restarts": restarts,
                            "started": started, "oom": oom, "exit_code": exit_code}

    def containers(self):
        return [{"id": n, "name": n, "state": c["status"]} for n, c in self.items.items()]

    def inspect(self, name):
        c = self.items[name]
        return {"State": {"Status": c["status"], "Health": {"Status": c["health"]} if c["health"] else None,
                          "StartedAt": c["started"], "OOMKilled": c["oom"], "ExitCode": c["exit_code"]},
                "RestartCount": c["restarts"], "Config": {"Image": "img:latest", "Tty": False},
                "Created": "2026-09-01T00:00:00Z"}

    def logs(self, name, since, until=None, tail=None, tty=False):
        return [(t, line) for t, line in self.lines.get(name, []) if t >= since and (until is None or t <= until)]

    def restart(self, name, timeout_s=10):
        self.restarted.append(name)


class FakePos:
    def __init__(self):
        self.events: list[dict] = []
        self.heartbeats: list[dict] = []
        self.up = True
        self.last_error = ""
        self.outbox: list[dict] = []

    def send(self, path, payload):
        if not self.up:
            self.outbox.append(payload)
            return None
        self.events.append(payload)
        return {"task_ref": f"T-{len(self.events)}"}

    def flush(self):
        if self.up:
            self.events += self.outbox
            self.outbox = []
        return 0

    def pending(self):
        return len(self.outbox)

    def heartbeat(self, payload):
        if self.up:
            self.heartbeats.append(payload)
        else:
            self.last_error = "URLError: refused"
        return self.up


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def cfg(tmp_path):
    c = copy.deepcopy(config.DEFAULTS)
    c.update(pos_url="http://pos", token="t" * 32, docker_host="", nexus_dsn="", knowlage_url="http://kb",
             litellm_url="http://litellm", litellm_key="", test_hook=True, state_dir=str(tmp_path), listen="127.0.0.1:0")
    c["http"] = []
    c["apps_every_min"] = 10_000  # app probes are tested on their own
    return c


@pytest.fixture
def sen(cfg, clock, monkeypatch):
    from sentinel import checks

    monkeypatch.setattr(checks, "host", lambda **_: {"mem_avail_pct": 50.0, "swap_pct": 10.0, "load5": 1.0, "cpus": 4,
                                                     "disk_pct": {"/state": 60.0}})
    s = Sentinel(cfg, store=Store(":memory:"), docker=FakeDocker(), pos=FakePos(), clock=clock)
    s.s.set_meta("created_at", clock() - 3600)  # past the warm-up
    s.s.set_meta("apps_at", clock())
    return s
