"""The Chief of Staff (Chief of Staff): the week packet, goals, the weekly job and the meeting."""

import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, chat, goals, integrations, mcp_server, scheduler, tasks, weekly, weekly_packet
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate
from pos.main import create_app

WEEK = "2026-W10"  # Mon 2 Mar - Sun 8 Mar 2026, a fixed past week


@pytest.fixture(autouse=True)
def no_outside(monkeypatch):
    monkeypatch.delenv("POS_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("POS_REPORT_REPOS", raising=False)
    monkeypatch.delenv("POS_REPORT_KNOWLAGE", raising=False)
    monkeypatch.delenv("POS_PUBLIC_URL", raising=False)
    monkeypatch.setattr(weekly_packet, "http_get", None)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "w.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    c.execute("UPDATE actors SET name = 'David' WHERE is_owner = 1")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


def _cos(conn, me, tmp_path) -> Ctx:
    made = agents.create_agent(conn, me, name=weekly.NAME, purpose="weekly report and meeting",
                               lifetime="long_lived", data_dir=tmp_path,
                               permissions=["tasks:read", "tasks:write", "tasks:claim", "messages:send",
                                            "approvals:request"])
    aid = made["agent"]["id"]
    conn.execute("UPDATE actors SET role = ? WHERE id = ?", (weekly.ROLE, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


def _at(day: int, hour: int = 10) -> str:
    """A UTC timestamp on day `day` (0 = Monday) of WEEK."""
    start, _ = weekly_packet.bounds(WEEK)
    return (start + timedelta(days=day, hours=hour)).isoformat(timespec="seconds")


def _done(conn, me, title, day, **kw):
    t = tasks.create(conn, me, {"title": title, "status": "next", **kw})
    conn.execute("UPDATE tasks SET status = 'done', completed_at = ?, created_at = ? WHERE id = ?",
                 (_at(day), _at(day, 8), t["id"]))
    return t


# ------------------------------------------------------------------ the packet

def test_week_bounds_are_prague_mondays():
    start, end = weekly_packet.bounds("2026-W10")
    assert start.isoformat() == "2026-03-01T23:00:00+00:00"  # Monday 00:00 CET
    assert end - start == timedelta(days=7)
    assert weekly_packet.previous_week("2026-W01") == "2025-W52"
    with pytest.raises(ValueError):
        weekly_packet.parse_week("next week")


def test_packet_counts_tasks_agents_dev_and_deltas(conn, me, tmp_path):
    from pos import projects

    proj = projects.create(conn, me, name="Web shop", goal="sell", channel=False)
    dev = agents.create_agent(conn, me, name="Software Engineer", purpose="code", lifetime="long_lived",
                              data_dir=tmp_path)["agent"]
    # This week: 3 done (one high priority in a project, one by the agent), 1 waiting, 1 overdue.
    big = _done(conn, me, "Launch the checkout", 1, priority=1, project=proj["id"], estimate_min=240)
    _done(conn, me, "Fix typo", 1, priority=3)
    agent_task = _done(conn, me, "Refactor API", 3, assignee={"type": "agent", "id": dev["id"]}, topic="api")
    tasks.create(conn, me, {"title": "Chat answer", "status": "done", "topic": "chat"})  # excluded
    conn.execute("UPDATE tasks SET completed_at = ? WHERE topic = 'chat'", (_at(2),))
    w = tasks.create(conn, me, {"title": "Waiting for the bank", "status": "waiting"})
    tasks.create(conn, me, {"title": "Overdue invoice", "status": "next", "deadline": "2026-01-01"})
    # Last week (same weekday span): 1 done.
    _done(conn, me, "Old thing", -6)
    # The agent's runs and cost this week: 3 ok, 1 error, $1.50 for 1 accepted task.
    for i, st in enumerate(("ok", "ok", "ok", "error")):
        conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'exec', ?, ?)",
                     (dev["id"], agent_task["id"], st, _at(2, 9 + i)))
    conn.execute("INSERT INTO engine_usage (at, engine, actor_id, task_id, input_tokens, output_tokens, cost_usd) "
                 "VALUES (?, 'claude', ?, ?, 1000, 500, 1.5)", (_at(2), dev["id"], agent_task["id"]))
    conn.execute("INSERT INTO deploys (old_sha, new_sha, status, commits, created_at) VALUES ('a', 'b', 'ok', 4, ?)",
                 (_at(4),))
    conn.execute("INSERT INTO deploys (old_sha, new_sha, status, stage, created_at) "
                 "VALUES ('b', 'c', 'reverted', 'tests', ?)", (_at(4, 12),))
    conn.commit()

    _, end = weekly_packet.bounds(WEEK)
    p = weekly_packet.build(conn, WEEK, now=end + timedelta(days=30))
    assert p["week"] == WEEK and p["period"] == {**p["period"], "start": "2026-03-02", "end": "2026-03-08",
                                                 "partial": False}
    t = p["tasks"]
    assert (t["done"], t["prev"]["done"]) == (3, 1)
    assert t["waiting"] == 1 and t["overdue"] == 1 and t["overdue_list"][0]["title"] == "Overdue invoice"
    assert [d["done"] for d in t["done_by_day"]] == [0, 2, 0, 1, 0, 0, 0]
    assert t["done_by_day"][1]["day"] == "Út"
    split = {b["name"]: b["done"] for b in t["by_project"]}
    assert split["Web shop"] == 1 and split["#api"] == 1
    assert t["highlights"][0]["ref"] == big["ref"]  # the highest priority, largest item first
    who = {a["name"]: a for a in t["by_assignee"]}
    assert who["Software Engineer"]["kind"] == "agent" and who["Software Engineer"]["done"] == 1
    assert t["waiting_list"][0]["ref"] == w["ref"]

    a = p["agents"]
    assert (a["runs"], a["ok"], a["errors"], a["success_rate"]) == (4, 3, 1, 0.75)
    assert a["cost_usd"] == 1.5 and a["accepted"] == 1 and a["cost_per_accepted"] == 1.5
    assert a["per_agent"][0]["name"] == "Software Engineer" and a["per_agent"][0]["tokens"] == 1500

    k = p["kpis"]
    assert k["tasks_done"] == {"value": 3, "prev": 1, "delta": 2}
    assert k["deploys"]["value"] == 1
    assert p["dev"]["available"] is False and "GitHub" in p["dev"]["note"]
    assert p["incidents"]["failed_deploys"][0]["status"] == "reverted"
    assert p["communication"]["available"] is False
    assert "hotovo 3" in weekly_packet.summary_line(p)
    json.dumps(p)  # compact and serialisable


def test_packet_reads_github_and_knowlage_when_configured(conn, monkeypatch):
    monkeypatch.setenv("POS_GITHUB_TOKEN", "t")
    monkeypatch.setenv("POS_REPORT_KNOWLAGE", "1")
    s, u = weekly_packet.bounds(WEEK)
    calls = []

    def fake(url, headers, params):
        calls.append(url)
        if url.endswith("/orgs/ObseumEU/repos"):
            return [{"full_name": "ObseumEU/PersonalOS", "pushed_at": "2026-03-05T10:00:00Z"},
                    {"full_name": "ObseumEU/old", "pushed_at": "2025-01-01T00:00:00Z"}]
        if url.endswith("/commits"):
            if params["since"] >= s.isoformat(timespec="seconds"):
                return [{"parents": [{}], "author": {"login": "dev"}}, {"parents": [{}, {}], "author": None},
                        {"parents": [{}], "author": {"login": "david"}}]
            return [{"parents": [{}]}]
        if url.endswith("/api/documents"):
            return [{"origin": "mail", "channel": "acme.cz", "addedAt": _at(1)},
                    {"origin": "mail", "channel": "acme.cz", "addedAt": _at(2)},
                    {"origin": "discord", "channel": "general", "addedAt": _at(3)},
                    {"origin": "mail", "channel": "old.cz", "addedAt": "2025-01-01T00:00:00Z"}]
        raise AssertionError(url)

    monkeypatch.setattr(weekly_packet, "http_get", fake)
    p = weekly_packet.build(conn, WEEK, now=u)
    assert p["dev"]["available"] and p["dev"]["commits"] == 2 and p["dev"]["merges"] == 1
    assert p["dev"]["repos"] == [{"repo": "ObseumEU/PersonalOS", "commits": 2, "merges": 1, "prev": 1,
                                  "authors": ["dev", "david"]}]
    assert p["kpis"]["commits"] == {"value": 2, "prev": 1, "delta": 1}
    assert not any("ObseumEU/old" in c for c in calls)  # not pushed this week: not read
    c = p["communication"]
    assert c["available"] and c["items"] == 3 and c["by_origin"] == {"mail": 2, "discord": 1}
    assert c["top_channels"][0] == {"origin": "mail", "channel": "acme.cz", "items": 2}


def test_packet_fails_soft_when_github_breaks(conn, monkeypatch):
    monkeypatch.setenv("POS_GITHUB_TOKEN", "t")

    def boom(url, headers, params):
        raise RuntimeError("503 from GitHub")

    monkeypatch.setattr(weekly_packet, "http_get", boom)
    p = weekly_packet.build(conn, WEEK, now=weekly_packet.bounds(WEEK)[1])
    assert p["dev"]["available"] is False and "503" in p["dev"]["note"]


# ------------------------------------------------------------------ goals

def test_goals_crud_links_and_progress(conn, me):
    g = goals.create(conn, me, {"title": "Ship the web shop", "why": "Revenue", "target": "10 orders by June",
                                "owner": "David", "due": "2026-06-30"})
    assert g["status"] == "active" and g["owner_name"] == "David" and g["progress_effective"] == 0
    child = goals.create(conn, me, {"title": "Checkout works", "parent_id": g["id"]})
    assert child["parent_title"] == "Ship the web shop"
    a = tasks.create(conn, me, {"title": "Payment gateway", "status": "next"})
    b = tasks.create(conn, me, {"title": "Cart", "status": "next"})
    goals.link_to(conn, me, child["id"], a["ref"])
    goals.link_to(conn, me, child["id"], b["id"])
    goals.link_to(conn, me, child["id"], "topic:#Shop")
    tasks.complete(conn, me, a["id"])
    c = goals.get(conn, child["id"])
    assert c["tasks"] == {"total": 2, "done": 1, "open": 1} and c["progress_effective"] == 50
    assert {"kind": "topic", "ref": "shop"} in c["links"]
    c = goals.update(conn, me, child["id"], {"progress": 80, "status": "active"})
    assert c["progress_effective"] == 80  # set progress wins over the task share
    with pytest.raises(goals.Invalid):
        goals.update(conn, me, child["id"], {"progress": 120})
    with pytest.raises(goals.Invalid):
        goals.update(conn, me, child["id"], {"parent_id": child["id"]})
    with pytest.raises(goals.Invalid):
        goals.create(conn, me, {"title": " "})
    with pytest.raises(goals.Invalid):
        goals.create(conn, me, {"title": "x", "owner": "Nobody here"})
    goals.update(conn, me, g["id"], {"status": "dropped"})
    assert [x["id"] for x in goals.list_goals(conn)] == [child["id"]]
    goals.archive(conn, me, child["id"])
    assert goals.list_goals(conn, "all") == [goals.get(conn, g["id"])]


def test_goals_and_reports_over_rest(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, scheduler=False))) as client:
        r = client.post("/api/goals", json={"title": "Hire a designer", "target": "signed by May", "owner": "me"})
        assert r.status_code == 201, r.text
        gid = r.json()["id"]
        assert client.patch(f"/api/goals/{gid}", json={"progress": 30}).json()["progress_effective"] == 30
        assert client.patch(f"/api/goals/{gid}", json={"status": "nope"}).status_code == 422
        assert [g["title"] for g in client.get("/api/goals").json()] == ["Hire a designer"]
        week = weekly_packet.current_week()
        assert client.get(f"/api/reports/{week}").status_code == 404
        built = client.post(f"/api/reports/{week}/build").json()
        assert built["week"] == week and "hotovo" in built["summary"]
        listed = client.get("/api/reports").json()
        assert listed["reports"][0]["week"] == week and listed["reports"][0]["status"] == "draft"
        assert listed["schedule"] == "weekly fri 14:00"
        job = next(j for j in client.get("/api/jobs").json() if j["action"] == "weekly_report")
        assert client.patch(f"/api/jobs/{job['id']}", json={"schedule": "weekly thu 09:30"}).status_code == 200
        assert client.get("/api/reports").json()["schedule"] == "weekly thu 09:30"  # configurable on Automations
        rep = client.get(f"/api/reports/{week}").json()
        assert rep["packet"]["goals"][0]["title"] == "Hire a designer" and rep["transcript"] == []
        assert client.get("/api/reports/soon").status_code == 422


# ------------------------------------------------------------------ the schedule and the meeting

def _weekly_messages(conn):
    cid = weekly.channel_id(conn)
    return [r["body"] for r in conn.execute("SELECT body FROM chat_messages WHERE channel_id = ? ORDER BY id", (cid,))]


def test_scheduler_seeds_the_weekly_jobs(conn):
    scheduler.seed(conn)
    jobs = {j["action"]: j for j in scheduler.list_jobs(conn)}
    assert jobs["weekly_report"]["schedule"] == "weekly fri 14:00"
    fire = datetime.fromisoformat(jobs["weekly_report"]["next_run_at"]).astimezone(weekly_packet.TZ)
    assert (fire.weekday(), fire.hour, fire.minute) == (4, 14, 0)
    assert jobs["weekly_meeting_timeouts"]["schedule"] == "every 30m"


def test_weekly_job_without_agent_stores_the_packet_only(conn):
    out = scheduler.weekly_report(conn)
    assert "no Chief of Staff" in out["skipped"]
    assert weekly.list_reports(conn)[0]["status"] == "draft"


def test_the_whole_meeting(conn, me, tmp_path):
    cos = _cos(conn, me, tmp_path)
    out = scheduler.weekly_report(conn)
    week = out["week"]
    t = tasks.get(conn, me, tasks.parse_id(out["task"]))
    assert t["assignee_id"] == cos.actor_id and t["status"] == "next" and t["topic"] == "weekly"
    assert "weekly_packet" in t["notes"] and t["definition_of_done"]
    assert weekly.weekly_job(conn)["skipped"].endswith("is still open")  # never two tasks for one week

    # The agent's run: the packet, the narrative, publish with questions.
    tasks.claim(conn, cos, t["id"])
    packet = weekly.packet_for(conn)
    assert packet["week"] == week
    with pytest.raises(weekly.Invalid):
        weekly.publish(conn, cos, narrative=" ", questions=["?"])
    with pytest.raises(weekly.Invalid):
        weekly.publish(conn, cos, narrative="x", questions=[f"q{i}" for i in range(6)])
    pub = weekly.publish(conn, cos, narrative="## Co se stalo\nKlidný týden.", decisions=["Najmout grafika?"],
                         questions=["Co se povedlo?", "Co nešlo?", "Priority na příští týden?"],
                         headline="Klidný týden, dva úkoly po termínu.")
    assert pub["status"] == "meeting" and pub["url"] == f"/reports/{week}"
    opening = _weekly_messages(conn)[-1]
    assert opening.startswith(f"Davide, týdenní report je hotový: [{week}](/reports/{week}). "
                              "Máš 15 minut na krátký meeting?")
    assert "1. Co se povedlo?" in opening and "3. Priority na příští týden?" in opening and "@David" in opening
    assert chat.inbox_unread(conn, me.actor_id) >= 1  # the owner is pinged
    assert tasks.get(conn, me, t["id"])["status"] == "waiting"  # no tokens while waiting
    assert weekly.publish(conn, cos, narrative="## Co se stalo\nOprava.", questions=["x"])["updated"]
    assert len(_weekly_messages(conn)) == 1  # an update does not ping twice

    # Someone else may not run the meeting.
    with pytest.raises(Forbidden):
        weekly.reply(conn, Ctx(actors.assistant_id(conn), via="mcp"), "hi")

    # The owner answers in #weekly: the task comes back to the agent's queue.
    row = conn.execute("SELECT * FROM weekly_reports WHERE week = ?", (week,)).fetchone()
    root = row["thread_message_id"]
    chat.send(conn, me, row["channel_id"], "Povedl se launch. Priorita: platby.", reply_to=root)
    t2 = tasks.get(conn, me, t["id"])
    assert t2["status"] == "next" and "odpověděl" in t2["progress_note"]
    st = weekly.status(conn, cos)
    assert st["status"] == "meeting" and st["new_from_owner"] and st["conversation"][-1]["owner"]
    assert "Povedl se launch" in st["conversation"][-1]["body"] and st["hours_left"] > 23

    # A follow-up question parks the task again.
    tasks.claim(conn, cos, t["id"])
    r = weekly.reply(conn, cos, "Díky. Mám založit cíl na platby do konce měsíce?")
    assert r["posted"] and tasks.get(conn, me, t["id"])["status"] == "waiting"

    # The owner wrote while the agent was busy: reply shows it first instead of posting blindly.
    chat.send(conn, me, row["channel_id"], "Ano, cíl: 50 plateb do 31. 3.", reply_to=root)
    tasks.claim(conn, cos, t["id"])
    chat.send(conn, me, row["channel_id"], "A ať to dělá Software Engineer.", reply_to=root)
    first = weekly.reply(conn, cos, "Rozumím.")
    assert not first["posted"] and len(first["new_from_owner"]) == 2

    # Goals and next week's tasks, then close.
    g = goals.create(conn, cos, {"title": "Platby v e-shopu", "target": "50 plateb do 31. 3.", "owner": "David"})
    nt = tasks.create(conn, cos, {"title": "Napojit platební bránu", "status": "next", "assignee": "David",
                                  "notes": "### Proč\nCíl platby.", "definition_of_done": "Brána běží."})
    goals.link_to(conn, cos, g["id"], nt["ref"])
    closed = weekly.close(conn, cos, notes="### Zápis\n- Launch se povedl.\n- Cíl: platby.",
                          summary="Díky Davide, zapsáno.")
    assert closed["status"] == "closed" and closed["tasks"] == [nt["ref"]] and g["id"] in closed["goals"]
    assert tasks.get(conn, me, t["id"])["status"] == "done"
    last = _weekly_messages(conn)[-1]
    assert last.startswith("Díky Davide, zapsáno.") and nt["ref"] in last and f"/reports/{week}" in last
    rep = weekly.get_report(conn, week, me.actor_id)
    assert rep["meeting_notes"].startswith("### Zápis") and rep["decisions"] == ["Najmout grafika?"]
    assert rep["tasks_created"][0]["title"] == "Napojit platební bránu"
    assert rep["goals_changed"][0]["title"] == "Platby v e-shopu"
    assert len([m for m in rep["transcript"] if m["owner"]]) == 3
    with pytest.raises(weekly.Invalid):
        weekly.close(conn, cos, notes="again", summary="")
    # Next week's packet follows up on this meeting's tasks.
    conn.execute("UPDATE weekly_reports SET week = ? WHERE week = ?",
                 (weekly_packet.previous_week(weekly_packet.current_week()), week))
    p = weekly_packet.build(conn, weekly_packet.current_week(), outside=False)
    assert p["last_meeting"]["tasks"][0]["ref"] == nt["ref"]
    assert p["goals"][0]["title"] == "Platby v e-shopu"


def _open_meeting(conn, me, cos):
    out = weekly.weekly_job(conn)
    t = tasks.parse_id(out["task"])
    tasks.claim(conn, cos, t)
    weekly.publish(conn, cos, narrative="## Co se stalo\nNic.", questions=["Priority?"])
    return out["week"], t


def _expire(conn, week):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE weekly_reports SET deadline_at = ? WHERE week = ?", (past, week))
    conn.commit()


def test_no_answer_in_24h_closes_with_the_report_only(conn, me, tmp_path):
    cos = _cos(conn, me, tmp_path)
    week, t = _open_meeting(conn, me, cos)
    assert weekly.meeting_timeouts(conn) == {}  # not yet
    _expire(conn, week)
    assert weekly.meeting_timeouts(conn) == {"closed": [week]}
    rep = weekly.get_report(conn, week)
    assert rep["status"] == "no_reply" and "neodpověděl" in rep["meeting_notes"]
    assert tasks.get(conn, me, t)["status"] == "done"
    assert "na meeting nedošlo" in _weekly_messages(conn)[-1]
    assert scheduler.weekly_meeting_timeouts(conn) == {}


def test_owner_goes_quiet_the_agent_is_asked_once_then_the_core_closes(conn, me, tmp_path):
    cos = _cos(conn, me, tmp_path)
    week, t = _open_meeting(conn, me, cos)
    row = conn.execute("SELECT * FROM weekly_reports WHERE week = ?", (week,)).fetchone()
    chat.send(conn, me, row["channel_id"], "Hned se ozvu.", reply_to=row["thread_message_id"])
    tasks.claim(conn, cos, t)
    assert not weekly.reply(conn, cos, "Dobře, čekám.")["posted"]  # first it shows the unseen answer
    assert weekly.reply(conn, cos, "Dobře, čekám.")["parked"]
    _expire(conn, week)
    assert weekly.meeting_timeouts(conn) == {"nudged": [week]}
    assert tasks.get(conn, me, t)["status"] == "next"  # the agent closes with what it has
    tasks.claim(conn, cos, t)
    conn.execute("UPDATE tasks SET status = 'waiting' WHERE id = ?", (t,))
    _expire(conn, week)
    assert weekly.meeting_timeouts(conn) == {"closed": [week]}
    assert weekly.get_report(conn, week)["status"] == "closed"


def test_meeting_tools_over_mcp(tmp_path):
    db = tmp_path / "m.db"
    c = connect(db)
    migrate(c)
    actors.ensure_builtin(c)
    agents.seed_builtin_permissions(c)
    c.execute("UPDATE actors SET name = 'David' WHERE is_owner = 1")
    me = Ctx(actors.owner_id(c))
    cos = _cos(c, me, tmp_path)
    out = weekly.weekly_job(c)
    c.close()
    for name in ("weekly_packet", "report_publish", "meeting_status", "meeting_reply", "meeting_close",
                 "goal_list", "goal_upsert", "goal_link"):
        assert name in mcp_server.tool_names()
    assert mcp_server.TOOL_PERMISSIONS["goal_upsert"] == "tasks:write"

    def run(actor, calls):
        server = mcp_server.build(db, default_actor=lambda conn: actor)

        async def scenario():
            from mcp.client import Client

            res = []
            async with Client(server) as cl:
                for name, args in calls:
                    r = await cl.call_tool(name, args)
                    res.append((r.is_error, r.structured_content or (json.loads(r.content[0].text)
                                                                      if not r.is_error else r.content[0].text)))
            return res

        return anyio.run(scenario)

    res = run(cos.actor_id, [
        ("weekly_packet", {}),
        ("goal_upsert", {"title": "Platby", "target": "50 plateb", "owner": "David"}),
        ("report_publish", {"narrative": "## Co se stalo\nKlid.", "questions": ["Priority?"],
                            "task_id": out["task"]}),
        ("meeting_status", {}),
    ])
    assert [e for e, _ in res] == [False, False, False, False], res
    assert res[0][1]["week"] == out["week"] and res[1][1]["title"] == "Platby"
    assert res[2][1]["status"] == "meeting" and res[3][1]["status"] == "meeting"
    # Another agent cannot publish or close the meeting.
    refused = run(actors.assistant_id(connect(db)), [("meeting_close", {"notes": "x", "summary": "y"})])
    assert refused[0][0] and "Chief of Staff" in str(refused[0][1])
