"""The SRE's svr03 runbook (pos.ops_runbook, ops/runbook): typed allowlisted actions (no shell),
an audit row per call with the run id, redacted and cut output, one CTO task for anything off
the catalogue (never the owner), a clear answer without the executor, the permission gate, and
the host executor's own re-validation."""

import importlib.util
import json
import os
import pwd
import sys
import time
from pathlib import Path

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, mcp_server, ops_runbook, review_policy, tasks
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
    ("caddy_remove_site", {"container": "kb-caddy-1", "site": "a.cz"}),
    ("caddy_remove_site", {"container": "caddy-proxy"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "a.cz; reboot"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "a.cz {\n}\nb.cz"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "https://a.cz"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "a.cz:443"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "*.a.cz"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "a.cz,,b.cz"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "a.cz, a.cz"}),
    ("caddy_remove_site", {"container": "caddy-proxy", "site": "a.cz", "mode": "force"}),
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
        if name == "caddy_remove_site":
            sample["site"] = "example.cz"
        pl = ops_runbook.plan(name, ops_runbook.validate(name, sample))
        argvs = [pl[name][k] for k in ("validate", "reload")] if name == "caddy_remove_site" else [pl.get("argv") or []]
        assert "known_hosts" in pl or "api" in pl or all(isinstance(a, str) for v in argvs for a in v)


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


def test_review_policy_kniha_runs_in_the_api_and_its_output_goes_into_the_task(app, monkeypatch):
    conn, sre, owner = app["conn"], app["sre"], app["owner"]
    calls = []

    def redirect(c, apply=False):
        calls.append(apply)
        return {"to_kniha_lead": ["T-001→Kniha Lead: Opravit formulář"], "applied": apply}

    monkeypatch.setattr(review_policy, "redirect_kniha", redirect)
    task = tasks.create(conn, owner, {"title": "Kniha reviews to the Kniha Lead"})["id"]
    no_executor = {"sender": lambda *a: pytest.fail("never the host executor")}
    out = ops_runbook.run(conn, Ctx(sre), "review_policy_kniha_dry", {}, "T-1002 dry", task_id=task, **no_executor)
    assert out["ok"] and json.loads(out["output"])["to_kniha_lead"] and calls == [False]
    out = ops_runbook.run(conn, Ctx(sre), "review_policy_kniha_apply", None, "T-1002 apply", task_id=task,
                          **no_executor)
    assert out["ok"] and json.loads(out["output"])["applied"] and calls == [False, True]
    with pytest.raises(ops_runbook.Invalid):  # nothing from the caller reaches the command
        ops_runbook.validate("review_policy_kniha_apply", {"db": "/tmp/x.db"})
    conn.commit()
    bodies = [r["body"] for r in conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task,))]
    assert len(bodies) == 2 and "review_policy_kniha_dry" in bodies[0] and "T-001→Kniha Lead" in bodies[1]
    assert [r["detail"]["outcome"] for r in _rows(conn)] == ["ok", "ok"]

    def broken(c, apply=False):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(review_policy, "redirect_kniha", broken)
    out = ops_runbook.run(conn, Ctx(sre), "review_policy_kniha_dry", {}, "again", **no_executor)
    assert not out["ok"] and out["outcome"] == "failed" and "locked" in out["error"]


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
        {**good, "action": "review_policy_kniha_apply", "params": {}},  # runs in the API only
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


CADDYFILE = """{
\temail ops@obseum.cz
}

(internal) {
\t@out not remote_ip 10.0.0.0/8
\trespond @out 403
}

personalos.obseum.cz {
\timport internal
\treverse_proxy personalos-web-1:80
}

libertyloft.cz, www.libertyloft.cz {
\t# old site, "now {on} GitHub Pages"
\theader X-Info "a } b"
\troot * /srv/liberty
\tfile_server {
\t\thide .git
\t}
}

www.libertyloft.cz {
\tredir https://libertyloft.cz{uri}
}

kb.obseum.cz,
  kb2.obseum.cz {
\treverse_proxy kb-caddy-1:8080
}
"""


def _caddy(executor, tmp_path, monkeypatch, site, mode, runner):
    path = tmp_path / "Caddyfile"
    if not path.exists():
        path.write_text(CADDYFILE)
    monkeypatch.setitem(ops_runbook.CADDY, "caddy-proxy", str(path))
    out = executor.handle({"v": 1, "action": "caddy_remove_site", "reason": "T-254 ACME fails",
                           "params": {"container": "caddy-proxy", "site": site, "mode": mode}}, runner=runner)
    return out, path


def test_caddy_remove_site_finds_exactly_one_block_and_keeps_the_rest():
    blocks = ops_runbook.caddy_blocks(CADDYFILE)
    assert [b[2] for b in blocks] == [(), ("(internal)",), ("personalos.obseum.cz",),
                                      ("libertyloft.cz", "www.libertyloft.cz"), ("www.libertyloft.cz",),
                                      ("kb.obseum.cz", "kb2.obseum.cz")]
    new = ops_runbook.remove_caddy_site(CADDYFILE, "www.libertyloft.cz, libertyloft.cz")
    assert "libertyloft.cz, www" not in new and "root * /srv/liberty" not in new and "hide .git" not in new
    assert new.count("www.libertyloft.cz {") == 1 and "kb2.obseum.cz {" in new and "\n\n\n" not in new
    assert len(new.splitlines()) == len(CADDYFILE.splitlines()) - 9  # 8 lines and the blank after
    assert "kb.obseum.cz, kb2.obseum.cz" in ops_runbook.validate(
        "caddy_remove_site", {"container": "caddy-proxy", "site": "KB.obseum.cz,kb2.obseum.cz"})["site"]
    for site in ("libertyloft.cz", "obseum.cz", "personalos.obseum.cz, kb.obseum.cz"):  # no exact match
        with pytest.raises(ops_runbook.Invalid, match="no site blocks"):
            ops_runbook.remove_caddy_site(CADDYFILE, site)
    twice = CADDYFILE + "\nwww.libertyloft.cz {\n\trespond 404\n}\n"
    with pytest.raises(ops_runbook.Invalid, match="2 site blocks"):
        ops_runbook.remove_caddy_site(twice, "www.libertyloft.cz")
    with pytest.raises(ops_runbook.Invalid, match="unclosed"):
        ops_runbook.remove_caddy_site(CADDYFILE + "a.cz {\n", "a.cz")


def test_caddy_remove_site_dry_shows_the_diff_and_writes_nothing(executor, tmp_path, monkeypatch):
    out, path = _caddy(executor, tmp_path, monkeypatch, "libertyloft.cz, www.libertyloft.cz", "dry",
                       lambda argv, t: pytest.fail("a dry run runs nothing"))
    assert out["ok"] and "dry run" in out["output"] and "-libertyloft.cz, www.libertyloft.cz {" in out["output"]
    assert path.read_text() == CADDYFILE and [p.name for p in tmp_path.iterdir()] == ["Caddyfile"]
    out, _ = _caddy(executor, tmp_path, monkeypatch, "nowhere.cz", "apply", lambda argv, t: pytest.fail("no"))
    assert not out["ok"] and "nothing changed" in out["output"] and path.read_text() == CADDYFILE
    assert [p.name for p in tmp_path.iterdir()] == ["Caddyfile"]


def test_caddy_remove_site_apply_backs_up_validates_and_reloads(executor, tmp_path, monkeypatch):
    ran = []

    def runner(argv, timeout):
        ran.append(argv)
        return 0, "Valid configuration\n"

    out, path = _caddy(executor, tmp_path, monkeypatch, "libertyloft.cz, www.libertyloft.cz", "apply", runner)
    assert out["ok"], out
    assert ran == [["docker", "exec", "caddy-proxy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile"],
                   ["docker", "exec", "caddy-proxy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile"]]
    assert "/srv/liberty" not in path.read_text() and "www.libertyloft.cz {" in path.read_text()
    backups = [p for p in tmp_path.iterdir() if p.name.startswith("Caddyfile.bak-")]
    assert len(backups) == 1 and backups[0].read_text() == CADDYFILE and len(backups[0].name) == 26


def test_caddy_remove_site_restores_the_old_file_when_validate_fails(executor, tmp_path, monkeypatch):
    ran = []

    def runner(argv, timeout):
        ran.append(argv[4])
        return (1, "Error: adapting config: unknown directive") if argv[4] == "validate" else (0, "")

    out, path = _caddy(executor, tmp_path, monkeypatch, "libertyloft.cz, www.libertyloft.cz", "apply", runner)
    assert not out["ok"] and out["exit_code"] == 1 and "validate failed" in out["output"]
    assert ran == ["validate"] and path.read_text() == CADDYFILE  # never reloaded, the old file is back
    assert [p.read_text() for p in tmp_path.iterdir() if p.name.startswith("Caddyfile.bak-")] == [CADDYFILE]


def _proc(root, pid, name, swap_kb, cgroup):
    d = root / str(pid)
    d.mkdir()
    swap = "" if swap_kb is None else f"VmSwap:\t{swap_kb:>8} kB\n"
    (d / "status").write_text(f"Name:\t{name}\nVmRSS:\t   1000 kB\n{swap}")
    (d / "cgroup").write_text(cgroup + "\n")


def test_swap_top_lists_the_processes_with_most_swap_and_their_containers(executor, tmp_path, monkeypatch):
    api, kb = "a" * 64, "b" * 64
    _proc(tmp_path, 10, "python", 4000, f"0::/system.slice/docker-{api}.scope")
    _proc(tmp_path, 11, "uvicorn", 1000, f"0::/system.slice/docker-{api}.scope")
    _proc(tmp_path, 12, "java", 9000, f"0::/user.slice/docker-{kb}.scope")
    _proc(tmp_path, 13, "sshd", 500, "0::/system.slice/ssh.service")
    _proc(tmp_path, 14, "idle", 0, "0::/")
    _proc(tmp_path, 15, "kthreadd", None, "0::/")
    (tmp_path / "self").mkdir()
    ran = []

    def runner(argv, timeout):
        ran.append(argv)
        return 0, f"{api} personalos-api-1\n"

    monkeypatch.setattr(ops_runbook, "PROC", str(tmp_path))
    out = executor.handle({"v": 1, "action": "swap_top", "params": {}, "reason": "swap 81 %"}, runner=runner)
    assert out["ok"], out
    assert ran == [["docker", "ps", "--no-trunc", "--format", "{{.ID}} {{.Names}}"]]
    lines = out["output"].splitlines()
    assert lines[:4] == ["9000 java bbbbbbbbbbbb", "4000 python personalos-api-1", "1000 uvicorn personalos-api-1",
                         "500 sshd -"]
    assert "5000 personalos-api-1" in lines and lines[-1] == "total 14500 kB swap in 4 processes"
    assert not executor.handle({"v": 1, "action": "swap_top", "params": {"top": "99"}, "reason": "r"})["ok"]


def _secret_tree(tmp_path):
    files = {
        "opt/app/.env": "API_TOKEN=leakedvalue111\n",
        "opt/app/.env.production": "DB_PASSWORD=leakedvalue222\n",
        "opt/app/config.yml": "token: leakedvalue333\n",
        "opt/app/db.sql.bak": "INSERT leakedvalue444\n",
        "opt/overlay2/l/.env": "IN_OVERLAY=leakedvalue555\n",
        "home/u/.docker/config.json": '{"auths": {"ghcr.io": {"auth": "leakedvalue666"}}}\n',
        "home/u/config.json": "{}\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
        os.utime(tmp_path / rel, (1760000000, 1760000000))
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(CADDYFILE.replace("\troot * /srv/liberty", "\tbasic_auth { u leakedvalue777 }"))
    return caddyfile


def test_secret_files_scan_shows_path_owner_mtime_and_never_a_files_content(executor, tmp_path, monkeypatch):
    caddyfile = _secret_tree(tmp_path)
    roots = (str(tmp_path / "home"), str(tmp_path / "opt"), str(tmp_path / "missing"))
    monkeypatch.setattr(ops_runbook, "SCAN_ROOTS", roots)
    monkeypatch.setattr(ops_runbook, "CADDY_SCAN", {"caddy-proxy": str(caddyfile), "kb-caddy-1": str(tmp_path / "no")})
    opened = []
    real_open = open

    def spy(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    with monkeypatch.context() as m:
        m.setattr("builtins.open", spy)
        out = executor.handle({"v": 1, "action": "secret_files_scan", "params": {}, "reason": "T-1295 scan"},
                              runner=lambda argv, t: pytest.fail("no program runs"))
    assert out["ok"], out
    assert opened == [str(caddyfile), str(tmp_path / "no")]  # only the Caddyfiles, never a file that was found
    text = out["output"]
    assert "leakedvalue" not in text and "basic_auth" not in text and "reverse_proxy" not in text
    me, day = pwd.getpwuid(os.getuid()).pw_name, time.strftime("%Y-%m-%d", time.localtime(1760000000))
    found = [f"{tmp_path}/{rel} {me} {day}" for rel in
             ("home/u/.docker/config.json", "opt/app/.env", "opt/app/.env.production", "opt/app/db.sql.bak")]
    assert [line for line in text.splitlines() if line.startswith(f"{tmp_path}/")] == found
    assert "# 4 found; 0 directories not readable" in text
    lines = text.splitlines()
    i = lines.index(f"# caddy-proxy ({caddyfile}): 4 site blocks")
    assert lines[i + 1:i + 5] == ["personalos.obseum.cz  import internal: yes",
                                  "libertyloft.cz, www.libertyloft.cz  import internal: no",
                                  "www.libertyloft.cz  import internal: no",
                                  "kb.obseum.cz, kb2.obseum.cz  import internal: no"]
    assert f"# kb-caddy-1 ({tmp_path / 'no'}): not read (FileNotFoundError)" in lines
    # nothing from the caller: no roots, no find expression
    bad = executor.handle({"v": 1, "action": "secret_files_scan", "params": {"roots": "/"}, "reason": "r"})
    assert not bad["ok"] and bad["error"].startswith("refused")
    short = ops_runbook.secret_files(roots, limit=2)
    assert short["total"] == 4 and short["lines"] == found[:2] and not short["cut"]


def test_secret_files_scan_output_is_not_cut_at_8k_by_the_api(app, monkeypatch, tmp_path):
    conn, sre = app["conn"], app["sre"]
    _socket(monkeypatch, tmp_path)
    listing = "".join(f"/opt/server/app{i}/.env drosko 2026-10-01\n" for i in range(1000))
    out = ops_runbook.run(conn, Ctx(sre, via="mcp"), "secret_files_scan", {}, "T-1295",
                          sender=lambda *a: {"ok": True, "exit_code": 0, "output": listing})
    assert out["ok"] and not out["truncated"] and out["output"].count("\n") == 1000


def test_the_executor_cuts_long_output_and_survives_a_crashing_runner(executor):
    def big(argv, timeout):
        return 0, "x" * (executor.MAX_OUTPUT * 2)

    out = executor.handle({"v": 1, "action": "docker_ps", "params": {}, "reason": "r"}, runner=big)
    assert len(out["output"]) == executor.MAX_OUTPUT

    def boom(argv, timeout):
        raise RuntimeError("docker is gone")

    out = executor.handle({"v": 1, "action": "docker_ps", "params": {}, "reason": "r"}, runner=boom)
    assert not out["ok"] and "docker is gone" in out["error"]
