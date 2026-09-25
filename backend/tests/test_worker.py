import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("pos_worker")
from pos_worker.claude import ClaudeSession  # noqa: E402
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


FAKE_CLAUDE = r'''
import json, sys, time
args = sys.argv[1:]
prompt = sys.stdin.read()
assert "--restricted" in args and "--append-system-prompt-file" in args and "--mcp-config" in args
sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-42"
def out(ev):
    print(json.dumps(ev), flush=True)
out({"type": "system", "subtype": "init", "session_id": sid, "model": args[args.index("--model") + 1]})
if "--resume" in args:
    out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": "Adapted: " + prompt.splitlines()[0]}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "Adapted: " + prompt.splitlines()[0],
         "total_cost_usd": 0.01, "usage": {"input_tokens": 40, "output_tokens": 12, "cache_creation_input_tokens": 0}})
else:
    out({"type": "rate_limit_event", "session_id": sid, "rate_limit_info": {"status": "allowed", "resetsAt": 1790368200, "rateLimitType": "five_hour"}})
    for i in range(4):
        time.sleep(0.5)
        out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "tool_use", "name": "mcp__pos__report_progress"}]}})
        out({"type": "user", "session_id": sid, "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": "Finished without changes",
         "total_cost_usd": 0.02, "usage": {"input_tokens": 90, "output_tokens": 20, "cache_creation_input_tokens": 10}})
'''


def _wrap(tmp_path, name, code):
    script = tmp_path / f"fake_{name}.py"
    script.write_text(code)
    if sys.platform == "win32":
        cmd = tmp_path / f"{name}.cmd"
        cmd.write_text(f'@set PYTHONUTF8=1\r\n@"{sys.executable}" "{script}" %*\r\n')
        return str(cmd)
    sh = tmp_path / name
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    sh.chmod(0o755)
    return str(sh)


@pytest.fixture
def fake_codex(tmp_path):
    return _wrap(tmp_path, "codex", FAKE_CODEX)


@pytest.fixture
def fake_claude(tmp_path):
    return _wrap(tmp_path, "claude", FAKE_CLAUDE)


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


def worker_for(client, key, binary, workdir):
    def new_session(engine, model, me):
        if engine == "claude":
            return ClaudeSession(binary=binary, workdir=str(workdir), model=model, system_prompt=me["guardrails"],
                                 mcp_servers={"pos": {"type": "http", "url": "http://x/mcp"}})
        return CodexSession(binary=binary, workdir=str(workdir))

    return Worker(PosClient("http://testserver", key, http=client), new_session, poll_wait=0, sleep=lambda s: None)


def _inject_when_running(tmp_path, owner, agent_id, text):
    c = connect(Settings(data_dir=tmp_path).db_path)
    for _ in range(100):
        if c.execute("SELECT 1 FROM runs WHERE actor_id = ? AND status = 'running'", (agent_id,)).fetchone():
            break
        time.sleep(0.1)
    time.sleep(0.3)
    agents.send_message(c, owner, agent_id, text, priority="change_plan")
    c.close()


def test_worker_runs_a_task_and_injects_a_message(setup, fake_codex, tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
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


def test_claude_worker_injects_via_resume_and_records_usage(setup, fake_claude, tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "claude")
    client, conn, owner, agent_id, key = setup
    from pos import engines, tasks

    t = tasks.create(conn, owner, {"title": "Draft the release notes", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    worker = worker_for(client, key, fake_claude, tmp_path)
    th = threading.Thread(target=_inject_when_running, args=(tmp_path, owner, agent_id, "Mention the A2A facade"))
    th.start()
    assert worker.step() == "ok"
    th.join()
    run = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC", (agent_id,)).fetchone()
    assert run["engine"] == "claude" and run["status"] == "ok" and run["input_tokens"] == 40
    done = tasks.get(conn, owner, t["id"])
    assert done["status"] == "review" and "Adapted" in done["progress_note"]
    usage = conn.execute("SELECT * FROM engine_usage WHERE actor_id = ?", (agent_id,)).fetchall()
    assert usage and usage[0]["cost_usd"] > 0
    st = engines.status(conn)["claude"]
    assert st["window_5h"]["runs"] == 1 and st["paused_until"] is None


def test_claude_usage_limit_pauses_claude_and_auto_falls_back(setup, monkeypatch):
    client, conn, owner, agent_id, key = setup
    from pos import agents as agents_mod, engines

    run_id = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at, engine) VALUES (?, 'task', 'ok', 'x', 'claude')",
                          (agent_id,)).lastrowid
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    jsonl = '{"type":"result","is_error":true,"result":"Claude AI usage limit reached|4102444800","usage":{}}'
    engines.record_claude(conn, row, jsonl)
    assert engines.paused_until(conn, "claude").startswith("2100-01-01")
    assert engines.choose(conn, agent_id)[0] is None or engines.choose(conn, agent_id)[0] == "codex"
    agents_mod.set_engine(conn, owner, agent_id, "auto")
    engine, why, _ = engines.choose(conn, agent_id)
    assert engine == "codex"  # Claude is paused, so auto falls back to Codex


def test_worker_entry_point_imports_and_reads_config(monkeypatch):
    import importlib

    main = importlib.import_module("pos_worker.__main__")
    monkeypatch.setenv("WORKER_CODEX_CONFIG", 'a="1"||b="2"')
    assert main.extra_config() == ['a="1"', 'b="2"']
    monkeypatch.setenv("WORKER_CLAUDE_MCP", '{"kb": {"type": "http", "url": "http://kb/ingest/mcp"}}')
    assert main.claude_extra_mcp()["kb"]["type"] == "http"


def test_second_worker_of_the_same_agent_does_not_take_a_task_in_progress(setup, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    monkeypatch.delenv("POS_CODEX_DISABLED")
    client, conn, owner, agent_id, key = setup
    from pos import tasks

    t = tasks.create(conn, owner, {"title": "Polish the tasks page", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    h = {"Authorization": f"Bearer {key}"}
    ref = t["ref"]
    first = client.post("/api/worker/runs", json={"task_id": ref, "kind": "task"}, headers=h).json()["run_id"]
    assert client.post(f"/api/worker/tasks/{ref}/claim?run_id={first}", headers=h).status_code == 200

    # A duplicate worker process: the task is not offered, and a claim is refused.
    assert "task" not in client.get("/api/worker/next?wait=0", headers=h).json()
    second = client.post("/api/worker/runs", json={"task_id": ref, "kind": "task"}, headers=h).json()["run_id"]
    assert client.post(f"/api/worker/tasks/{ref}/claim?run_id={second}", headers=h).status_code == 409
    client.post(f"/api/worker/runs/{second}/finish", json={"status": "cancelled", "jsonl": ""}, headers=h)

    # Once the first run is gone (worker crashed), the task can be resumed.
    conn.execute("UPDATE runs SET status = 'error' WHERE id = ?", (first,))
    conn.commit()
    assert client.get("/api/worker/next?wait=0", headers=h).json()["task"]["ref"] == ref
    third = client.post("/api/worker/runs", json={"task_id": ref, "kind": "task"}, headers=h).json()["run_id"]
    assert client.post(f"/api/worker/tasks/{ref}/claim?run_id={third}", headers=h).status_code == 200


FAKE_CODEX_LIMITED = r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "thread.started", "thread_id": "thread-lim"}), flush=True)
print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again at Sep 29th, 2099 8:47 AM."}), flush=True)
print(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}}), flush=True)
sys.exit(1)
'''


def test_auto_is_codex_first_and_switches_to_claude_on_the_codex_limit(setup, fake_claude, tmp_path, monkeypatch):
    monkeypatch.delenv("POS_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("POS_CODEX_DISABLED")
    client, conn, owner, agent_id, key = setup
    from pos import engines, tasks

    assert engines.default_engine() == "auto" and engines.auto_order() == ["codex", "claude"]
    assert engines.choose(conn, agent_id)[0] == "codex"
    limited = _wrap(tmp_path, "codex_limited", FAKE_CODEX_LIMITED)

    def new_session(engine, model, me):
        if engine == "claude":
            return ClaudeSession(binary=fake_claude, workdir=str(tmp_path), model=model, system_prompt=me["guardrails"],
                                 mcp_servers={"pos": {"type": "http", "url": "http://x/mcp"}})
        return CodexSession(binary=limited, workdir=str(tmp_path))

    worker = Worker(PosClient("http://testserver", key, http=client), new_session, poll_wait=0, sleep=lambda s: None)
    t = tasks.create(conn, owner, {"title": "Summarise the inbox", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()

    # Codex refuses: the task is not failed or handed back, it waits in the queue.
    assert worker.step() == "requeued"
    back = tasks.get(conn, owner, t["id"])
    assert back["status"] == "next" and back["assignee_id"] == agent_id and "usage limit" in back["progress_note"]
    assert engines.paused_until(conn, "codex").startswith("2099-09-29")
    # The next run goes to Claude right away and finishes the task.
    assert worker.step() == "ok"
    runs = conn.execute("SELECT engine, status FROM runs WHERE actor_id = ? ORDER BY id", (agent_id,)).fetchall()
    assert [(r["engine"], r["status"]) for r in runs] == [("codex", "error"), ("claude", "ok")]
    assert tasks.get(conn, owner, t["id"])["status"] == "review"
    # After the reset Codex is first again.
    conn.execute("UPDATE engine_limits SET paused_until = '2000-01-01T00:00:00+00:00' WHERE engine = 'codex'")
    assert engines.choose(conn, agent_id)[0] == "codex"
