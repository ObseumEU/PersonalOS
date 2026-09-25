import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("pos_worker")
from pos_worker.client import PosClient  # noqa: E402
from pos_worker.codex import CodexSession  # noqa: E402
from pos_worker.loop import Worker  # noqa: E402

from pos import actors, agents  # noqa: E402
from pos.config import Settings  # noqa: E402
from pos.core import Ctx  # noqa: E402
from pos.db import connect  # noqa: E402
from pos.main import create_app  # noqa: E402

FAKE_CODEX = r'''
import json, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
def out(ev):
    print(json.dumps(ev), flush=True)
if "resume" in args:
    tid = args[args.index("resume") + 1]
    out({"type": "thread.started", "thread_id": tid})
    out({"type": "item.completed", "item": {"type": "agent_message", "text": "Resumed and adapted: " + prompt.splitlines()[0]}})
    out({"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 10}})
else:
    out({"type": "thread.started", "thread_id": "thread-123"})
    for i in range(4):
        time.sleep(0.5)
        out({"type": "item.completed", "item": {"type": "command_execution", "command": f"step {i}"}})
    out({"type": "item.completed", "item": {"type": "agent_message", "text": "Finished without changes"}})
    out({"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 20, "output_tokens": 30}})
'''


@pytest.fixture
def fake_codex(tmp_path):
    script = tmp_path / "fake_codex.py"
    script.write_text(FAKE_CODEX)
    if sys.platform == "win32":
        cmd = tmp_path / "codex.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
        return str(cmd)
    sh = tmp_path / "codex"
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    sh.chmod(0o755)
    return str(sh)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    out = agents.create_agent(conn, owner, name="Dev agent", purpose="code", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "approvals:request"], data_dir=tmp_path)
    yield client, conn, owner, out["agent"]["id"], out["api_key"]
    conn.close()
    client.__exit__(None, None, None)


def worker_for(client, key, codex, workdir):
    return Worker(PosClient("http://testserver", key, http=client),
                  lambda: CodexSession(binary=codex, workdir=str(workdir)), poll_wait=0, sleep=lambda s: None)


def test_worker_runs_a_task_and_injects_a_message(setup, fake_codex, tmp_path):
    client, conn, owner, agent_id, key = setup
    from pos import tasks

    t = tasks.create(conn, owner, {"title": "Fix the timezone bug", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    worker = worker_for(client, key, fake_codex, tmp_path)

    def inject():
        c = connect(Settings(data_dir=tmp_path).db_path)
        for _ in range(100):
            if c.execute("SELECT 1 FROM runs WHERE actor_id = ? AND status = 'running'", (agent_id,)).fetchone():
                break
            time.sleep(0.1)
        time.sleep(0.3)
        agents.send_message(c, owner, agent_id, "Customer is in Prague, use Europe/Prague", priority="change_plan")
        c.close()

    th = threading.Thread(target=inject)
    th.start()
    assert worker.step() == "ok"
    th.join()

    run = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC", (agent_id,)).fetchone()
    # The first turn was stopped mid-way to inject the message; the resumed turn reports usage.
    assert run["status"] == "ok" and run["input_tokens"] >= 50
    msg = conn.execute("SELECT delivered_in_run FROM messages").fetchone()
    assert msg["delivered_in_run"] == run["id"]
    done = tasks.get(conn, owner, t["id"])
    assert done["status"] == "review"
    assert "Resumed and adapted" in done["progress_note"]
    assert conn.execute("SELECT COUNT(*) FROM budget_runs WHERE agent_id = ?", (str(agent_id),)).fetchone()[0] >= 1


def test_worker_holds_when_frozen_and_idles_without_tokens(setup, fake_codex, tmp_path):
    client, conn, owner, agent_id, key = setup
    from pos import killswitch

    worker = worker_for(client, key, fake_codex, tmp_path)
    assert worker.step() == "idle"
    agents.send_message(conn, owner, agent_id, "FYI: new repo layout")
    assert worker.step() == "read_messages" and worker.context[0]["body"] == "FYI: new repo layout"
    killswitch.freeze(conn, owner, "test")
    assert worker.step() == "held"
    assert conn.execute("SELECT COUNT(*) FROM runs WHERE actor_id = ?", (agent_id,)).fetchone()[0] == 0


def test_worker_api_needs_an_agent_key(setup):
    client = setup[0]
    assert client.get("/api/worker/me").status_code == 401
    r = client.get("/api/worker/me", headers={"Authorization": f"Bearer {setup[4]}"})
    assert r.status_code == 200 and "Ústava" in r.json()["guardrails"]
