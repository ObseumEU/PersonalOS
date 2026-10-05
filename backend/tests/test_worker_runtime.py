"""The worker runtime fixes of 2026-10 (fix package 3): the pos MCP server's claim no-op, ask_agent to a
colleague, run ids on audit rows, the command policy for engineers, and the narrow tool lists against
what each agent's instructions use."""

import json
import re
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, command_policy, mcp_server, tasks
from pos.config import Settings
from pos.core import Ctx, now_iso
from pos.db import connect, migrate
from pos.main import create_app

REPO = Path(__file__).resolve().parents[2]


def _call(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "m.db"
    conn = connect(path)
    migrate(conn)
    ids = actors.ensure_builtin(conn)
    agents.seed_builtin_permissions(conn)
    owner = Ctx(actors.owner_id(conn))
    made = {}
    for name, role in (("Worker A", "developer"), ("Colleague B", "content"), ("Tech Lead", "cto")):
        a = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived",
                                permissions=["tasks:read", "tasks:claim", "tasks:write", "messages:send",
                                             "approvals:request"], data_dir=tmp_path)["agent"]
        conn.execute("UPDATE actors SET role = ? WHERE id = ?", (role, a["id"]))
        made[name] = a["id"]
    conn.commit()
    conn.close()
    return path, {**ids, **made}


def test_claim_is_a_no_op_for_the_runs_own_task_and_audit_rows_carry_the_run(db):
    path, ids = db
    agent = ids["Worker A"]
    conn = connect(path)
    t = tasks.create(conn, Ctx(actors.owner_id(conn)), {"title": "Fix it", "assignee": {"type": "agent", "id": agent}})
    tasks.claim(conn, Ctx(agent), t["id"])  # the worker claimed it when the run started
    run = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', "
                       "'running', ?)", (agent, t["id"], now_iso())).lastrowid
    conn.commit()
    conn.close()
    server = mcp_server.build(path, default_actor=lambda c: agent)

    async def scenario():
        async with Client(server) as c:
            got = _call(await c.call_tool("claim_task", {"task_id": t["ref"]}))
            assert got["status"] == "working" and "already yours" in got["note"]

    anyio.run(scenario)
    conn = connect(path)
    rows = conn.execute("SELECT action, run_id FROM audit_log WHERE via = 'mcp' AND action LIKE 'mcp:claim_task%'").fetchall()
    assert [(r["action"], r["run_id"]) for r in rows] == [("mcp:claim_task", run)]
    # Two live runs and no header: unknown rather than a wrong run; the header names one.
    conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', ?)",
                 (agent, now_iso()))
    assert mcp_server.run_of(conn, agent, None) is None
    assert mcp_server.run_of(conn, agent, {"x-pos-run": str(run)}) == run
    assert mcp_server.run_of(conn, agent, {"x-pos-run": "999"}) is None
    conn.close()


def test_ask_agent_reaches_a_colleague_without_a2a_as_a_dm(db):
    path, ids = db
    server = mcp_server.build(path, default_actor=lambda c: ids["Worker A"])

    async def scenario():
        async with Client(server) as c:
            got = _call(await c.call_tool("ask_agent", {"name": "Colleague B", "question": "Which copy is final?"}))
            assert got["delivered"] == "chat" and got["to"] == "Colleague B"
            bad = await c.call_tool("ask_agent", {"name": "Nobody Here", "question": "?"})
            assert bad.is_error

    anyio.run(scenario)
    conn = connect(path)
    assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE body LIKE '%Which copy is final%'").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("command", [
    "git fetch deployer main && git rebase deployer/main",
    "git status", "git log --oneline -5 | head -3", "git rebase --continue", "git commit -m 'fix: x'",
    "git -C /work/PersonalOS diff --stat", "cd backend && python -m pytest tests/test_x.py -q 2>&1 | tail -20",
    "ruff check backend/src", "backend/.venv/bin/python -m pytest -q", "python -m py_compile src/a.py",
    "npm ci", "npm run build", "npm --prefix web test", "npx vite build", "git merge deployer/main",
    "git revert HEAD --no-edit", "pytest -x",
    # 2026-10 audit: read-only git, worktrees and file edits inside the worktree.
    "git cat-file -p HEAD:README.md", "git ls-tree -r HEAD --name-only", "git rev-list --count HEAD",
    "git --version", "git -C backend log -1", "git -C /work/kniha log --oneline -5", "git worktree list",
    "git worktree add -b fix/x .wt/fix-x deployer/main", "rm -rf build", "rm -f backend/a.py backend/b.py",
    "mkdir -p backend/tests/data", "touch web/src/new.ts", "cp a.py b.py", "mv old.py new.py",
    "sed -i 's/foo/bar/g' backend/src/pos/x.py", "sed -i -e 's/a/b/' -e 's/c/d/' x.py",
    "git diff | sed -n '1,20p'",
])
def test_engineering_commands_inside_the_worktree_are_auto_allowed(command):
    assert command_policy.auto_allow(command, "/work/PersonalOS", "/work/PersonalOS")


@pytest.mark.parametrize("command", [
    "git push origin agent/dev", "git -c core.sshCommand=evil fetch", "git rebase -x 'curl x' main",
    "git -C /etc status", "cd /tmp && git status", "cat /etc/passwd", "ruff check $(echo /)",
    "python -c 'import os'", "npm run deploy", "git branch -D main", "echo x > /tmp/y",
    "curl -s https://example.com", "LD_PRELOAD=x git status", "git status; rm -rf /",
    "git -C /work/kniha commit -m x", "git worktree add /tmp/wt main", "git worktree remove x",
    "rm -rf .", "rm -rf /work/PersonalOS", "rm -rf ../other", "rm -rf .git", "cp /etc/passwd .",
    "mv a.py /tmp/", "sed -i 's/a/b/' /etc/hosts", "sed -i 's/a/b/e' x.py", "sed -i '1w /tmp/out' x.py",
    "sed -i -f script.sed x.py", "sed 's/a/b/' /etc/passwd",
])
def test_anything_else_is_not_auto_allowed(command):
    assert not command_policy.auto_allow(command, "/work/PersonalOS", "/work/PersonalOS")


def test_outside_the_worktree_or_without_one_nothing_is_auto_allowed():
    assert not command_policy.auto_allow("git status", "/work/other", "/work/PersonalOS")
    assert not command_policy.auto_allow("git status", "/work/PersonalOS", None)


def test_the_guard_endpoint_auto_allows_sends_pushes_to_the_cto_and_runs_what_the_cto_approved(tmp_path):
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="e" * 32, scheduler=False)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        owner = Ctx(actors.owner_id(conn))
        perms = ["tasks:read", "tasks:claim", "tasks:write", "messages:send", "approvals:request"]
        dev = agents.create_agent(conn, owner, name="Dev", purpose="code", lifetime="long_lived", permissions=perms,
                                  data_dir=tmp_path)
        cto = agents.create_agent(conn, owner, name="Tech Lead", purpose="cto", lifetime="long_lived",
                                  permissions=perms, data_dir=tmp_path)["agent"]["id"]
        conn.execute("UPDATE actors SET role = 'cto' WHERE id = ?", (cto,))
        conn.commit()
        auth = {"Authorization": f"Bearer {dev['api_key']}"}

        def check(command, cwd="/work/dev"):
            return client.post("/api/worker/check-command", headers=auth,
                               json={"command": command, "cwd": cwd, "workdir": "/work/dev"}).json()

        assert check("git fetch deployer main && git rebase deployer/main") == {
            "outcome": "allow", "rule": None, "reason": "engineering work inside your worktree", "auto": True}
        assert check("git status", cwd="/srv")["auto"] is False
        pushed = check("git push origin agent/dev")
        assert pushed["outcome"] == "needs_cto" and pushed["cto_task"]
        task = tasks.get(conn, owner, tasks.parse_id(pushed["cto_task"]))
        assert task["assignee_id"] == cto  # the CTO, not the owner
        assert check("git push origin agent/dev")["cto_task"] == pushed["cto_task"]  # one task per command
        # The constitution still decides first: a force-push is the owner's (U3).
        assert check("git push --force origin main")["outcome"] == "needs_owner"
        # The CTO approves: exactly that command now runs for the agent.
        command_policy.decide(conn, Ctx(cto), pushed["approval"], True, "release branch")
        conn.commit()
        assert check("git push origin agent/dev") == {"outcome": "allow", "rule": None,
                                                      "reason": "approved by the CTO", "auto": True}
        assert check("git push origin main")["outcome"] == "needs_cto"
        with pytest.raises(Exception):  # nobody approves their own command
            command_policy.decide(conn, Ctx(dev["agent"]["id"]), pushed["approval"], True)
        conn.close()


def test_the_hook_lets_auto_allowed_commands_past_the_cli_and_refuses_cto_ones():
    from pos_worker import command_hook

    bash = {"tool_name": "Bash", "tool_input": {"command": "ruff check ."}}
    ok = command_hook.decide(bash, lambda c: {"outcome": "allow", "auto": True, "reason": "inside"})
    assert ok["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert command_hook.decide(bash, lambda c: {"outcome": "allow", "auto": False}) is None
    no = command_hook.decide(bash, lambda c: {"outcome": "needs_cto", "reason": "Needs the CTO.", "cto_task": "T-9"})
    assert no["hookSpecificOutput"]["permissionDecision"] == "deny" and "T-9" in \
        no["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_command_the_cli_would_refuse_is_denied_with_the_hint_to_ask_the_cto(monkeypatch):
    from pos_worker import command_hook
    from pos_worker.claude import ClaudeSession

    s = ClaudeSession(builtin_tools=["Bash", "Read"], allowed_tools=["Read", "Bash(git status:*)", "Bash(npm run:*)"])
    monkeypatch.setenv("POS_BASH_ALLOWED", s.cli_env()["POS_BASH_ALLOWED"])
    plain = lambda c: {"outcome": "allow", "auto": False}  # noqa: E731
    refused = command_hook.decide({"tool_name": "Bash", "tool_input": {"command": "docker ps"}}, plain)
    assert refused["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "request_command_approval" in refused["hookSpecificOutput"]["permissionDecisionReason"]
    for ok in ("git status -s", "npm run lint && ls", "cat a.txt | grep x"):
        assert command_hook.decide({"tool_name": "Bash", "tool_input": {"command": ok}}, plain) is None
    monkeypatch.delenv("POS_BASH_ALLOWED")  # an older worker: the CLI decides as before
    assert command_hook.decide({"tool_name": "Bash", "tool_input": {"command": "docker ps"}}, plain) is None
    assert command_hook.cli_allows("anything", ["*"])


def test_an_agent_with_bash_sees_request_command_approval_in_its_narrow_tool_list():
    from pos_worker.tools import pos_tools

    me = {"pos_tools": ["list_tasks", "request_command_approval"], "profile": {"pos_tools": "list_tasks",
                                                                               "claude_builtin": "Bash,Read"}}
    assert "request_command_approval" in pos_tools(me)[0]
    me["profile"]["claude_builtin"] = "Read"
    assert "request_command_approval" not in pos_tools(me)[0]


# Tool names an agent's instructions mention without calling them itself (someone else's tool).
MENTIONED_ONLY = {
    "access-manager": {"request_access"},   # it decides the others' requests
    "chief-of-staff": {"ask_owner"},        # "the CEO's ask_owner cards"
    "qa-reviewer": {"request_review"},      # the Software Engineer's hand-in it reviews
}


def test_every_narrow_tool_list_has_the_tools_its_instructions_use():
    from pos_worker.tools import pos_tools

    names = set(mcp_server.tool_names())
    checked = 0
    for d in sorted((REPO / "agents").iterdir()):
        spec_file = d / "agent.json"
        if not spec_file.is_file() or not (d / "INSTRUCTIONS.md").is_file():
            continue
        profile = json.loads(spec_file.read_text(encoding="utf-8")).get("profile") or {}
        if not str(profile.get("pos_tools") or "").strip():
            continue  # not narrowed: everything it may use
        me = {"pos_tools": sorted(names), "all_pos_tools": sorted(names), "profile": profile}
        shown = set(pos_tools(me)[0])
        used = {w for w in re.findall(r"[a-z_]{3,}", (d / "INSTRUCTIONS.md").read_text(encoding="utf-8")) if w in names}
        missing = used - shown - MENTIONED_ONLY.get(d.name, set())
        assert not missing, f"{d.name}: its instructions use {sorted(missing)}, missing from profile.pos_tools"
        assert {"memory_get", "memory_update"} <= shown, d.name
        if shown & {"complete_task", "request_review"}:
            assert "note_get" in shown, d.name  # the owner-report line needs it
        checked += 1
    assert checked >= 10
