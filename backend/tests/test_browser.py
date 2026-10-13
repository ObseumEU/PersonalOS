import base64
import sys

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, approvals, browser, killswitch
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()

def test_policy_lets_reading_forms_and_sending_through_and_stops_paying_signing_deleting():
    d = browser.decide
    assert d("browser_navigate", {"url": "https://news.example.com"})[0] == "allow"
    assert d("browser_type", {"element": "Search box", "text": "x", "submit": True})[0] == "allow"
    assert d("browser_fill_form", {"fields": [{"name": "Name", "value": "x"}]})[0] == "allow"
    assert d("browser_click", {"element": "Log in button"})[0] == "allow"
    assert d("browser_click", {"element": "Pay now"})[0] == "approval"
    assert d("browser_click", {"element": "Odeslat zprávu"})[0] == "allow"  # ordinary work (Ú1)
    assert d("browser_click", {"element": "Sign contract"})[0] == "approval"
    assert d("browser_click", {"element": "Delete repository"})[0] == "approval"
    assert d("browser_navigate", {"url": "https://ib.fio.cz/login"})[0] == "approval"  # banking
    assert d("browser_type", {"element": "Reply message", "text": "hi", "submit": True})[0] == "allow"
    assert d("browser_press_key", {"key": "Enter"}, last_field="Chat message")[0] == "allow"
    # the owner's personal channels: posting asks, reading does not
    li = "https://www.linkedin.com/feed/"
    assert d("browser_navigate", {"url": li})[0] == "allow"
    assert d("browser_click", {"element": "Post"}, url=li)[0] == "approval"
    assert d("browser_press_key", {"key": "Enter"}, url=li, last_field="Write a comment")[0] == "approval"
    assert d("browser_fill_form", {"fields": [{"name": "Card number", "value": "4111"}]})[0] == "approval"
    assert d("browser_navigate", {"url": "https://other.example"}, allow_hosts=["example.com"])[0] == "approval"
    red = browser.redact("browser_type", {"element": "Password", "text": "hunter2"})
    assert red["text"] == "<7 chars>" and "hunter2" not in str(red)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    out = agents.create_agent(conn, owner, name="Browser", purpose="web research", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "approvals:request", "browser:use"],
                              data_dir=tmp_path)
    conn.commit()
    yield client, conn, owner, out["agent"]["id"], out["api_key"], settings
    conn.close()
    client.__exit__(None, None, None)


def test_check_and_log_endpoints(app):
    client, conn, owner, agent_id, key, settings = app
    h = {"Authorization": f"Bearer {key}"}
    ok = client.post("/api/worker/browser/check", json={"tool": "browser_navigate", "args": {"url": "https://x.org"}},
                     headers=h).json()
    assert ok["decision"] == "allow"
    ask = client.post("/api/worker/browser/check", headers=h, json={
        "tool": "browser_click", "args": {"element": "Buy now"}, "url": "https://shop.example", "screenshot": PNG}).json()
    assert ask["decision"] == "approval"
    a = approvals.get(conn, ask["approval_id"])
    assert a["action"] == "browser: click" and a["details"]["screenshot"].endswith(".png")
    assert client.get(f"/api/browser/screenshots/{a['details']['screenshot']}").status_code == 200
    assert client.get("/api/browser/screenshots/../../personalos.db").status_code == 404
    assert client.get(f"/api/worker/approvals/{ask['approval_id']}", headers=h).json()["status"] == "pending"
    logged = client.post("/api/worker/browser/log", headers=h, json={
        "tool": "browser_type", "args": {"element": "Password", "text": "hunter2"}, "url": "https://x.org",
        "screenshot": PNG}).json()
    assert logged["screenshot"]
    row = conn.execute("SELECT detail FROM audit_log WHERE action = 'browser:type'").fetchone()
    assert row and "hunter2" not in row["detail"]
    killswitch.freeze(conn, owner, "test")
    assert client.post("/api/worker/browser/check", json={"tool": "browser_navigate", "args": {}},
                       headers=h).json()["decision"] == "refuse"


def test_the_guard_sends_its_task_as_a_ref_and_the_approval_and_steps_land_on_the_task(app):
    """The guard sends POS_TASK_ID, the run's task ref ("T-12"): stored as text it broke the approvals foreign key
    (prod: POST /api/worker/browser/check 500, the agent could not ask) and filed steps the task trace never found."""
    from pos import tasks

    client, conn, owner, agent_id, key, settings = app
    h = {"Authorization": f"Bearer {key}"}
    t = tasks.create(conn, owner, {"title": "Koupit doménu", "assignee": {"type": "agent", "id": agent_id}})
    conn.commit()
    ref = t["ref"]
    r = client.post("/api/worker/browser/check", headers=h, json={
        "tool": "browser_click", "args": {"element": "Buy now"}, "url": "https://shop.example", "task_id": ref})
    assert r.status_code == 200, r.text
    assert approvals.get(conn, r.json()["approval_id"])["task_id"] == t["id"]
    client.post("/api/worker/browser/log", headers=h, json={"tool": "browser_click", "args": {"element": "x"},
                                                             "url": "https://shop.example", "task_id": ref})
    row = conn.execute("SELECT entity, entity_id FROM audit_log WHERE action = 'browser:click'").fetchone()
    assert (row["entity"], row["entity_id"]) == ("task", t["id"])
    # An unknown task is no 500: the step is just not linked.
    r = client.post("/api/worker/browser/check", headers=h, json={
        "tool": "browser_click", "args": {"element": "Buy now"}, "url": "https://shop.example", "task_id": "T-99999"})
    assert r.status_code == 200 and approvals.get(conn, r.json()["approval_id"])["task_id"] is None


def test_guard_proxies_playwright_and_waits_for_approval():
    """End to end in its own process (stdio MCP children misbehave under pytest on
    Windows): a fake Playwright MCP behind the guard, the real API, an approval."""
    pytest.importorskip("pos_worker")
    import subprocess
    from pathlib import Path

    script = Path(__file__).with_name("browser_e2e.py")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout[-2000:] + out.stderr[-2000:]
    assert "E2E OK" in out.stdout
