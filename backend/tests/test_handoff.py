"""The owner handoff in an agent's live browser (pos.handoff, pos.api_handoff): the state machine, who may
open it, the relay of frames and input, timeouts and reminders, and the LinkedIn connect flow on top of it."""
import base64
import json
import time
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, browser, handoff, needs_me, outbound_linkedin, push, scheduler
from pos.config import Settings
from pos.core import Ctx, now_iso
from pos.db import connect
from pos.main import create_app

JPEG = b"\xff\xd8\xff\xe0" + b"1" * 64
PASSWORD = "Tajne-heslo-123"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(outbound_linkedin, "_path", lambda: tmp_path / "secrets" / "linkedin.bin")
    settings = Settings(data_dir=tmp_path, password="owner-pw", session_secret="s" * 32)
    client = TestClient(create_app(settings))
    client.__enter__()
    assert client.post("/api/auth/login", json={"password": "owner-pw"}).status_code == 200
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    made = agents.create_agent(conn, owner, name="Content & Brand", purpose="LinkedIn posts", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim", "approvals:request", "browser:use"],
                               data_dir=tmp_path)
    agent_id, key = made["agent"]["id"], made["api_key"]
    task = client.post("/api/tasks", json={"title": "Napsat příspěvek", "status": "working",
                                           "assignee": {"type": "agent", "id": agent_id}}).json()
    tid = int(task["ref"].split("-")[1])
    run_id = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', "
                          "'running', ?)", (agent_id, tid, now_iso())).lastrowid
    conn.execute("UPDATE tasks SET status = 'working' WHERE id = ?", (tid,))
    conn.commit()
    w = type("W", (), {})()
    w.client, w.conn, w.owner, w.agent_id, w.run_id, w.task_id, w.settings = (client, conn, owner, agent_id, run_id,
                                                                              tid, settings)
    w.h = {"Authorization": f"Bearer {key}"}
    yield w
    conn.close()
    client.__exit__(None, None, None)


def ask(w, **kw) -> dict:
    body = {"run_id": w.run_id, "title": "Přihlas se do LinkedIn – zbytek udělám já", "reason": "Potřebuju tvůj účet.",
            "url": "https://www.linkedin.com/login?session_redirect=secret-token", **kw}
    r = w.client.post("/api/worker/browser/handoff", json=body, headers=w.h)
    assert r.status_code == 200, r.text
    return r.json()


def task_status(w) -> str:
    return w.conn.execute("SELECT status FROM tasks WHERE id = ?", (w.task_id,)).fetchone()["status"]


def test_the_owner_opens_types_and_finishes(world):
    w = world
    h = ask(w, done_hint={"url_contains": "/feed"})
    assert h["status"] == "waiting"
    assert ask(w)["id"] == h["id"]  # one open handoff per run
    assert task_status(w) == "waiting"  # a pause, not a failure
    items = [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]
    assert len(items) == 1 and items[0]["title"].startswith("Přihlas se do LinkedIn")
    assert items[0]["m_link"] == f"/m/handoff/{h['id']}" and items[0]["blocking"] is True
    # The push carries the agent's words and opens the live browser in the app.
    payload = push._needs_payload(items[0], {"preview": True})
    assert payload["title"] == "Přihlas se do LinkedIn – zbytek udělám já" and payload["url"] == f"/m/handoff/{h['id']}"

    opened = w.client.post(f"/api/handoffs/{h['id']}/open", json={"app": "m"}).json()
    assert opened["status"] == "active" and opened["agent"]["name"] == "Content & Brand"
    row = w.conn.execute("SELECT actor_id, via, detail FROM audit_log WHERE action = 'handoff_opened'").fetchone()
    assert row["actor_id"] == actors.owner_id(w.conn) and row["via"] == "m"
    # The agent's guard sends a frame; the owner gets it (and nothing newer until the next one).
    r = w.client.post(f"/api/worker/browser/handoff/{h['id']}/frame", headers=w.h, json={
        "frame": base64.b64encode(JPEG).decode(), "url": "https://www.linkedin.com/login", "w": 1280, "h": 800})
    assert r.json()["version"] == 1
    f = w.client.get(f"/api/handoffs/{h['id']}/frame", params={"after": 0, "wait": 0})
    assert f.status_code == 200 and f.content == JPEG and f.headers["x-frame-version"] == "1"
    assert f.headers["x-frame-width"] == "1280" and f.headers["cache-control"] == "no-store"
    assert w.client.get(f"/api/handoffs/{h['id']}/frame", params={"after": 1, "wait": 0}).status_code == 204
    assert w.client.post(f"/api/worker/browser/handoff/{h['id']}/frame", headers=w.h,
                         json={"frame": base64.b64encode(b"not a jpeg").decode()}).status_code == 422
    # His input goes through validated; junk is dropped; the guard drains it once.
    sent = w.client.post(f"/api/handoffs/{h['id']}/input", json={"events": [
        {"t": "click", "x": 0.5, "y": 2, "n": 9}, {"t": "text", "text": PASSWORD}, {"t": "key", "key": "Enter"},
        {"t": "key", "key": "rm -rf /"}, {"t": "eval", "code": "x"}, {"t": "wheel", "dx": 0, "dy": 99999}]}).json()
    assert sent["accepted"] == 4
    poll = w.client.get(f"/api/worker/browser/handoff/{h['id']}/poll", params={"wait": 0}, headers=w.h).json()
    assert poll["status"] == "active" and poll["watching"] is True and poll["profile"] is False
    assert poll["events"] == [{"t": "click", "x": 0.5, "y": 1.0, "button": "left", "n": 3},
                              {"t": "text", "text": PASSWORD}, {"t": "key", "key": "Enter"},
                              {"t": "wheel", "dx": 0.0, "dy": 2000.0}]
    assert w.client.get(f"/api/worker/browser/handoff/{h['id']}/poll", params={"wait": 0},
                        headers=w.h).json()["events"] == []
    # Hotovo: the task goes back to work, the agent keeps the login (browser:profile), the guard hears it.
    done = w.client.post(f"/api/handoffs/{h['id']}/done", json={"keep_login": True}).json()
    assert done["status"] == "done" and done["finished_by"] == "owner"
    assert task_status(w) == "working"
    assert browser.may_keep_profile(w.conn, w.agent_id)
    poll = w.client.get(f"/api/worker/browser/handoff/{h['id']}/poll", params={"wait": 0}, headers=w.h).json()
    assert poll["status"] == "done" and poll["profile"] is True and poll["keep_login"] is True
    assert not [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]
    # What he typed is nowhere: not in the audit, not in the database; the audit keeps counts and no query string.
    dump = "\n".join(str(tuple(r)) for r in w.conn.execute("SELECT * FROM audit_log"))
    dump += "\n".join(str(tuple(r)) for r in w.conn.execute("SELECT * FROM browser_handoffs"))
    assert PASSWORD not in dump and "secret-token" not in dump
    fin = json.loads(w.conn.execute("SELECT detail FROM audit_log WHERE action = 'handoff_done'").fetchone()["detail"])
    assert fin["text_chars"] == len(PASSWORD) and fin["click"] == 1 and fin["profile_granted"] is True
    # Finished: no more input, no more frames.
    assert w.client.post(f"/api/handoffs/{h['id']}/input", json={"events": [{"t": "key", "key": "a"}]}).status_code == 409
    assert w.client.get(f"/api/handoffs/{h['id']}/frame", params={"wait": 0}).headers["x-handoff-status"] == "done"


def test_only_the_signed_in_owner_opens_it(world, tmp_path):
    w = world
    h = ask(w)
    with TestClient(create_app(w.settings)) as stranger:  # not signed in
        for method, path in (("get", ""), ("post", "/open"), ("get", "/frame"), ("post", "/input"), ("post", "/done"),
                             ("post", "/cancel")):
            r = getattr(stranger, method)(f"/api/handoffs/{h['id']}{path}", **({"json": {}} if method == "post" else {}))
            assert r.status_code == 401, (path, r.status_code)
        inv = w.client.post("/api/invites", json={"email": "jana@firma.cz", "name": "Jana"}).json()
        stranger.post("/api/auth/accept", json={"token": inv["token"], "password": "a-long-secret"})
        assert stranger.get(f"/api/handoffs/{h['id']}").status_code == 403  # a member, not the owner
        assert stranger.post(f"/api/handoffs/{h['id']}/input", json={"events": []}).status_code == 403
        assert stranger.post(f"/api/handoffs/{h['id']}/done", json={}).status_code == 403
        assert not [i for i in stranger.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]
    # Another agent cannot read this agent's handoff, nor ask for one on a run that is not its own.
    other = agents.create_agent(w.conn, w.owner, name="Other", purpose="x", lifetime="long_lived",
                                permissions=["tasks:read", "browser:use"], data_dir=tmp_path)
    w.conn.commit()
    oh = {"Authorization": f"Bearer {other['api_key']}"}
    assert w.client.get(f"/api/worker/browser/handoff/{h['id']}/poll", headers=oh).status_code == 404
    assert w.client.post("/api/worker/browser/handoff", headers=oh,
                         json={"run_id": w.run_id, "title": "x"}).status_code == 403


def test_without_the_browser_grant_or_a_title_there_is_no_handoff(world, tmp_path):
    w = world
    assert w.client.post("/api/worker/browser/handoff", headers=w.h,
                         json={"run_id": w.run_id, "title": "  "}).status_code == 422
    plain = agents.create_agent(w.conn, w.owner, name="Plain", purpose="x", lifetime="long_lived",
                                permissions=["tasks:read"], data_dir=tmp_path)
    w.conn.commit()
    assert w.client.post("/api/worker/browser/handoff", headers={"Authorization": f"Bearer {plain['api_key']}"},
                         json={"run_id": w.run_id, "title": "x"}).status_code == 403


def test_the_agent_detects_done_itself(world):
    w = world
    h = ask(w, done_hint={"url_contains": "/feed", "bogus": "x"})
    assert handoff.get(w.conn, h["id"])["done_hint"] == {"url_contains": "/feed"}
    r = w.client.post(f"/api/worker/browser/handoff/{h['id']}/done", headers=w.h).json()
    assert r["status"] == "done"
    got = handoff.get(w.conn, h["id"])
    assert got["finished_by"] == "hint" and task_status(w) == "working"
    assert not browser.may_keep_profile(w.conn, w.agent_id)  # only the owner's Hotovo grants the login


def test_cancel_tells_the_agent_and_the_task_goes_on(world):
    w = world
    h = ask(w)
    out = w.client.post(f"/api/handoffs/{h['id']}/cancel").json()
    assert out["status"] == "cancelled" and task_status(w) == "working"
    assert w.client.get(f"/api/worker/browser/handoff/{h['id']}/poll", params={"wait": 0},
                        headers=w.h).json()["status"] == "cancelled"
    assert w.client.post(f"/api/handoffs/{h['id']}/done", json={}).status_code == 409


def _new_run(w) -> int:
    """The agent's next run on the same task (after it was woken)."""
    rid = w.conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', "
                         "'running', ?)", (w.agent_id, w.task_id, now_iso())).lastrowid
    w.conn.commit()
    return rid


def test_the_live_window_parks_it_and_opening_it_wakes_the_agent(world, monkeypatch):
    """prod 2026-10-07: T-957/T-958's handoffs expired unseen. Now nothing dies: the run ends, the item stays, and
    opening it wakes the agent, which prepares the page again and continues the same handoff live."""
    w = world
    sent, woken = [], []
    monkeypatch.setattr(push, "send", lambda conn, settings, actor_id, payload, **kw: sent.append(payload) or 1)
    from pos import owner_fallback, wake

    monkeypatch.setattr(owner_fallback, "SENDER", lambda subject, text: "m1")
    monkeypatch.setattr(wake, "wake", lambda actor_id: woken.append(actor_id))
    h = ask(w, minutes=45)
    got = handoff.get(w.conn, h["id"])
    t0 = datetime.fromisoformat(got["created_at"])
    assert datetime.fromisoformat(got["expires_at"]) - t0 == timedelta(minutes=handoff.MAX_MINUTES)  # never long
    assert handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=3))["expired"] == []
    out = handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=11))
    assert out["expired"] == [h["id"]]
    got = handoff.get(w.conn, h["id"])
    assert got["status"] == "parked" and got["finished_by"] == "timeout" and task_status(w) == "waiting"
    # The guard hears it and ends the run; the item stays, blocking, with no "expired" in it.
    assert w.client.get(f"/api/worker/browser/handoff/{h['id']}/poll", params={"wait": 0},
                        headers=w.h).json()["status"] == "parked"
    w.conn.execute("UPDATE runs SET status = 'ok' WHERE id = ?", (w.run_id,))
    w.conn.commit()
    item = [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"][0]
    assert item["expired"] is False and item["parked"] is True and item["blocking"] is True
    # One reminder push for a parked one he has not opened, later.
    assert handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=61))["reminded"] == [h["id"]]
    assert sent[-1]["title"].startswith("Připomínka") and sent[-1]["url"] == f"/m/handoff/{h['id']}"
    assert handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=62))["reminded"] == []

    # He opens it (even hours later): "preparing", the agent is woken with the task.
    opened = w.client.post(f"/api/handoffs/{h['id']}/open", json={"app": "m"}).json()
    assert opened["status"] == "preparing" and opened["preparing"] is True
    assert woken == [w.agent_id] and task_status(w) == "next"
    assert "připrav stránku znovu" in w.conn.execute("SELECT progress_note FROM tasks WHERE id = ?",
                                                     (w.task_id,)).fetchone()[0]
    assert w.client.get(f"/api/handoffs/{h['id']}/frame", params={"wait": 0}).headers["x-handoff-status"] == "preparing"
    assert w.client.post(f"/api/handoffs/{h['id']}/open", json={}).json()["status"] == "preparing"  # no 2nd wake
    assert woken == [w.agent_id]
    # The agent's new run asks again on the same task: the same handoff goes live, at once active (he is there).
    w.conn.execute("UPDATE tasks SET status = 'working' WHERE id = ?", (w.task_id,))
    w.conn.commit()
    w.run_id = _new_run(w)
    again = ask(w)
    assert again["id"] == h["id"] and again["status"] == "active"
    got = handoff.get(w.conn, h["id"])
    assert got["run_id"] == w.run_id and got["reattached"] == 1 and task_status(w) == "waiting"
    assert len([i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]) == 1
    r = w.client.post(f"/api/worker/browser/handoff/{h['id']}/frame", headers=w.h,
                      json={"frame": base64.b64encode(JPEG).decode(), "w": 800, "h": 600})
    assert r.status_code == 200
    assert w.client.get(f"/api/handoffs/{h['id']}/frame", params={"wait": 0}).status_code == 200
    done = w.client.post(f"/api/handoffs/{h['id']}/done", json={"keep_login": False}).json()
    assert done["status"] == "done" and task_status(w) == "working"
    assert not [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]
    acts = [r["action"] for r in w.conn.execute("SELECT action FROM audit_log WHERE action LIKE 'handoff_%'")]
    assert {"handoff_parked", "handoff_reprepare", "handoff_reattached", "handoff_done"} <= set(acts)


def test_preparing_without_the_agent_parks_again_and_he_can_cancel(world, monkeypatch):
    w = world
    from pos import owner_fallback, wake

    monkeypatch.setattr(owner_fallback, "SENDER", lambda subject, text: "m1")
    woken = []
    monkeypatch.setattr(wake, "wake", lambda actor_id: woken.append(actor_id))
    h = ask(w)
    w.conn.execute("UPDATE runs SET status = 'error' WHERE id = ?", (w.run_id,))
    w.conn.commit()
    scheduler.reap_runs(w.conn)
    got = handoff.get(w.conn, h["id"])
    assert got["status"] == "parked" and got["finished_by"] == "run_ended"
    assert w.client.post(f"/api/handoffs/{h['id']}/open", json={}).json()["status"] == "preparing"
    later = datetime.now().astimezone() + timedelta(minutes=handoff.PREPARE_MINUTES + 1)
    handoff.sweep(w.conn, w.settings, now=later)
    assert handoff.get(w.conn, h["id"])["status"] == "parked"
    # "Zkusit znovu" (resume) wakes it again; Zrušit ends it for good and tells the agent.
    assert w.client.post(f"/api/handoffs/{h['id']}/resume").json()["status"] == "preparing"
    assert len(woken) == 2
    cancelled = w.client.post(f"/api/handoffs/{h['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and task_status(w) == "next"
    assert "nežádej" in w.conn.execute("SELECT progress_note FROM tasks WHERE id = ?", (w.task_id,)).fetchone()[0]
    assert not [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]


def test_without_a_push_device_the_owner_gets_one_email_per_handoff(world, monkeypatch):
    w = world
    from pos import owner_fallback

    mails = []
    monkeypatch.setattr(owner_fallback, "SENDER", lambda subject, text: mails.append((subject, text)) or "m1")
    h = ask(w)
    out = handoff.sweep(w.conn, w.settings)
    assert out["notified"] == [{"id": h["id"], "via": "email"}]
    subject, text = mails[0]
    assert subject == "Čeká na tebe: Přihlas se do LinkedIn – zbytek udělám já"
    assert f"/m/handoff/{h['id']}" in text and "notifikace" in text and "secret-token" not in text
    got = handoff.get(w.conn, h["id"])
    assert got["notified_via"] == "email" and got["notified_at"]
    handoff.sweep(w.conn, w.settings)
    t0 = datetime.fromisoformat(got["created_at"])
    handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=30))  # parked: still the same handoff
    assert len(mails) == 1  # at most once per handoff
    # With a working push device there is no e-mail (the "Čeká na tebe" push covers it).
    push.ensure_schema(w.conn)
    w.conn.execute("INSERT INTO push_subscriptions (actor_id, endpoint, p256dh, auth, created_at) VALUES (?, ?, 'k', "
                   "'a', ?)", (actors.owner_id(w.conn), "https://fcm.googleapis.com/x", now_iso()))
    w.conn.execute("UPDATE browser_handoffs SET status = 'cancelled' WHERE id = ?", (h["id"],))
    w.conn.commit()
    w.run_id = _new_run(w)
    h2 = ask(w)
    assert h2["id"] != h["id"]
    handoff.sweep(w.conn, w.settings)
    assert len(mails) == 1 and handoff.get(w.conn, h2["id"])["notified_via"] == "push"


def test_a_failed_email_is_recorded_and_not_retried(world, monkeypatch):
    w = world
    from pos import owner_fallback

    calls = []

    def boom(subject, text):
        calls.append(1)
        raise RuntimeError("POS_GMAIL_SEND_TOKEN_X is not set")

    monkeypatch.setattr(owner_fallback, "SENDER", boom)
    h = ask(w)
    handoff.sweep(w.conn, w.settings)
    handoff.sweep(w.conn, w.settings)
    assert calls == [1] and handoff.get(w.conn, h["id"])["notified_via"].startswith("failed:")
    assert w.conn.execute("SELECT 1 FROM audit_log WHERE action = 'handoff_notify_failed'").fetchone()


def test_owner_without_push_sees_one_setup_item_until_he_hides_it(world, monkeypatch):
    w = world
    from pos import owner_fallback

    assert not [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "setup"]
    monkeypatch.setattr(owner_fallback, "SENDER", lambda subject, text: "m1")
    ask(w)
    handoff.sweep(w.conn, w.settings)  # it mattered: a handoff had to go by e-mail
    items = w.client.get("/api/needs-me").json()["items"]
    setup = [i for i in items if i["kind"] == "setup"]
    assert len(setup) == 1 and setup[0]["title"] == "Zapnout notifikace v telefonu"
    assert setup[0]["m_link"] == "/m/settings" and setup[0]["blocking"] is False
    assert w.client.post(setup[0]["hide_url"]).json() == {"hidden": True}
    assert not [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "setup"]


def test_old_expired_handoffs_become_parked_when_their_task_still_waits(world):
    w = world
    h = ask(w)
    handoff._ready.clear()
    w.conn.execute("UPDATE browser_handoffs SET status = 'expired', finished_at = ?, finished_by = 'timeout' "
                   "WHERE id = ?", (now_iso(), h["id"]))
    other = w.conn.execute("INSERT INTO browser_handoffs (actor_id, task_id, run_id, title, status, created_at, "
                           "expires_at) VALUES (?, NULL, NULL, 'x', 'expired', ?, ?)",
                           (w.agent_id, now_iso(), now_iso())).lastrowid
    w.conn.commit()
    handoff.ensure_schema(w.conn)
    assert handoff.get(w.conn, h["id"])["status"] == "parked"
    assert handoff.get(w.conn, other)["closed_at"]  # no task: closed, not listed
    keys = [i["key"] for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]
    assert keys == [f"handoff:{h['id']}"]


def test_clean_event_rejects_what_is_not_input():
    c = handoff.clean_event
    assert c({"t": "click", "x": "nan", "y": 0}) is None
    assert c({"t": "key", "key": "Control+Shift+Tab"}) == {"t": "key", "key": "Control+Shift+Tab"}
    assert c({"t": "key", "key": "Control+Alt+Meta+Shift+a"}) is None
    assert c({"t": "text", "text": "x" * 900}) == {"t": "text", "text": "x" * handoff.MAX_TEXT}
    assert c({"t": "move", "x": -1, "y": 0.2}) == {"t": "move", "x": 0.0, "y": 0.2}
    assert c("click") is None and c({"t": "text", "text": ""}) is None


# ------------------------------------------------------------------ LinkedIn: the agent connects, the owner clicks

def test_connect_linkedin_starts_the_agent_flow_and_ends_with_a_token(world, monkeypatch):
    w = world
    for name in ("POS_LINKEDIN_CLIENT_ID", "POS_LINKEDIN_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    st = w.client.get("/api/integrations/linkedin/status").json()
    assert st["app"] is False and st["flow"] is None
    started = w.client.post("/api/integrations/linkedin/agent-connect").json()
    assert started["agent"] == "Content & Brand" and started["existing"] is False
    assert w.client.post("/api/integrations/linkedin/agent-connect").json()["existing"] is True
    t = w.conn.execute("SELECT * FROM tasks WHERE source = 'linkedin:connect'").fetchone()
    assert t["assignee_id"] == w.agent_id and "browser_request_owner_handoff" in t["notes"]
    assert "browser_capture_secret" in t["notes"] and "Never ask him to find" in t["notes"]
    assert w.client.get("/api/integrations/linkedin/status").json()["flow"]["task_ref"] == started["task_ref"]
    # /start without an app no longer fails with "set the env": it starts the same flow.
    assert w.client.get("/api/integrations/linkedin/start", follow_redirects=False).status_code == 302

    # The guard captures the app's keys off the developer portal (only there, only over HTTPS, only for this run).
    run = w.conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', "
                         "'running', ?)", (w.agent_id, t["id"], now_iso())).lastrowid
    w.conn.commit()
    cap = "/api/worker/browser/capture"
    body = {"run_id": run, "url": "https://www.linkedin.com/developers/apps/1/auth"}
    assert w.client.post(cap, headers=w.h, json={**body, "target": "linkedin.client_secret", "value": "••••••"}
                         ).status_code == 422
    assert w.client.post(cap, headers=w.h, json={**body, "url": "https://evil.example/x", "target":
                                                 "linkedin.client_secret", "value": "s3cr3t-value"}).status_code == 403
    assert w.client.post(cap, headers=w.h, json={**body, "target": "aws.key", "value": "x"}).status_code == 422
    assert w.client.post(cap, headers=w.h, json={**body, "target": "linkedin.client_id", "value": "86abc"}).json()["ok"]
    assert w.client.post(cap, headers=w.h, json={**body, "target": "linkedin.client_secret",
                                                 "value": "s3cr3t-value"}).json()["chars"] == 12
    assert outbound_linkedin.app_creds() == ("86abc", "s3cr3t-value")
    assert b"s3cr3t-value" not in (w.settings.data_dir / "secrets" / "linkedin-app.bin").read_bytes()
    assert "s3cr3t-value" not in "\n".join(str(tuple(r)) for r in w.conn.execute("SELECT * FROM audit_log"))
    assert w.client.get("/api/integrations/linkedin/status").json()["app"] is True

    # The consent: a one-time state for the agent's browser (no PersonalOS session there).
    state = outbound_linkedin.new_flow_state(w.conn, Ctx(w.agent_id))
    w.conn.commit()
    assert "client_id=86abc" in outbound_linkedin.auth_url(state)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/accessToken"):
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            assert form["client_secret"] == "s3cr3t-value" and form["code"] == "c0de"
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 5184000})
        return httpx.Response(200, json={"sub": "abc", "name": "David"})

    monkeypatch.setattr(outbound_linkedin, "_transport", httpx.MockTransport(handler))
    with TestClient(create_app(w.settings)) as agent_browser:
        r = agent_browser.get("/api/integrations/linkedin/callback", params={"code": "c0de", "state": state})
        assert r.status_code == 200 and "LinkedIn je připojený" in r.text
        again = agent_browser.get("/api/integrations/linkedin/callback", params={"code": "c0de", "state": state})
        assert again.status_code == 400  # used up
        assert agent_browser.get("/api/integrations/linkedin/callback",
                                 params={"code": "x", "state": "guess"}).status_code == 400
    assert outbound_linkedin.connected()["author"] == "urn:li:person:abc"
    row = w.conn.execute("SELECT detail, via FROM audit_log WHERE action = 'linkedin_connected'").fetchone()
    assert json.loads(row["detail"])["flow"] == "agent" and row["via"] == "linkedin-flow"


def test_capture_needs_the_open_connect_task(world):
    w = world
    r = w.client.post("/api/worker/browser/capture", headers=w.h, json={
        "run_id": w.run_id, "url": "https://www.linkedin.com/developers/apps/1/auth", "target": "linkedin.client_id",
        "value": "86abc"})
    assert r.status_code == 403 and "Připojit LinkedIn" in r.json()["detail"]


def test_the_expiry_check_renews_through_the_agent(world):
    w = world
    outbound_linkedin.save_token({"access_token": "t", "author": "urn:li:person:x", "expires_at": time.time() + 86400,
                                  "name": "David"})
    out = outbound_linkedin.expiry_check(w.conn)
    assert out["reconnect_task"]
    t = w.conn.execute("SELECT title, notes, assignee_id FROM tasks WHERE source = 'linkedin:connect'").fetchone()
    assert t["assignee_id"] == w.agent_id and "renew" in t["notes"]
    assert outbound_linkedin.expiry_check(w.conn) == {}  # one at a time


def test_no_ui_text_sends_the_owner_for_linkedin_keys():
    """The owner never reads 'set POS_LINKEDIN_CLIENT_ID': connecting is the agent's job now."""
    from pathlib import Path

    web = Path(__file__).resolve().parents[2] / "web" / "src"
    if not web.is_dir():
        pytest.skip("no web sources here")
    hits = [p.name for p in web.rglob("*.ts*") if "POS_LINKEDIN_CLIENT" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_needs_me_lists_handoffs_first_and_only_for_the_owner(world):
    w = world
    ask(w)
    items = needs_me.collect(w.conn, w.owner)["items"]
    assert items[0]["kind"] == "handoff" and needs_me.collect(w.conn, w.owner)["counts"]["handoff"] == 1


def test_guard_hands_the_live_browser_to_the_owner_and_continues():
    """End to end in its own process (stdio MCP children misbehave under pytest on Windows): the real guard,
    a fake Playwright MCP, the real API, the owner on his phone (handoff_e2e.py)."""
    pytest.importorskip("pos_worker")
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).with_name("handoff_e2e.py")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=150)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-3000:]
    assert "HANDOFF E2E OK" in out.stdout


def test_a_temporary_1password_write_token_is_taken_off_the_console_only_for_its_task(world, monkeypatch, tmp_path):
    """1Password cannot give the existing service account write access (T-918, 2026-10-07): the agent prepares a
    temporary one, the owner clicks Create, and the token shown once goes straight into PersonalOS."""
    from pos import op_write

    w = world
    monkeypatch.setattr(op_write, "_path", lambda: tmp_path / "secrets" / "op-write-token.bin")
    tok = "ops_" + "eyJ" + "x" * 60
    cap = "/api/worker/browser/capture"
    body = {"run_id": w.run_id, "url": "https://obseum.1password.eu/developer-tools/service-accounts/new",
            "target": op_write.TARGET, "value": tok}
    r = w.client.post(cap, headers=w.h, json=body)
    assert r.status_code == 403 and "write-token task" in r.json()["detail"]  # no task of that kind
    w.conn.execute("UPDATE tasks SET source = ? WHERE id = ?", (op_write.TASK_SOURCE, w.task_id))
    w.conn.commit()
    assert w.client.post(cap, headers=w.h, json={**body, "url": "https://evil.example/1password.eu"}
                         ).status_code == 403
    assert w.client.post(cap, headers=w.h, json={**body, "url": "http://my.1password.com/x"}).status_code == 403
    assert w.client.post(cap, headers=w.h, json={**body, "value": "••••••••"}).status_code == 422
    assert w.client.post(cap, headers=w.h, json=body).json()["chars"] == len(tok)
    assert op_write.token() == tok and tok.encode() not in op_write._path().read_bytes()
    assert tok not in "\n".join(str(tuple(r)) for r in w.conn.execute("SELECT * FROM audit_log"))
    op_write.discard()
    assert op_write.token() is None and not op_write.present()
    assert op_write.host_ok("my.1password.com") and not op_write.host_ok("1password.com.evil.example")
