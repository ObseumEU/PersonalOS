"""The owner handoff end to end (run by test_handoff.py in its own process): the real guard over stdio with a
fake Playwright MCP, the real API, and a thread that plays the owner on his phone (opens it, sees a frame, taps,
types his password, presses Hotovo). The agent's call returns, the session is the same browser, the password is
redacted from what the agent reads and stored nowhere."""
import base64
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import anyio

os.environ["POS_CODEX_DISABLED"] = "1"
sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient  # noqa: E402

from pos import actors, agents, browser  # noqa: E402
from pos.config import Settings  # noqa: E402
from pos.core import Ctx, now_iso  # noqa: E402
from pos.db import connect  # noqa: E402
from pos.main import create_app  # noqa: E402

PASSWORD = "Moje-Heslo-2026"
JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 64).decode()
FAKE_PLAYWRIGHT = r'''
import json, os, re
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("fake-playwright")
LOG = os.environ["FAKE_PW_LOG"]
state = {"url": "about:blank", "typed": ""}


@mcp.tool()
def browser_navigate(url: str) -> str:
    state["url"] = url
    return f"### Page\n- Page URL: {url}\n- Page Title: Login"


@mcp.tool()
def browser_run_code_unsafe(code: str) -> str:
    tag = re.search(r"/\*pos:(\w+)\*/", code).group(1)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(tag + "\n")
    if tag == "hinput":
        events = json.loads(re.search(r"const ev = (\[.*?\]); const v", code).group(1))
        for e in events:
            if e["t"] == "text":
                state["typed"] += e["text"]
            if e["t"] == "key" and e["key"] == "Enter" and state["typed"]:
                state["url"] = "https://www.linkedin.com/feed/"  # logged in
    if tag == "text":  # the agent reads the page: it shows what was typed (a careless site)
        return "### Result\n" + json.dumps({"url": state["url"], "title": "Feed", "total": 30,
                                             "text": "Welcome back! Your password " + state["typed"]})
    val = {"screenshot": {"data": "''' + JPEG + r'''", "vw": 1280, "vh": 800, "url": state["url"], "title": "Login",
                          "tabs": 1},
           "hframe": {"data": "''' + JPEG + r'''", "vw": 1280, "vh": 800, "url": state["url"], "title": "Login"},
           "hdone": "/feed" in state["url"],
           "info": {"url": state["url"], "title": "Login", "tabs": 1}}.get(tag, True)
    return "### Result\n" + json.dumps(val) + "\n### Ran Playwright code\n```js\n" + code + "\n```"


mcp.run()
'''

tmp = Path(tempfile.mkdtemp())
settings = Settings(data_dir=tmp)
client = TestClient(create_app(settings))
client.__enter__()
conn = connect(settings.db_path)
owner = Ctx(actors.owner_id(conn))
made = agents.create_agent(conn, owner, name="Content & Brand", purpose="posts", lifetime="long_lived",
                           permissions=["tasks:read", "tasks:claim", "approvals:request", "browser:use"], data_dir=tmp)
key, agent_id = made["api_key"], made["agent"]["id"]
run_id = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', ?)",
                      (agent_id, now_iso())).lastrowid
conn.commit()
fake = tmp / "fake_playwright.py"
fake.write_text(FAKE_PLAYWRIGHT)
os.environ.update(POS_AGENT_KEY=key, POS_RUN_ID=str(run_id), WORKER_WORKDIR=str(tmp / "work"),
                  FAKE_PW_LOG=str(tmp / "pw.log"), PLAYWRIGHT_MCP=f'"{sys.executable}" "{fake}"')

from pos_worker import browser_guard  # noqa: E402

guard = browser_guard.Guard()
h = {"Authorization": f"Bearer {key}"}


class Bridge:  # the guard's PersonalOS client, served by the TestClient
    async def post(self, path, json=None, headers=None):
        return await anyio.to_thread.run_sync(lambda: client.post(path, json=json, headers={**h, **(headers or {})}))

    async def put(self, path, json=None):
        return await anyio.to_thread.run_sync(lambda: client.put(path, json=json, headers=h))

    async def get(self, path, params=None):
        return await anyio.to_thread.run_sync(lambda: client.get(path, params=params, headers=h))


guard.pos = Bridge()
seen = {}


def the_owner():
    """On his phone: the push opens /m/handoff/<id>; he sees the page, taps the field, types, Enter, Hotovo."""
    for _ in range(300):
        items = [i for i in client.get("/api/needs-me").json()["items"] if i["kind"] == "handoff"]
        if items:
            break
        time.sleep(0.1)
    hid = items[0]["id"]
    seen["item"] = items[0]
    assert client.post(f"/api/handoffs/{hid}/open", json={"app": "m"}).json()["status"] == "active"
    for _ in range(100):
        f = client.get(f"/api/handoffs/{hid}/frame", params={"after": 0, "wait": 2})
        if f.status_code == 200:
            seen["frame"] = f.headers["x-frame-version"]
            break
    client.post(f"/api/handoffs/{hid}/input", json={"events": [
        {"t": "click", "x": 0.5, "y": 0.4}, {"t": "text", "text": PASSWORD}, {"t": "key", "key": "Enter"}]})
    for _ in range(100):  # the guard takes the input; the page reaches the feed: done by the hint or by him
        st = client.get(f"/api/handoffs/{hid}").json()["status"]
        if st == "done":
            seen["by"] = "hint"
            return
        time.sleep(0.1)
    client.post(f"/api/handoffs/{hid}/done", json={"keep_login": True})
    seen["by"] = "owner"


async def main():
    with anyio.fail_after(90):
        await guard.load_policy()
        guard.engine = browser_guard.Engine(guard.playwright, browser_guard.Slot(str(tmp / "slots"), 2, 5), 0, 30)
        early = await guard.call("browser_request_owner_handoff", {"title": "x"})
        assert early.is_error and "Open the page first" in early.content[0].text
        await guard.call("browser_navigate", {"url": "https://www.linkedin.com/login"})
        th = threading.Thread(target=the_owner)
        th.start()
        res = await guard.call("browser_request_owner_handoff", {
            "title": "Přihlas se do LinkedIn – zbytek udělám já", "reason": "Připojuju LinkedIn.",
            "done_url_contains": "/feed"})
        th.join()
        text = res.content[0].text
        assert not res.is_error and "done" in text and "linkedin.com/feed" in text, text
        read = await guard.call("browser_get_page_text", {})
        rt = "\n".join(getattr(c, "text", "") for c in read.content)
        assert PASSWORD not in rt and "REDACTED" in rt, rt  # what he typed never reaches the agent
        await guard.close()


anyio.run(main)
log = (tmp / "pw.log").read_text().split()
assert "hinput" in log and "hframe" in log and "hdone" in log, log
assert seen["item"]["m_link"].startswith("/m/handoff/") and seen["frame"]
c = connect(settings.db_path)
dump = "\n".join(str(tuple(r)) for r in c.execute("SELECT * FROM audit_log"))
assert PASSWORD not in dump
actions = [r["action"] for r in c.execute("SELECT action FROM audit_log ORDER BY id")]
assert "handoff_requested" in actions and "handoff_opened" in actions and "handoff_done" in actions, actions
assert "browser:request_owner_handoff" in actions, actions
row = c.execute("SELECT status, finished_by FROM browser_handoffs").fetchone()
assert row["status"] == "done", dict(row)
if seen["by"] == "owner":
    assert browser.may_keep_profile(c, agent_id)
client.__exit__(None, None, None)
print("HANDOFF E2E OK", json.dumps({"finished_by": row["finished_by"]}))
