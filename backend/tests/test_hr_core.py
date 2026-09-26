"""Head of People wired to the real core: actors, tasks, runs, budget, API and MCP."""

import json
from datetime import timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, mcp_server, tasks
from pos.config import Settings
from pos.core import Ctx, now_iso
from pos.db import connect, migrate
from pos.hr import HRPolicy, service
from pos.hr.platform import CorePlatform
from pos.main import create_app


@pytest.fixture(autouse=True)
def no_codex(monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "t.db")
    migrate(c)
    actors.ensure_builtin(c)
    service.ensure_hr_agent(c)
    yield c
    c.close()


def new_agent(conn, name, purpose, created_by=None, lifetime="long_lived"):
    aid = conn.execute("INSERT INTO actors (kind, name, created_at) VALUES ('agent', ?, ?)",
                       (name, now_iso())).lastrowid
    service.register_agent(conn, aid, purpose=purpose, lifetime=lifetime, created_by=created_by)
    conn.commit()
    return aid


def test_hr_agent_is_a_system_member(conn):
    hr_id = service.ensure_hr_agent(conn)
    assert service.ensure_hr_agent(conn) == hr_id
    agents = {a.name: a for a in CorePlatform(conn, Ctx(hr_id)).list_agents()}
    assert agents["Head of People"].system and agents["Nexus"].system and agents["Executive Assistant"].system


def test_stats_come_from_tasks_runs_and_budget(conn):
    owner = Ctx(actors.owner_id(conn))
    mail = new_agent(conn, "Head of Customer Success", "Sorts incoming emails")
    agent = Ctx(mail, via="mcp")
    refs = [tasks.create(conn, owner, {"title": f"t{i}", "assignee": "Head of Customer Success"})["id"] for i in range(4)]
    for tid in refs[:3]:
        tasks.claim(conn, agent, tid)
    tasks.intervene(conn, owner, refs[1], "wrong folder")
    for tid in refs[:3]:
        tasks.complete(conn, agent, tid)
    tasks.review(conn, owner, refs[0], accept=True)
    tasks.review(conn, owner, refs[1], accept=True)
    tasks.review(conn, owner, refs[2], accept=False, comment="missed attachments")
    conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'error', ?)",
                 (mail, now_iso()))
    conn.execute(
        """INSERT INTO budget_runs (at, agent_id, source, input_tokens, cached_input_tokens, output_tokens,
           reasoning_output_tokens, billable_tokens) VALUES (?, ?, 'exec', 0, 0, 0, 0, 1500)""",
        (now_iso(), str(mail)),
    )
    conn.commit()

    now = service.utcnow() + timedelta(minutes=1)
    s = CorePlatform(conn, owner).agent_stats(str(mail), now - timedelta(days=14), now)
    assert (s.tasks_completed, s.tasks_completed_unassisted, s.tasks_returned) == (3, 1, 1)
    assert (s.tasks_failed, s.tasks_open, s.owner_interventions, s.tokens_used) == (1, 2, 1, 1500)


def test_daily_review_archives_idle_agents_only(conn):
    owner = Ctx(actors.owner_id(conn))
    idle = new_agent(conn, "Old helper", "Summarises meeting notes")
    busy = new_agent(conn, "Software Engineer", "Fixes GitHub issues")
    tasks.create(conn, owner, {"title": "Fix login", "assignee": "Software Engineer"})
    conn.commit()

    later = service.utcnow() + timedelta(days=20)
    dry = service.daily_review(conn, owner, apply=False, now=later)
    assert [(p["agent"], p["kind"]) for p in dry["proposals"]] == [("Old helper", "archive_idle")]
    assert actors.get(conn, idle)["archived_at"] is None

    report = service.daily_review(conn, owner, now=later)
    assert report["proposals"][0]["applied"]
    assert actors.get(conn, idle)["archived_at"] is not None
    assert actors.get(conn, busy)["archived_at"] is None
    assert actors.find_by_name(conn, "Nexus") is not None

    from pos import versioning

    assert [h["action"] for h in versioning.history(conn, "actor", idle)] == ["archive"]
    service.restore(conn, owner, idle)
    assert actors.get(conn, idle)["archived_at"] is None
    assert [h["action"] for h in versioning.history(conn, "actor", idle)][-1] == "unarchive"
    assert versioning.history(conn, "hr_profile", idle)[0]["action"] == "create"


def test_review_is_owner_or_hr_only(conn):
    nexus = Ctx(actors.find_by_name(conn, "Nexus")["id"])
    with pytest.raises(Exception, match="only the owner or the Head of People"):
        service.daily_review(conn, nexus)
    assert service.daily_review(conn, nexus, apply=False)["active_before"] >= 4


def test_merge_task_goes_to_hr_agent(conn):
    owner = Ctx(actors.owner_id(conn))
    new_agent(conn, "Mail A", "Sorts incoming emails")
    new_agent(conn, "Mail B", "Sort incoming email")
    report = service.daily_review(conn, owner)
    [task_id] = report["task_ids"]
    t = tasks.get(conn, owner, int(task_id))
    assert t["title"].startswith("Sloučit agenta") and t["assignee_name"] == "Head of People"


def test_weekly_report_is_a_task_for_the_owner(conn):
    owner = Ctx(actors.owner_id(conn))
    first = service.weekly_report(conn, owner)
    t = tasks.get(conn, owner, int(first["report_task_id"]))
    assert t["title"].startswith("HR přehled") and t["assignee_name"] == actors.OWNER_NAME
    assert "| Dokončené úkoly | 0 | – |" in t["notes"]
    second = service.weekly_report(conn, owner)
    assert "| Dokončené úkoly | 0 | 0 |" in tasks.get(conn, owner, int(second["report_task_id"]))["notes"]


def test_admit_agent_limits(conn):
    policy = HRPolicy(max_active_agents=2)
    dev = new_agent(conn, "Software Engineer", "Fixes GitHub issues")
    dev_ctx = Ctx(dev, via="mcp")
    assert service.admit_agent(conn, dev_ctx, name="x", purpose="y", policy=policy) == {"allowed": True}

    new_agent(conn, "Head of Customer Success", "Sorts incoming emails", created_by=dev)
    reuse = service.admit_agent(conn, dev_ctx, name="Mail 2", purpose="sort incoming email", policy=policy)
    assert (reuse["allowed"], reuse["decision"], reuse["limit"]) == (False, "reuse", "active_limit")

    ask = service.admit_agent(conn, dev_ctx, name="Research", purpose="market research", policy=policy)
    assert ask["decision"] == "ask_owner" and ask["approval_id"]

    new_agent(conn, "Helper", "Books travel", created_by=dev)
    defer = service.admit_agent(conn, dev_ctx, name="z", purpose="translate", policy=HRPolicy())
    assert (defer["limit"], defer["decision"]) == ("daily_limit", "defer")
    # The owner has no daily limit.
    owner = Ctx(actors.owner_id(conn))
    assert service.admit_agent(conn, owner, name="z", purpose="translate") == {"allowed": True}


def test_http_api(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        assert client.get("/api/hr").json()["ratings"] == []  # a GET never runs a review
        client.post("/api/hr/review")
        overview = client.get("/api/hr").json()
        names = {r["name"] for r in overview["ratings"]}
        assert "Head of People" in names and not names & {"Nexus", "Knowledge agent", "Deployer"}  # services: no reviews
        assert client.post("/api/hr/review", params={"apply": "false"}).json()["proposals"] == []
        assert client.post("/api/hr/weekly").json()["report_task_id"]
        assert client.put("/api/hr/agents/999/profile", json={"purpose": "x"}).status_code == 404
        assert client.post("/api/hr/admit", json={"name": "n", "purpose": "p"}).json() == {"allowed": True}
        assert client.get("/api/hr").json()["last_weekly"]


def _call(result):
    assert not result.is_error, result.content
    sc = result.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_mcp_tools(tmp_path):
    db = tmp_path / "m.db"
    conn = connect(db)
    migrate(conn)
    agent = actors.ensure_builtin(conn)["Knowledge agent"]
    service.ensure_hr_agent(conn)
    conn.close()
    server = mcp_server.build(db, default_actor=lambda c: agent)

    async def scenario():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"hr_overview", "hr_review", "hr_admit_agent", "hr_weekly_report"} <= names
            assert _call(await c.call_tool("hr_admit_agent", {"name": "n", "purpose": "p"}))["allowed"] is True
            assert _call(await c.call_tool("hr_review", {}))["proposals"] == []
            refused = await c.call_tool("hr_review", {"apply": True})
            assert refused.is_error

    anyio.run(scenario)


def test_schedule_runs_daily_then_weekly_once(conn):
    from datetime import datetime

    from pos.core import TZ
    from pos.hr import schedule

    monday_early = datetime(2026, 9, 28, 5, 0, tzinfo=TZ)
    monday = datetime(2026, 9, 28, 7, 0, tzinfo=TZ)
    tuesday = datetime(2026, 9, 29, 7, 0, tzinfo=TZ)
    assert schedule.due(conn, monday_early) == []
    assert schedule.run_due(conn, monday) == ["daily", "weekly"]
    assert schedule.run_due(conn, monday) == []
    assert schedule.run_due(conn, tuesday) == ["daily"]


def test_approved_limit_raise_lets_the_agent_in(conn):
    from pos import approvals, settings_store
    from pos.hr.policy import SETTING_MAX_ACTIVE, current

    owner = Ctx(actors.owner_id(conn))
    settings_store.put(conn, owner, SETTING_MAX_ACTIVE, 1)
    new_agent(conn, "Head of Customer Success", "Sorts incoming emails")
    pm = Ctx(new_agent(conn, "COO", "Splits team work by role"), via="mcp")
    ask = service.admit_agent(conn, pm, name="Research", purpose="market research")
    assert ask["allowed"] is False and ask["decision"] == "ask_owner"
    approvals.decide(conn, owner, ask["approval_id"], True)
    assert current(conn).max_active_agents > 1  # room for one more
    assert service.admit_agent(conn, pm, name="Research", purpose="market research") == {"allowed": True}
    from pos import versioning

    row = conn.execute("SELECT id FROM settings WHERE key = ?", (SETTING_MAX_ACTIVE,)).fetchone()
    assert versioning.history(conn, "setting", row["id"])[-1]["action"] == "raise_agent_limit"


def test_hr_never_archives_a_role_agent_from_git(conn, tmp_path, monkeypatch):
    (tmp_path / "agents" / "head-of-customer-success").mkdir(parents=True)
    monkeypatch.setenv("POS_AGENTS_REPO_DIR", str(tmp_path / "agents"))
    owner = Ctx(actors.owner_id(conn))
    mail = new_agent(conn, "Head of Customer Success", "Sorts incoming emails")
    later = service.utcnow() + timedelta(days=20)
    service.daily_review(conn, owner, now=later)
    assert actors.get(conn, mail)["archived_at"] is None
    proposal = conn.execute("SELECT title FROM tasks WHERE title LIKE 'Návrh HR: archivovat Head of Customer Success%'").fetchone()
    assert proposal is not None


def test_weekly_report_says_which_shared_tools_help(conn):
    from pos import comments

    owner = Ctx(actors.owner_id(conn))
    dev = new_agent(conn, "Software Engineer", "Fixes GitHub issues")
    for ok in (1, 1, 0):
        conn.execute("INSERT INTO tool_usage (tool, actor_id, at, ok) VALUES ('shared/pdf-to-text', ?, ?, ?)",
                     (dev, now_iso(), ok))
    conn.commit()
    report = service.weekly_report(conn, owner)
    assert report["tool_usage"][0] == {"tool": "shared/pdf-to-text", "uses": 3, "failed": 1, "by": ["Software Engineer"]}
    notes = [a["body"] for a in comments.list_for(conn, owner, int(report["report_task_id"]))]
    assert any("shared/pdf-to-text: 3× (1 failed)" in n for n in notes)


def test_claude_usage_counts_for_hr_with_cache_reads_and_cost_per_task(conn):
    """A Software Engineer on Claude: engine usage (not budget runs) is what HR and the task show."""
    from pos import engines
    from pos.hr.metrics import rate_agent

    owner = Ctx(actors.owner_id(conn))
    dev = new_agent(conn, "Software Engineer", "Improves PersonalOS")
    t = tasks.create(conn, owner, {"title": "Fix the flaky test", "assignee": "Software Engineer"})
    for cost in (0.5, 1.5):  # two runs on the same task (one handed back, one done)
        run_id = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, engine) "
                              "VALUES (?, ?, 'task', 'ok', ?, 'claude')", (dev, t["id"], now_iso())).lastrowid
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        engines.record_claude(conn, row, json.dumps({
            "type": "result", "is_error": False, "result": "ok", "total_cost_usd": cost,
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_creation_input_tokens": 1000,
                      "cache_read_input_tokens": 20000}}))

    now = service.utcnow() + timedelta(minutes=1)
    s = CorePlatform(conn, owner).agent_stats(str(dev), now - timedelta(days=7), now)
    assert (s.tokens_used, s.cost_usd, s.tasks_worked) == (2300, 2.0, 1)
    r = rate_agent(s)
    assert (r.tokens_used, r.tokens_per_task, r.cost_per_task) == (2300, 2300, 2.0)

    usage = tasks.get(conn, owner, t["id"])["usage"]
    assert usage == {"runs": 2, "tokens": 2300, "cache_read_tokens": 40000, "cost_usd": 2.0}
