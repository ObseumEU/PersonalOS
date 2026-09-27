"""Tool calls a model wrote as text (pos.pseudo_tools): detected, never shown as content, never
accepted as a result; a worker run that ends with them is retried once, then escalated; a hired
developer without a profile of its own gets real tools (Bash, git).

The markup is built from pieces here so this file itself never contains a literal tool call."""

import sys

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, agents_code, chat, pseudo_tools, task_summary, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


def tag(name: str, close: bool = False, attrs: str = "") -> str:
    return "<" + ("/" if close else "") + name + attrs + ">"


FC, INV, PAR = "function_calls", "invoke", "parameter"


def fake_call(tool: str = "bash", command: str = "git clone https://github.com/ObseumEU/Kniha.git",
              prefix: str = "", cut: bool = False) -> str:
    out = (tag(prefix + FC) + " " + tag(prefix + INV, attrs=f' name="{tool}"') + " "
           + tag(prefix + PAR, attrs=' name="command"') + command + tag(prefix + PAR, True) + " "
           + tag(prefix + INV, True) + " ")
    return out + ("</fun…" if cut else tag(prefix + FC, True))


T215 = ('Pracuji na úkolu **"Kniha: prozkoumat repo"** (status: working). Začínám klonováním a analýzou repo. '
        + fake_call(cut=True))


# ------------------------------------------------------------------ detection and cleaning

def test_detects_and_cleans_the_t215_text_and_the_antml_variant():
    assert pseudo_tools.contains(T215)
    assert pseudo_tools.clean(T215) == ('Pracuji na úkolu **"Kniha: prozkoumat repo"** (status: working). '
                                        "Začínám klonováním a analýzou repo. [nástroj: bash …]")
    assert "git clone" not in pseudo_tools.clean(T215, marker=False)
    antml = "Hotovo. " + fake_call("mcp__pos__complete_task", "x", prefix="antml:") + " konec"
    assert pseudo_tools.contains(antml)
    assert pseudo_tools.clean(antml) == "Hotovo. [nástroj: pos__complete_task …] konec"
    # a lone call without the wrapper, and a stray closing tag
    assert pseudo_tools.clean("a " + tag(INV, attrs=' name="Read"') + "x" + tag(INV, True) + " b") == "a [nástroj: Read …] b"


def test_code_and_ordinary_text_are_left_alone():
    quoted = "The bug: `" + tag(FC) + "` in text.\n```\n" + fake_call() + "\n```"
    assert not pseudo_tools.contains(quoted) and pseudo_tools.clean(quoted) == quoted
    for text in ("a < b, invoke name later", "<b>bold</b>", "", None, "parameter name=x"):
        assert not pseudo_tools.contains(text)


# ------------------------------------------------------------------ results, chat, memory

def _conn(tmp_path):
    from pos.db import migrate

    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.commit()
    return c


def _agent(conn, tmp_path, name="Kniha Developer", perms=("tasks:read", "tasks:claim", "tasks:write", "messages:send")):
    owner = Ctx(actors.owner_id(conn))
    out = agents.create_agent(conn, owner, name=name, purpose="dev", lifetime="long_lived", permissions=list(perms),
                              data_dir=tmp_path)
    return owner, out["agent"]["id"], out["api_key"]


def test_an_agent_cannot_hand_in_or_report_tool_calls_written_as_text(tmp_path):
    conn = _conn(tmp_path)
    owner, aid, _ = _agent(conn, tmp_path)
    t = tasks.create(conn, owner, {"title": "Explore the repo", "assignee": {"type": "agent", "id": aid}})
    me = Ctx(aid)
    with pytest.raises(tasks.Invalid, match="NOT executed"):
        tasks.report_progress(conn, me, t["id"], 10, T215)
    with pytest.raises(tasks.Invalid, match="NOT executed"):
        tasks.complete(conn, me, t["id"], T215)
    assert tasks.get(conn, owner, t["id"])["status"] != "review"
    # a person may paste anything (e.g. reporting this very bug)
    tasks.update(conn, owner, t["id"], {"progress_note": T215})


def test_agent_chat_and_memory_never_carry_the_markup(tmp_path):
    from pos import agent_memory

    conn = _conn(tmp_path)
    owner, aid, _ = _agent(conn, tmp_path)
    ch = chat.dm_channel(conn, aid, actors.owner_id(conn))
    msg = chat.send(conn, Ctx(aid), ch["id"], "Jdu na to. " + fake_call())
    body = conn.execute("SELECT body FROM chat_messages WHERE id = ?", (msg["id"],)).fetchone()["body"]
    assert body == "Jdu na to. [nástroj: bash …]"
    with pytest.raises(chat.ChatError):
        chat.send(conn, Ctx(aid), ch["id"], fake_call())
    agent_memory.set_body(conn, Ctx(aid), "Repo: ObseumEU/Kniha. " + fake_call())
    assert agent_memory.get(conn, aid)["body"] == "Repo: ObseumEU/Kniha."


# ------------------------------------------------------------------ the summary (Shrnutí)

def test_summary_refuses_a_role_play_with_fake_tool_calls_and_uses_the_fallback(tmp_path, monkeypatch):
    from pos import runner

    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    t = tasks.create(conn, me, {"title": "Kniha: prozkoumat repo",
                                "notes": "Projdi repo ObseumEU/Kniha a napiš stručný nález.\n\n" + fake_call()})
    prompts = []

    def fake_run(c, req):
        prompts.append(req.prompt)
        return runner.RunResult(run_id=7, status="ok", output=T215)

    monkeypatch.setattr(runner, "available", lambda engine="codex": True)
    monkeypatch.setattr(runner, "run", fake_run)
    out = task_summary.summary(conn, me, t["id"])
    assert out["source"] == "fallback" and not pseudo_tools.contains(out["text"])
    assert out["text"].startswith("Projdi repo ObseumEU/Kniha")
    assert "git clone" not in prompts[0] and "nemáš žádné nástroje" in prompts[0]  # input cleaned, role stated
    # a long answer that is not 2-3 sentences, or one that starts doing the task, is no summary either
    assert not task_summary.usable_text("Pracuji na úkolu X. Budu zkoumat repo.")
    assert not task_summary.usable_text("x" * (task_summary.MAX_MODEL_CHARS + 1))
    assert task_summary.usable_text("Jde o průzkum repa Kniha. Právě probíhá.")


def test_a_bad_cached_summary_is_never_served_and_is_written_again(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    t = tasks.create(conn, me, {"title": "Kniha", "notes": "Prozkoumat repo Kniha a sepsat nález."})
    calls = []
    monkeypatch.setattr(task_summary, "_llm", lambda c, task: calls.append(1) or ("Jde o repo Kniha. Probíhá.", 9))
    task_summary.ensure_schema(conn)
    fp = task_summary._stored_fp(task_summary.fingerprint(tasks.get(conn, me, t["id"]), 0), tasks.get(conn, me, t["id"]))
    conn.execute("INSERT INTO task_summaries VALUES (?, ?, 'inbox', ?, 'llm', 200, '2026-09-27T21:17:06+00:00')",
                 (t["id"], fp, T215))
    assert not pseudo_tools.contains(task_summary.summary(conn, me, t["id"], generate=False)["text"])
    assert "Pracuji" not in task_summary.summary(conn, me, t["id"], generate=False)["text"]
    assert task_summary.summary(conn, me, t["id"])["text"] == "Jde o repo Kniha. Probíhá." and calls == [1]
    assert not pseudo_tools.contains(task_summary.cached_for(conn, [t["id"]])[t["id"]])


# ------------------------------------------------------------------ a hired developer's tools

def test_a_hired_developer_without_a_profile_gets_bash_and_git(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENTS_REPO_DIR", str(tmp_path / "no-agents"))  # hired: no agent.json yet
    dev = agents_code.worker_profile("Kniha Developer", role="developer")
    assert "Bash" in dev["claude_builtin"].split(",")
    assert "Bash(git clone:*)" in dev["claude_tools"].split("|") and "Read" in dev["claude_tools"].split("|")
    assert agents_code.worker_profile("Kniha Lead", role="product_lead") == {}  # the pool's defaults, as before
    # its agent.json from the hire has no profile: the role's default still applies; one with a profile wins
    base = tmp_path / "agents"
    (base / "kniha-developer").mkdir(parents=True)
    (base / "kniha-developer" / "agent.json").write_text('{"name": "Kniha Developer", "role": "developer", "effort": "medium"}')
    (base / "ceo").mkdir()
    (base / "ceo" / "agent.json").write_text('{"name": "CEO", "role": "developer", "profile": {"claude_tools": " "}}')
    got = agents_code.worker_profile("Kniha Developer", base)
    assert got["claude_builtin"] == agents_code.DEV_PROFILE["claude_builtin"] and got["effort"] == "medium"
    assert agents_code.worker_profile("CEO", base) == {"claude_tools": " "}


def test_worker_me_serves_the_developer_profile_and_the_session_gets_bash(tmp_path, monkeypatch):
    pytest.importorskip("pos_worker")
    from pos_worker.claude import ClaudeSession
    from pos_worker.tools import tool_list

    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_AGENTS_REPO_DIR", str(tmp_path / "no-agents"))
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        owner, aid, key = _agent(conn, tmp_path)
        conn.execute("UPDATE actors SET role = 'developer' WHERE id = ?", (aid,))
        conn.commit()
        me = client.get("/api/worker/me", headers={"Authorization": f"Bearer {key}"}).json()
        prof = me["profile"]
        s = ClaudeSession(builtin_tools=prof["claude_builtin"].split(","), allowed_tools=tool_list(prof["claude_tools"]),
                          mcp_servers={"pos": {"type": "http", "url": "http://x/mcp"}})
        args = s._args("mcp.json", None, "settings.json")
        assert args[args.index("--tools") + 1].startswith("Bash,")
        assert "Bash(git clone:*)" in args and "--settings" in args  # the command guard hook is on
        conn.close()


def test_the_hire_writes_the_developer_profile_into_git(tmp_path):
    from pos import hiring

    conn = _conn(tmp_path)
    owner = Ctx(actors.owner_id(conn))
    _, aid, _ = _agent(conn, tmp_path)
    agents.create_agent(conn, owner, name="Software Engineer", purpose="code", lifetime="long_lived",
                        permissions=["tasks:read"], data_dir=tmp_path)
    lead = actors.get(conn, actors.owner_id(conn))
    ref = hiring._commit_agent_files(conn, owner, aid, "Kniha Developer", "dev", {"role": "developer"}, lead,
                                     ["tasks:read"], "normal", "# Kniha Developer")
    notes = tasks.get(conn, owner, tasks.parse_id(ref))["notes"]
    assert '"claude_builtin": "Bash,Read,Edit,Write,Glob,Grep"' in notes


# ------------------------------------------------------------------ the worker's guard

FAKE_CLAUDE = r'''
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
sid = args[args.index("--resume") + 1] if "--resume" in args else "sess-fake"
L = "<"
call = (L + "function_calls>" + L + 'invoke name="bash">' + L + 'parameter name="command">git clone x' + L
        + "/parameter>" + L + "/invoke>" + L + "/function_calls>")
def out(ev):
    print(json.dumps(ev), flush=True)
out({"type": "system", "subtype": "init", "session_id": sid, "model": "m"})
fixed = "--resume" in args and os.environ.get("FAKE_MODE") == "fixed"
if fixed:
    assert "NOT executed" in prompt
    out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "tool_use", "name": "Bash"}]}})
    out({"type": "user", "session_id": sid, "message": {"content": [{"type": "tool_result", "content": "cloned"}]}})
    text = "Repo naklonováno, nález: Python, bez testů."
else:
    text = "Začínám klonováním. " + call
out({"type": "assistant", "session_id": sid, "message": {"content": [{"type": "text", "text": text}]}})
out({"type": "result", "subtype": "success", "is_error": False, "session_id": sid, "result": text,
     "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0}})
'''


def _wrap(tmp_path, code):
    script = tmp_path / "fake_claude.py"
    script.write_text(code, encoding="utf-8")
    if sys.platform == "win32":
        cmd = tmp_path / "claude.cmd"
        cmd.write_text(f'@set PYTHONUTF8=1\r\n@"{sys.executable}" "{script}" %*\r\n')
        return str(cmd)
    sh = tmp_path / "claude"
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    sh.chmod(0o755)
    return str(sh)



@pytest.fixture
def pool(tmp_path, monkeypatch):
    pytest.importorskip("pos_worker")
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setenv("POS_AGENT_RUNTIME", "claude")
    settings = Settings(data_dir=tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner, aid, key = _agent(conn, tmp_path)
    lead = agents.create_agent(conn, owner, name="Kniha Lead", purpose="lead", lifetime="long_lived",
                               permissions=["tasks:read", "messages:send"], data_dir=tmp_path)["agent"]["id"]
    conn.execute("UPDATE actors SET reports_to = ? WHERE id = ?", (lead, aid))
    conn.commit()
    yield client, conn, owner, aid, key, lead, _wrap(tmp_path, FAKE_CLAUDE)
    conn.close()
    client.__exit__(None, None, None)


def _worker(client, key, binary, workdir):
    from pos_worker.claude import ClaudeSession
    from pos_worker.client import PosClient
    from pos_worker.loop import Worker

    def new_session(engine, model, me):
        return ClaudeSession(binary=binary, workdir=str(workdir), model=model, system_prompt="rules",
                             mcp_servers={"pos": {"type": "http", "url": "http://x/mcp"}})

    return Worker(PosClient("http://testserver", key, http=client), new_session, poll_wait=0, sleep=lambda s: None)


def test_fake_tool_calls_are_retried_once_then_escalated_never_done(pool, tmp_path, monkeypatch):
    client, conn, owner, aid, key, lead, binary = pool
    monkeypatch.setenv("FAKE_MODE", "again")
    t = tasks.create(conn, owner, {"title": "Kniha: prozkoumat repo", "assignee": {"type": "agent", "id": aid}})
    conn.commit()
    assert _worker(client, key, binary, tmp_path).step() == "error"
    row = tasks.get(conn, owner, t["id"])
    assert row["status"] == "next" and row["assignee_id"] == aid  # not done, not the owner's
    assert "jako text" in row["progress_note"] and not pseudo_tools.contains(row["progress_note"])
    run = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC", (aid,)).fetchone()
    assert run["status"] == "error" and pseudo_tools.MARKER in run["detail"]
    assert run["turns"] is None or run["turns"] >= 0
    incident = conn.execute("SELECT * FROM events WHERE title LIKE ?", ("%wrote tool calls as text%",)).fetchone()
    assert incident is not None and "git clone" not in str(dict(incident))
    dm = conn.execute("""SELECT m.body FROM chat_messages m JOIN channel_members a ON a.channel_id = m.channel_id
                         WHERE a.actor_id = ? AND m.author_id = ?""", (lead, aid)).fetchone()
    assert dm is not None and "[platforma]" in dm["body"]
    owner_msgs = conn.execute("SELECT body FROM chat_messages").fetchall()
    assert not any(pseudo_tools.contains(m["body"]) or "git clone" in m["body"] for m in owner_msgs)


def test_one_retry_with_real_tools_finishes_the_task(pool, tmp_path, monkeypatch):
    client, conn, owner, aid, key, lead, binary = pool
    monkeypatch.setenv("FAKE_MODE", "fixed")
    t = tasks.create(conn, owner, {"title": "Kniha: prozkoumat repo", "assignee": {"type": "agent", "id": aid}})
    conn.commit()
    assert _worker(client, key, binary, tmp_path).step() == "ok"
    row = tasks.get(conn, owner, t["id"])
    assert row["status"] == "review" and row["progress_note"].startswith("Repo naklonováno")
    run = conn.execute("SELECT * FROM runs WHERE actor_id = ? ORDER BY id DESC", (aid,)).fetchone()
    assert run["status"] == "ok" and run["tool_calls"] >= 1

