"""Browser guard end to end (run by test_browser.py in its own process)."""
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
from mcp.client import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402

import test_browser as tb  # noqa: E402
from pos import actors, agents, approvals  # noqa: E402
from pos.config import Settings  # noqa: E402
from pos.core import Ctx  # noqa: E402
from pos.db import connect  # noqa: E402
from pos.main import create_app  # noqa: E402

tmp = Path(tempfile.mkdtemp())
settings = Settings(data_dir=tmp)
client = TestClient(create_app(settings))
client.__enter__()
conn = connect(settings.db_path)
owner = Ctx(actors.owner_id(conn))
key = agents.create_agent(conn, owner, name="Browser", purpose="web research", lifetime="long_lived",
                          permissions=["tasks:read", "tasks:claim", "approvals:request", "browser:use"],
                          data_dir=tmp)["api_key"]
conn.commit()
os.environ["POS_AGENT_KEY"] = key
fake = tmp / "fake_playwright.py"
fake.write_text(tb.FAKE_PLAYWRIGHT)

from pos_worker import browser_guard  # noqa: E402

guard = browser_guard.Guard()
guard.poll_s = 0.1
h = {"Authorization": f"Bearer {key}"}


class Bridge:  # the guard's PersonalOS client, served by the TestClient
    async def post(self, path, json):
        return await anyio.to_thread.run_sync(lambda: client.post(path, json=json, headers=h))

    async def get(self, path):
        return await anyio.to_thread.run_sync(lambda: client.get(path, headers=h))


guard.pos = Bridge()


def approve_soon():
    for _ in range(200):
        c = connect(settings.db_path)
        pending = approvals.pending(c)
        if pending:
            approvals.decide(c, owner, pending[0]["id"], True)
            c.commit()
            c.close()
            return
        c.close()
        time.sleep(0.1)


async def main():
    with anyio.fail_after(60):
        async with Client(StdioServerParameters(command=sys.executable, args=[str(fake)])) as pw:
            nav = await guard.call(pw, "browser_navigate", {"url": "https://news.example"})
            assert 'trust="untrusted"' in nav.content[0].text, nav
            th = threading.Thread(target=approve_soon)
            th.start()
            click = await guard.call(pw, "browser_click", {"element": "Pay now", "ref": "e1"})
            th.join()
            assert not click.is_error and "clicked Pay now" in click.content[0].text, click


anyio.run(main)
c = connect(settings.db_path)
actions = [r["action"] for r in c.execute("SELECT action FROM audit_log WHERE action LIKE 'browser:%'")]
assert actions == ["browser:navigate", "browser:click"], actions
assert c.execute("SELECT status FROM approvals").fetchone()["status"] == "approved"
shots = list((tmp / "files" / "browser").rglob("*.png"))
assert len(shots) >= 3, shots  # the approval screenshot and one per action
client.__exit__(None, None, None)
print("E2E OK")
