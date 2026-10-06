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


def test_timeout_reminder_and_resume(world, monkeypatch):
    w = world
    sent = []
    monkeypatch.setattr(push, "send", lambda conn, settings, actor_id, payload, **kw: sent.append(payload) or 1)
    h = ask(w, minutes=20)
    t0 = datetime.fromisoformat(handoff.get(w.conn, h["id"])["created_at"])
    assert handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=5)) == {"expired": [], "reminded": []}
    out = handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=11))
    assert out["reminded"] == [h["id"]] and sent[0]["url"] == f"/m/handoff/{h['id']}"
    assert sent[0]["title"].startswith("Připomínka") and "9 min" in sent[0]["body"]
    assert handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=12))["reminded"] == []  # once
    out = handoff.sweep(w.conn, w.settings, now=t0 + timedelta(minutes=21))
    assert out["expired"] == [h["id"]]
    assert handoff.get(w.conn, h["id"])["finished_by"] == "timeout"
    assert task_status(w) == "waiting"  # it waits for him; the agent's run ends
    assert w.client.get(f"/api/worker/browser/handoff/{h['id']}/poll", params={"wait": 0},
                        headers=w.h).json()["status"] == "expired"
    item = [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"][0]
    assert item["expired"] is True and item["blocking"] is False
    assert w.client.post(f"/api/handoffs/{h['id']}/done", json={}).status_code == 409
    back = w.client.post(f"/api/handoffs/{h['id']}/resume").json()
    assert back["closed"] is True and task_status(w) == "next"
    assert not [i for i in w.client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]
    assert w.conn.execute("SELECT 1 FROM audit_log WHERE action = 'handoff_resumed'").fetchone()


def test_the_run_ending_expires_it_and_the_minute_job_sweeps(world):
    w = world
    h = ask(w)
    w.conn.execute("UPDATE runs SET status = 'error' WHERE id = ?", (w.run_id,))
    w.conn.commit()
    scheduler.reap_runs(w.conn)
    got = handoff.get(w.conn, h["id"])
    assert got["status"] == "expired" and got["finished_by"] == "run_ended"
    dismissed = w.client.post(f"/api/handoffs/{h['id']}/cancel").json()
    assert dismissed["closed"] is True and task_status(w) == "next"
    assert "nežádej" in w.conn.execute("SELECT progress_note FROM tasks WHERE id = ?", (w.task_id,)).fetchone()[0]


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
