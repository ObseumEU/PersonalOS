from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import a2a, actors, agents, killswitch, scheduler, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


def test_schedules():
    base = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)  # Friday, 12:00 in Prague
    assert scheduler.next_run("every 60m", base) == base + timedelta(minutes=60)
    d = scheduler.next_run("daily 07:00", base).astimezone(scheduler.TZ)
    assert (d.date(), d.hour) == (date(2026, 9, 26), 7)
    w = scheduler.next_run("weekdays 07:00", base).astimezone(scheduler.TZ)
    assert w.date() == date(2026, 9, 28)  # Monday
    f = scheduler.next_run("weekly fri 15:00", base).astimezone(scheduler.TZ)
    assert (f.date(), f.hour) == (date(2026, 9, 25), 15)
    with pytest.raises(ValueError):
        scheduler.next_run("sometimes", base)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    yield client, conn, Ctx(actors.owner_id(conn))
    conn.close()
    client.__exit__(None, None, None)


def test_jobs(app):
    client, conn, me = app
    jobs = {j["action"]: j for j in scheduler.list_jobs(conn)}
    assert set(jobs) == set(scheduler.ACTIONS)
    out = scheduler.run_job(conn, jobs["morning_brief"])
    assert tasks.get(conn, me, tasks.parse_id(out["task"]))["title"].startswith("Morning brief")

    w = tasks.create(conn, me, {"title": "VAT docs", "assignee": {"type": "external", "name": "Petr"},
                                "follow_up": date.today().isoformat()})
    out = scheduler.run_job(conn, jobs["follow_ups"])
    step = tasks.get(conn, me, tasks.parse_id(out["reminders"][0]))
    assert step["parent_id"] == w["id"] and step["assignee_type"] == "ai"
    assert tasks.get(conn, me, w["id"])["follow_up"] > date.today().isoformat()

    retro = scheduler.run_job(conn, jobs["nightly_retrospective"])
    assert tasks.get(conn, me, tasks.parse_id(retro["task"]))["assignee_type"] == "ai"

    killswitch.freeze(conn, me, "t")
    assert scheduler.run_job(conn, jobs["nightly_retrospective"]) == {"skipped": "kill switch is on"}
    killswitch.unfreeze(conn, me)

    r = client.patch(f"/api/jobs/{jobs['morning_brief']['id']}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert client.patch(f"/api/jobs/{jobs['morning_brief']['id']}", json={"schedule": "whenever"}).status_code == 422


def test_a2a_server(app, tmp_path):
    client, conn, me = app
    card = client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "PersonalOS" and card["url"].endswith("/a2a")
    out = agents.create_agent(conn, me, name="Outside", purpose="external agent", lifetime="long_lived",
                              permissions=["tasks:read", "messages:send"], data_dir=tmp_path)
    h = {"Authorization": f"Bearer {out['api_key']}"}
    rpc = lambda method, params: client.post("/a2a", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": method,  # noqa: E731
                                                                     "params": params}).json()
    msg = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": "Prepare the Q4 report #acme !high"}]}
    task = rpc("SendMessage", {"message": msg})["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    t = tasks.get(conn, me, tasks.parse_id(task["id"]))
    assert (t["topic"], t["priority"], t["source"]) == ("acme", 1, "a2a")
    assert rpc("GetTask", {"id": task["id"]})["result"]["id"] == task["id"]
    to_member = rpc("message/send", {"message": {**msg, "parts": [{"text": "hello"}]}, "metadata": {"to": "Assistant"}})
    assert "Delivered to Assistant" in to_member["result"]["message"]["parts"][0]["text"]
    assert rpc("SendStreamingMessage", {"message": msg})["error"]["code"] == -32004
    assert rpc("CancelTask", {"id": task["id"]})["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {}}).status_code == 401


def test_a2a_bridge_loopback(app, tmp_path, monkeypatch):
    """A remote member that is PersonalOS itself: the task travels out over A2A,
    is finished on the 'remote' side, and the answer comes back for review."""
    client, conn, me = app
    remote_side = agents.create_agent(conn, me, name="Remote side", purpose="accepts A2A", lifetime="long_lived",
                                      permissions=["tasks:read"], data_dir=tmp_path)
    monkeypatch.setenv("POS_A2A_KEY_RESEARCHER", remote_side["api_key"])
    researcher = agents.create_agent(conn, me, name="Researcher", purpose="remote research agent",
                                     lifetime="long_lived", runtime="a2a", a2a_url="http://testserver",
                                     permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    t = tasks.create(conn, me, {"title": "Compare Acme pricing 2025 vs 2026", "assignee": {"type": "agent", "id": researcher}})
    conn.commit()
    out = a2a.sync(conn, http=client)
    assert out["sent"] == [t["ref"]]
    assert tasks.get(conn, me, t["id"])["status"] == "working"
    link = a2a.links(conn)[0]
    remote_task = tasks.parse_id(link["remote_task_id"])
    # The remote side finishes its task.
    tasks.update(conn, me, remote_task, {"progress_note": "2026 is 8 % higher", "status": "done"})
    conn.commit()
    out = a2a.sync(conn, http=client)
    assert out["finished"] == [t["ref"]]
    back = tasks.get(conn, me, t["id"])
    assert back["status"] == "review" and "8 % higher" in back["progress_note"]


def test_reaper_releases_runs_of_dead_workers(app):
    client, conn, me = app
    t = tasks.create(conn, me, {"title": "Stuck", "assignee": "ai"})
    tasks.claim(conn, Ctx(actors.assistant_id(conn)), t["id"])
    rid = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, engine) VALUES (?, ?, 'task', 'running', '2020-01-01T00:00:00+00:00', 'claude')",
                       (actors.assistant_id(conn), t["id"])).lastrowid
    conn.commit()
    out = scheduler.reap_runs(conn)
    assert out["released"] == [rid]
    assert tasks.get(conn, me, t["id"])["status"] == "next"
    assert conn.execute("SELECT status FROM runs WHERE id = ?", (rid,)).fetchone()[0] == "error"


def test_a2a_empty_reply_keeps_the_task_queued_and_the_card_is_cached(app, tmp_path):
    import httpx

    client, conn, me = app
    calls = {"card": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".json"):
            calls["card"] += 1
            return httpx.Response(200, json={"url": "http://remote/a2a"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": "1", "result": {}})  # neither task nor message

    http = httpx.Client(transport=httpx.MockTransport(handler))
    silent = agents.create_agent(conn, me, name="Silent", purpose="remote", lifetime="long_lived", runtime="a2a",
                                 a2a_url="http://remote/.well-known/agent-card.json",
                                 permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    t = tasks.create(conn, me, {"title": "Ping", "assignee": {"type": "agent", "id": silent}})
    conn.commit()
    assert a2a.sync(conn, http=http)["sent"] == []
    assert tasks.get(conn, me, t["id"])["status"] == "next" and a2a.links(conn) == []
    a2a.sync(conn, http=http)
    assert calls["card"] == 1  # the second round used the cached endpoint
