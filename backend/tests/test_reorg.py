"""The 2026-09 reorganisation into a company (docs/REORG.md): agents from git with
their leads, schedules and worker profiles; the QA review gate before the
deployer promotes agent/dev; pos.reorg moving an install with the old agents
onto the new structure without deleting anything."""

import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, agents_code, deploy_review, org, reorg, roles, routing, schedules, selfdeploy, tasks
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def company(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "1")
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="t" * 32, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    yield {"conn": conn, "client": client, "data": tmp_path}
    conn.close()
    client.__exit__(None, None, None)


def _lead(conn, name):
    row = actors.find_by_name(conn, name)
    return actors.get(conn, row["reports_to"])["name"] if row and row["reports_to"] else None


def test_the_company_comes_from_the_files_with_leads_routines_and_profiles(company):
    conn = company["conn"]
    for name, lead in (("CEO", "Owner"), ("Chief of Staff", "CEO"), ("Executive Assistant", "Chief of Staff"),
                       ("CTO", "CEO"), ("Software Engineer", "CTO"), ("Hlídač", "SRE"), ("QA Reviewer", "CTO"),
                       ("Performance Coach", "Head of People"), ("Content & Brand", "Head of Growth"),
                       ("Home Assistant Specialist", "CTO"), ("Legal & Compliance", "CEO")):
        assert _lead(conn, name) == lead, name
    # routines come from agent.json once, as the owner's team schedules
    mine = {s["name"] for s in schedules.list_schedules(conn, actor_id=actors.find_by_name(conn, "CEO")["id"])}
    assert {"CEO: pondělní plán", "CEO: podklady pro board"} <= mine
    assert not schedules.list_schedules(conn, actor_id=actors.find_by_name(conn, "Legal & Compliance")["id"])
    assert agents_code.is_dormant("Legal & Compliance") and not agents_code.is_dormant("CEO")
    # each agent's worker settings come from its file (the pool serves many agents)
    hl = agents_code.worker_profile("Hlídač")
    assert hl["effort"] == "low" and "incident_close" in hl["pos_tools"] and hl["max_steps"] == 20
    assert agents_code.worker_profile("Software Engineer")["workdir"] == "/work/PersonalOS"
    # the routing rules name the new roles
    rules = {r["name"]: r["assignee"] for r in routing.list_rules(conn)}
    assert rules[f"GitHub issue labelled agent → {roles.ENGINEER}"] == "Software Engineer"
    assert rules["New e-mail → Customer Success triage"] == "Head of Customer Success"
    assert rules["Sentinel incident → Hlídač"] == "Hlídač" and rules["Grafana alert → Hlídač"] == "Hlídač"


def test_worker_me_serves_the_profile(company):
    conn, client = company["conn"], company["client"]
    hl = actors.find_by_name(conn, "Hlídač")
    key = agents.rotate_key(conn, Ctx(actors.owner_id(conn)), hl["id"])
    me = client.get("/api/worker/me", headers={"Authorization": f"Bearer {key}"}).json()
    assert me["profile"]["effort"] == "low" and me["profile"]["claude_tools"] == " "


def test_profile_workdir_only_under_work_or_repos(tmp_path):
    base = tmp_path / "agents"
    (base / "x").mkdir(parents=True)
    (base / "x" / "agent.json").write_text('{"name": "X", "effort": "turbo", "profile": {"workdir": "/etc", '
                                           '"pos_tools": "get_task", "unknown": 1}}', encoding="utf-8")
    assert agents_code.worker_profile("X", base) == {"pos_tools": "get_task"}


def test_the_worker_prefers_the_agents_profile(monkeypatch, tmp_path):
    pytest.importorskip("pos_worker")
    from pos_worker.__main__ import codex_effort, session_workdir, setting
    from pos_worker.tools import pos_tools

    monkeypatch.setenv("WORKER_CLAUDE_EFFORT", "medium")
    me = {"profile": {"effort": "low", "pos_tools": "get_task"}, "pos_tools": ["get_task", "create_task", "chat_send"]}
    assert setting(me, "effort", "WORKER_CLAUDE_EFFORT") == "low"
    assert setting({}, "effort", "WORKER_CLAUDE_EFFORT") == "medium"
    assert codex_effort(me) == ['model_reasoning_effort="low"'] and codex_effort({}) == []
    shown, hidden = pos_tools(me)
    assert "get_task" in shown and "create_task" in hidden
    assert session_workdir({"profile": {"workdir": str(tmp_path / "w")}}, "/work") == str(tmp_path / "w")
    assert session_workdir({}, "/work") == "/work"


def test_the_deploy_review_gate(company):
    conn = company["conn"]
    deployer = Ctx(actors.find_by_name(conn, "Deployer")["id"], via="deployer")
    sha = "a" * 40
    out = deploy_review.ask(conn, deployer, sha, base="b" * 40, author="Software Engineer", subject="Fix", commits=1)
    assert out["status"] == "pending" and out["task"]
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (tasks.parse_id(out["task"]),)).fetchone()
    qa = actors.find_by_name(conn, "QA Reviewer")
    assert t["assignee_id"] == qa["id"] and t["topic"] == "review"
    assert deploy_review.ask(conn, deployer, sha)["status"] == "pending"          # one task per sha
    se = actors.find_by_name(conn, "Software Engineer")
    with pytest.raises(Forbidden):
        deploy_review.decide(conn, Ctx(se["id"]), sha, "approve")               # nobody approves their own
    with pytest.raises(tasks.Invalid):
        deploy_review.decide(conn, Ctx(qa["id"]), sha, "return")                # a return says what to change
    assert deploy_review.decide(conn, Ctx(qa["id"]), sha[:12], "approve", "OK")["status"] == "approved"
    assert deploy_review.ask(conn, deployer, sha)["status"] == "approved"
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (t["id"],)).fetchone()["status"] == "done"


def _git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_the_deployer_waits_for_the_review_when_it_is_required(company, tmp_path):
    conn, client = company["conn"], company["client"]
    key = agents.rotate_key(conn, Ctx(actors.owner_id(conn)), actors.find_by_name(conn, "Deployer")["id"])
    rep = selfdeploy.Reporter("http://testserver", key, http=client)
    origin, work, deploy = tmp_path / "origin.git", tmp_path / "work", tmp_path / "deploy"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", str(origin), str(work))
    (work / "app.txt").write_text("v1")
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "push", "origin", "HEAD:main")
    _git(work, "checkout", "-q", "-b", "agent/dev")
    _git(work, "worktree", "add", "--detach", str(deploy), "main")
    (work / "app.txt").write_text("v2")
    _git(work, "commit", "-am", "Improve\n\nAgent: Software Engineer")
    kw = dict(source="agent/dev", remote="origin", target="main", test_cmd=f'"{sys.executable}" -c "pass"',
              up_cmd="", health_url=None, require_review=True)
    res = selfdeploy.promote_tick(deploy, rep, **kw)
    assert res.status == "nothing" and res.stage == "review"                   # waits, main untouched
    tip = _git(work, "rev-parse", "HEAD")
    qa = actors.find_by_name(conn, "QA Reviewer")
    deploy_review.decide(conn, Ctx(qa["id"]), tip, "approve", "OK")
    assert selfdeploy.promote_tick(deploy, rep, **kw).status == "ok"


def _legacy(conn, data_dir):
    """An install from before the reorganisation: the old names, their tasks, rules and schedules."""
    owner = Ctx(actors.owner_id(conn))
    conn.execute("UPDATE actors SET name = 'Assistant' WHERE name = 'Executive Assistant'")
    conn.execute("UPDATE actors SET name = 'Project manager' WHERE name = 'COO'")
    conn.execute("UPDATE actors SET name = 'HR agent' WHERE name = 'Head of People'")
    old = {}
    for name in ("Dev agent", "Mail agent", "Monitor", "Agent coach", "Asistent vedení", "Community agent"):
        old[name] = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived",
                                        permissions=["tasks:read", "tasks:claim", "messages:send"],
                                        data_dir=data_dir)["agent"]["id"]
    pm = conn.execute("SELECT id FROM actors WHERE name = 'Project manager'").fetchone()["id"]
    t = tasks.create(conn, owner, {"title": "Fix TZ", "assignee": {"type": "agent", "id": old["Dev agent"]},
                                   "notes": "x", "definition_of_done": "y", "status": "next"})
    done = tasks.create(conn, owner, {"title": "Done, waits for review", "notes": "x", "definition_of_done": "y",
                                      "assignee": {"type": "agent", "id": old["Dev agent"]}, "status": "next"})
    conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (done["id"],))
    schedules.create(conn, owner, {"name": "Daily standup", "schedule": "weekdays 08:30", "visibility": "team",
                                   "assignee": {"type": "agent", "id": pm}})
    rid = next(r["id"] for r in routing.list_rules(conn) if r["assignee"] == roles.ENGINEER
               and r["name"].startswith("GitHub issue"))
    routing.update_rule(conn, owner, rid, {"name": "GitHub issue labelled agent → Dev agent", "assignee": "Dev agent"})
    routing.create_rule(conn, owner, {"name": "Invoice e-mail → payment task for Nexus", "source": "gmail",
                                      "match": {"text_regex": "faktur"}, "assignee": "Nexus", "priority": 1,
                                      "enabled": False})
    conn.commit()
    return old, t, done


def test_reorg_moves_an_old_install_and_deletes_nothing(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="t" * 32, scheduler=False)
    client = TestClient(create_app(settings))  # POS_AGENTS_AS_CODE=0: only the platform's own members
    client.__enter__()
    conn = connect(settings.db_path)
    old, t, done = _legacy(conn, tmp_path)
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "1")

    dry = reorg.run(conn, tmp_path, apply=False)
    assert dry["create"] and actors.find_by_name(conn, "Dev agent") is not None     # a dry run writes nothing
    assert actors.find_by_name(conn, "CEO") is None

    report = reorg.run(conn, tmp_path, apply=True)
    assert actors.find_by_name(conn, "Executive Assistant") is not None           # renamed in place
    for name, new in (("Dev agent", "Software Engineer"), ("Monitor", "Hlídač"), ("Mail agent",
                                                                                  "Head of Customer Success")):
        row = actors.get(conn, old[name])
        assert row["archived_at"] and row["name"] == name                          # archived, not deleted
        assert actors.find_by_name(conn, new) is not None
    assert conn.execute("SELECT archived_at FROM actors WHERE name = 'Project manager'").fetchone()[0]
    se = actors.find_by_name(conn, "Software Engineer")
    assert tasks.get(conn, Ctx(actors.owner_id(conn)), t["id"])["assignee_id"] == se["id"]  # work moved
    moved = tasks.get(conn, Ctx(actors.owner_id(conn)), done["id"])
    assert moved["assignee_id"] == se["id"] and moved["status"] == "review"      # not redone by the successor
    rules = routing.list_rules(conn)
    assert not [r for r in rules if r["assignee"] in roles.LEGACY]
    assert any(r["name"] == routing.INVOICE_RULE and r["assignee"] == "CFO" and r["enabled"] for r in rules)
    assert org.chart(conn) and _lead(conn, "Access manager") == "CEO" and _lead(conn, "COO") == "CEO"
    assert not [s for s in schedules.list_schedules(conn) if s["name"] == "Daily standup"
                and s["assignee_name"] == "Project manager"]
    assert "Daily standup" in {s["name"] for s in schedules.list_schedules(
        conn, actor_id=actors.find_by_name(conn, "COO")["id"])}                  # the COO's own standup
    assert report["archive"]
    again = reorg.run(conn, tmp_path, apply=True)                                  # idempotent
    assert not any(again[k] for k in ("create", "org", "routing", "schedules", "tasks", "archive"))
    conn.close()
    client.__exit__(None, None, None)
