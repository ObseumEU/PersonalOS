"""The SRE's svr03 runbook (pos.ops_runbook, ops/runbook): typed allowlisted actions (no shell),
an audit row per call with the run id, redacted and cut output, one CTO task for anything off
the catalogue (never the owner), a clear answer without the executor, the permission gate, and
the host executor's own re-validation."""

import importlib.util
import json
import sys
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, mcp_server, ops_runbook
from pos.access import service as access
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))

    def mk(name, role, perms):
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", permissions=perms,
                                  data_dir=tmp_path)["agent"]["id"]
        conn.execute("UPDATE actors SET role = ? WHERE id = ?", (role, aid))
        conn.commit()
        return aid

    sre = mk("SRE", "sre", ["tasks:read", "tasks:write", "tasks:claim", "messages:send"])
    cto = mk("CTO", "cto", ["tasks:read", "tasks:write", "tasks:claim"])
    other = mk("Writer", "writer", ["tasks:read", "tasks:write", "tasks:claim"])
    access.seed(conn)
    ops_runbook.ensure(conn)
    yield {"conn": conn, "owner": owner, "sre": sre, "cto": cto, "other": other, "db": settings.db_path,
           "tmp": tmp_path}
    conn.close()
    client.__exit__(None, None, None)


def _rows(conn, prefix="ops_runbook:"):
    return [dict(r) | {"detail": json.loads(r["detail"])} for r in conn.execute(
        "SELECT * FROM audit_log WHERE action LIKE ? ORDER BY id", (prefix + "%",))]


def _live_run(conn, actor_id):
    rid = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', "
                       "'2026-10-04T08:00:00+00:00')", (actor_id,)).lastrowid
    conn.commit()
    return rid


def _call(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def _socket(monkeypatch, tmp_path):
    path = tmp_path / "runbook.sock"
    path.write_text("")  # stands in for the socket: only its existence is checked before sending
    monkeypatch.setenv(ops_runbook.SOCKET_ENV, str(path))
    return str(path)


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize("action,params", [
    ("docker_logs", {"container": "personalos-api-1; rm -rf /"}),
    ("docker_logs", {"container": "personalos-api-1", "since": "1h && reboot"}),
    ("docker_logs", {"container": "$(id)"}),
    ("docker_logs", {"container": "`id`"}),
    ("docker_logs", {"container": "personalos-api-1 --follow"}),
    ("docker_logs", {"container": "some-other-container"}),
    ("docker_logs", {"container": "personalos-api-1", "since": "48h"}),
    ("docker_logs", {"container": "personalos-api-1", "tail": 501}),
    ("docker_logs", {"container": "personalos-api-1", "tail": "10|cat"}),
    ("docker_logs", {"container": "personalos-api-1", "follow": True}),
    ("docker_logs", {}),
    ("journalctl_tail", {"unit": "sshd.service"}),
    ("journalctl_tail", {"unit": "pos-ops-runbook.service", "lines": 301}),
    ("systemctl_user_status", {"unit": "pos-ops-runbook.service\nreboot"}),
    ("compose_up", {"stack": "nexus-process-pilot", "service": "postgres"}),
    ("compose_up", {"stack": "kb", "service": "qdrant"}),
    ("compose_up", {"stack": "personalos", "service": "towerdog"}),
    ("restart", {"container": "nexus-process-pilot-postgres-1"}),
    ("restart", {"container": "personalos-api-1 > /etc/passwd"}),
    ("refresh_known_hosts", {"deployer": "../../etc"}),
    ("df", {"path": "/"}),
    ("df", "-h; id"),
])
def test_validation_refuses_anything_off_the_list_or_with_metacharacters(action, params):
    with pytest.raises(ops_runbook.Invalid):
        ops_runbook.validate(action, params)


def test_validated_actions_build_argv_lists_without_a_shell():
    p = ops_runbook.validate("docker_logs", {"container": "personalos-api-1", "since": "30m", "tail": "50"})
    assert ops_runbook.plan("docker_logs", p)["argv"] == ["docker", "logs", "--since", "30m", "--tail", "50",
                                                          "personalos-api-1"]
    p = ops_runbook.validate("compose_up", {"stack": "nexus-process-pilot", "service": "towerdog"})
    argv = ops_runbook.plan("compose_up", p)["argv"]
    assert argv[:2] == ["docker", "compose"] and argv[-5:] == ["up", "-d", "--no-deps", "--no-build", "towerdog"]
    assert "/opt/server/nexus-process-pilot/app/docker-compose.prod.yml" in argv
    p = ops_runbook.validate("journalctl_tail", {"unit": "pos-ops-runbook.service"})
    assert ops_runbook.plan("journalctl_tail", p)["argv"][:2] == ["journalctl", "--user"]
    p = ops_runbook.validate("journalctl_tail", {"unit": "docker.service", "lines": 20})
    assert ops_runbook.plan("journalctl_tail", p)["argv"] == ["journalctl", "-u", "docker.service", "-n", "20",
                                                              "--no-pager"]
    for name in ops_runbook.ACTIONS:  # every plan is a list of plain strings or a pinned file write
        spec = ops_runbook.ACTIONS[name]["params"]
        sample = {k: sorted(s[1])[0] for k, s in spec.items() if s[0] == "choice"}
        if name == "compose_up":
            sample = {"stack": "personalos", "service": "api"}
        pl = ops_runbook.plan(name, ops_runbook.validate(name, sample))
        assert "known_hosts" in pl or all(isinstance(a, str) for a in pl["argv"])


def test_the_pinned_github_keys_match_their_published_fingerprints():
    text = ops_runbook.known_hosts_text()
    assert text.count("github.com ") == 3
    for line in ops_runbook.PINNED_KNOWN_HOSTS:
        assert ops_runbook.fingerprint(line) == ops_runbook.PINNED_FINGERPRINTS[line.split()[1]]


# ------------------------------------------------------------------ the API side

def test_a_call_runs_through_the_executor_with_an_audit_row_and_the_run_id(app, monkeypatch, tmp_path):
    conn, sre = app["conn"], app["sre"]
    _socket(monkeypatch, tmp_path)
    sent = []

    def sender(path, req, timeout):
        sent.append(req)
        secret = "token=" + "s" * 30 + " sk-ant-" + "x" * 30 + "\n"
        return {"ok": True, "exit_code": 0, "output": secret + "line\n" * 3000}

    rid = _live_run(conn, sre)
    out = ops_runbook.run(conn, Ctx(sre, via="mcp", run_id=rid), "docker_logs",
                          {"container": "personalos-api-1", "tail": 100}, "api 500s since 10:00", task_id=None,
                          sender=sender)
    conn.commit()
    assert out["ok"] and out["outcome"] == "ok" and out["truncated"]
    assert len(out["output"]) <= ops_runbook.MAX_OUTPUT + 1
    assert sent == [{"v": 1, "action": "docker_logs", "params": {"container": "personalos-api-1", "since": "1h",
                                                                   "tail": 100},
                     "reason": "api 500s since 10:00", "request_id": None}]
    out2 = ops_runbook.run(conn, Ctx(sre, via="mcp"), "df", {}, "disk check",  # its live run is found
                           sender=lambda *a: {"ok": True, "exit_code": 0, "output": "password=hunter2xx /dev/sda1"})
    assert "hunter2xx" not in out2["output"]
    conn.commit()
    rows = _rows(conn)
    assert [r["action"] for r in rows] == ["ops_runbook:docker_logs", "ops_runbook:df"]
    first = rows[0]
    assert first["run_id"] == rid and rows[1]["run_id"] == rid and first["actor_id"] == sre
    assert first["detail"]["outcome"] == "ok" and first["detail"]["exit_code"] == 0
    assert first["detail"]["output_len"] > ops_runbook.MAX_OUTPUT and first["detail"]["reason"]
    assert first["detail"]["args"]["container"] == "personalos-api-1"


def test_a_refused_value_and_a_missing_reason_are_audited_and_nothing_is_sent(app, monkeypatch, tmp_path):
    conn, sre = app["conn"], app["sre"]
    _socket(monkeypatch, tmp_path)

    def sender(*a):
        raise AssertionError("must not be sent")

    out = ops_runbook.run(conn, Ctx(sre), "restart", {"container": "personalos-api-1;reboot"}, "x", sender=sender)
    assert out["outcome"] == "rejected" and "metacharacters" in out["error"]
    out = ops_runbook.run(conn, Ctx(sre), "uptime", {}, "  ", sender=sender)
    assert out["outcome"] == "rejected" and "reason" in out["error"]
    conn.commit()
    assert [r["detail"]["outcome"] for r in _rows(conn)] == ["rejected", "rejected"]


def test_an_unknown_action_makes_exactly_one_task_for_the_cto_never_the_owner(app):
    conn, sre, cto = app["conn"], app["sre"], app["cto"]
    owner_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id = ?",
                               (actors.owner_id(conn),)).fetchone()[0]
    rid = _live_run(conn, sre)
    out = ops_runbook.run(conn, Ctx(sre, run_id=rid), "apt_upgrade", {"packages": "docker-ce"},
                          "docker 27 has the CVE", sender=lambda *a: pytest.fail("must not run"))
    conn.commit()
    assert out["outcome"] == "escalated" and out["new_task"]
    rows = conn.execute("SELECT * FROM tasks WHERE assignee_id = ?", (cto,)).fetchall()
    assert len(rows) == 1 and "apt_upgrade" in rows[0]["title"]
    assert "docker-ce" in rows[0]["notes"] and "docker 27 has the CVE" in rows[0]["notes"]
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id = ?",
                        (actors.owner_id(conn),)).fetchone()[0] == owner_tasks
    # the same request again: a comment on the open task, still one task
    again = ops_runbook.run(conn, Ctx(sre), "apt_upgrade", {}, "still needed")
    conn.commit()
    assert not again["new_task"] and again["cto_task"] == out["cto_task"]
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id = ?", (cto,)).fetchone()[0] == 1
    audit_rows = _rows(conn)
    assert audit_rows[0]["detail"]["outcome"] == "escalated_cto" and audit_rows[0]["run_id"] == rid


def test_without_the_executor_the_tool_says_so_and_still_audits(app, monkeypatch, tmp_path):
    conn, sre = app["conn"], app["sre"]
    out = ops_runbook.run(conn, Ctx(sre), "uptime", {}, "is the host up", sender=lambda *a: pytest.fail("no"))
    assert out["outcome"] == "executor_missing" and "not installed" in out["error"]
    monkeypatch.setenv(ops_runbook.SOCKET_ENV, str(tmp_path / "nothing-here.sock"))
    out = ops_runbook.run(conn, Ctx(sre), "uptime", {}, "is the host up", sender=lambda *a: pytest.fail("no"))
    assert out["outcome"] == "executor_missing"
    _socket(monkeypatch, tmp_path)

    def down(*a):
        raise ConnectionRefusedError("refused")

    out = ops_runbook.run(conn, Ctx(sre), "uptime", {}, "is the host up", sender=down)
    assert not out["ok"] and out["outcome"] == "executor_error" and "unreachable" in out["error"]
    conn.commit()
    assert [r["detail"]["outcome"] for r in _rows(conn)] == ["executor_missing", "executor_missing",
                                                              "executor_error"]


# ------------------------------------------------------------------ permission and MCP

def test_only_the_sre_holds_ops_runbook_and_it_is_not_an_autonomy_default(app):
    conn, sre, other = app["conn"], app["sre"], app["other"]
    assert mcp_server.may_use(conn, sre, "ops_runbook") and mcp_server.may_use(conn, sre, "ops_runbook_list")
    assert not mcp_server.may_use(conn, other, "ops_runbook")
    assert "ops:runbook" in agents.PERMISSIONS and "ops:runbook" not in access.autonomy_caps()
    assert "tool:ops_runbook" not in access.autonomy_caps()
    assert access.restricted("tool:ops_runbook") and access.restricted("ops:runbook")
    out = access.request_access(conn, Ctx(other), what="capability", capability="ops:runbook", why="restart nexus")
    assert out["status"] == "pending" and out["needs_owner"]
    assert ops_runbook.ensure(conn) == {}  # once
    spec = json.loads((REPO / "agents" / "sre" / "agent.json").read_text(encoding="utf-8"))
    assert "ops:runbook" in spec["permissions"]


def test_mcp_tools_list_the_catalogue_and_refuse_others(app, monkeypatch, tmp_path):
    conn, db, sre, other, cto = app["db"], app["db"], app["sre"], app["other"], app["cto"]
    del conn

    async def call(actor, tool, args):
        async with Client(mcp_server.build(db, default_actor=lambda c: actor)) as c:
            return await c.call_tool(tool, args)

    data = _call(anyio.run(call, sre, "ops_runbook_list", {}))
    assert {a["action"] for a in data["actions"]} == set(ops_runbook.ACTIONS) and not data["executor_configured"]
    data = _call(anyio.run(call, sre, "ops_runbook", {"action": "uptime", "reason": "smoke"}))
    assert data["outcome"] == "executor_missing"
    data = _call(anyio.run(call, sre, "ops_runbook", {"action": "rm_everything", "params": {"path": "/"},
                                                      "reason": "cleanup"}))
    assert data["outcome"] == "escalated"
    assert anyio.run(call, other, "ops_runbook", {"action": "uptime", "reason": "x"}).is_error
    c2 = connect(db)
    assert c2.execute("SELECT COUNT(*) FROM tasks WHERE assignee_id = ?", (cto,)).fetchone()[0] == 1
    assert {r["action"] for r in _rows(c2)} == {"ops_runbook:uptime", "ops_runbook:rm_everything"}
    c2.close()


# ------------------------------------------------------------------ the host executor

@pytest.fixture
def executor(monkeypatch):
    """ops/runbook/executor.py with the catalogue it imports (the frozen copy = pos.ops_runbook)."""
    monkeypatch.setitem(sys.modules, "catalogue", ops_runbook)
    spec = importlib.util.spec_from_file_location("pos_ops_runbook_executor", REPO / "ops" / "runbook" / "executor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_executor_validates_again_and_never_runs_a_command_it_was_handed(executor):
    ran = []

    def runner(argv, timeout):
        ran.append(argv)
        return 0, "ok\n"

    good = {"v": 1, "action": "restart", "params": {"container": "kb-kb-1"}, "reason": "kb hangs"}
    out = executor.handle(good, runner=runner)
    assert out["ok"] and ran == [["docker", "restart", "kb-kb-1"]]
    bad = [
        {**good, "params": {"container": "kb-kb-1; reboot"}},
        {**good, "params": {"container": "nexus-process-pilot-postgres-1"}},
        {**good, "argv": ["sh", "-c", "reboot"]},
        {**good, "action": "shell"},
        {**good, "reason": ""},
        {**good, "v": 2},
        ["docker", "restart", "kb-kb-1"],
        None,
    ]
    for req in bad:
        res = executor.handle(req, runner=runner)
        assert not res["ok"] and res["error"].startswith("refused"), req
    assert len(ran) == 1


def test_the_executor_writes_only_the_pinned_known_hosts_of_a_listed_deployer(executor, tmp_path, monkeypatch):
    target = tmp_path / "known_hosts"
    target.write_text("github.com ssh-rsa AAAAforged\n")
    monkeypatch.setitem(ops_runbook.DEPLOYERS, "personalos", str(target))
    out = executor.handle({"v": 1, "action": "refresh_known_hosts", "params": {"deployer": "personalos"},
                           "reason": "host key mismatch"})
    assert out["ok"], out
    assert target.read_text() == ops_runbook.known_hosts_text()
    assert (tmp_path / "known_hosts.bak").read_text() == "github.com ssh-rsa AAAAforged\n"
    out = executor.handle({"v": 1, "action": "refresh_known_hosts", "params": {"deployer": "/etc/ssh"},
                           "reason": "x"})
    assert not out["ok"]


def test_the_executor_cuts_long_output_and_survives_a_crashing_runner(executor):
    def big(argv, timeout):
        return 0, "x" * (executor.MAX_OUTPUT * 2)

    out = executor.handle({"v": 1, "action": "docker_ps", "params": {}, "reason": "r"}, runner=big)
    assert len(out["output"]) == executor.MAX_OUTPUT

    def boom(argv, timeout):
        raise RuntimeError("docker is gone")

    out = executor.handle({"v": 1, "action": "docker_ps", "params": {}, "reason": "r"}, runner=boom)
    assert not out["ok"] and "docker is gone" in out["error"]
