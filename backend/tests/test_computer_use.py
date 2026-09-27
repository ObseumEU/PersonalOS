"""Browser use and computer use for agents (docs/BROWSER.md): grants, the outbound gate,
the mounts, per-run isolation, browser_login's redaction, downloads, the desktop sandbox
and the memory caps in compose."""

import asyncio
import base64
import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from pos import actors, agents, approvals, browser
from pos.access import service as access
from pos.config import Settings
from pos.core import Ctx
from pos.credentials import onepassword
from pos.credentials import service as creds
from pos.db import connect
from pos.main import create_app

pytest.importorskip("pos_worker")
import mcp_types as types  # noqa: E402

from pos_worker import browser_guard, computer, mounts, prompt  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
SECRET = "Hunter2-very-secret-pw"
REF = "op://PersonalOS Agents/Router/password"


# ------------------------------------------------------------------ the policy (Ú1)

def test_submitting_on_a_foreign_site_asks_first_but_reading_searching_and_logging_in_do_not():
    d = browser.decide
    url = "https://forum.example.com/thread/1"
    assert d("browser_navigate", {"url": url})[0] == "allow"
    assert d("browser_fill_form", {"fields": [{"name": "Comment", "value": "x"}]}, url=url)[0] == "allow"
    assert d("browser_click", {"element": "Search"}, url=url)[0] == "allow"
    assert d("browser_click", {"element": "Log in"}, url=url)[0] == "allow"
    assert d("browser_click", {"element": "Next page"}, url=url)[0] == "allow"
    assert d("browser_click", {"element": "Post comment"}, url=url)[0] == "approval"
    assert d("browser_click", {"element": "Submit"}, url=url)[0] == "approval"
    assert d("browser_click", {"element": "Uložit"}, url=url)[0] == "approval"
    assert d("browser_type", {"element": "Title", "text": "x", "submit": True}, url=url)[0] == "approval"
    assert d("browser_press_key", {"key": "Enter"}, url=url, last_field="Your name")[0] == "approval"
    assert d("browser_press_key", {"key": "Enter"}, url=url, last_field="Search the forum")[0] == "allow"
    assert d("browser_file_upload", {"paths": ["/work/a.pdf"]}, url=url)[0] == "approval"


def test_on_an_action_host_the_agent_acts_freely_except_banking():
    ha = ["192.168.1.56:8123", "homeassistant.local:8123"]
    d = browser.decide
    assert d("browser_click", {"element": "Save"}, url="http://192.168.1.56:8123/config", action_hosts=ha)[0] == "allow"
    assert d("browser_click", {"element": "Delete automation"}, url="http://192.168.1.56:8123/x",
             action_hosts=ha)[0] == "allow"
    # the port is part of an entry that names one; another port on the same host is not the HA UI
    assert d("browser_click", {"element": "Save"}, url="http://192.168.1.56:9000/", action_hosts=ha)[0] == "approval"
    # a host entry without a port covers subdomains
    assert browser.on_action_host("https://app.nexus.obseum.cloud/x", ["nexus.obseum.cloud"])
    assert not browser.on_action_host("https://nexus.obseum.cloud.evil.com/", ["nexus.obseum.cloud"])
    assert d("browser_navigate", {"url": "https://ib.fio.cz"}, action_hosts=["fio.cz"])[0] == "approval"


def test_computer_actions_use_the_page_and_the_element_under_the_pointer():
    d = browser.decide
    url = "https://shop.example.com/cart"
    assert d("computer_left_click", {"coordinate": [5, 5]}, url=url, element="a Read more")[0] == "allow"
    assert d("computer_left_click", {"coordinate": [5, 5]}, url=url, element="button[submit] Send")[0] == "approval"
    assert d("computer_left_click", {"coordinate": [5, 5]}, url=url, element="button Buy now")[0] == "approval"
    assert d("computer_key", {"text": "Return"}, url=url, last_field="textarea[textarea] Message")[0] == "approval"
    assert d("computer_key", {"text": "Return"}, url=url, last_field="input[search] Search")[0] == "allow"
    assert d("computer_left_click", {}, url="http://192.168.1.56:8123/", element="button Save",
             action_hosts=["192.168.1.56:8123"])[0] == "allow"
    assert d("computer_open_url", {"url": "https://ib.fio.cz"})[0] == "approval"


# ------------------------------------------------------------------ grants

def test_tool_browser_and_tool_computer_are_grantable_scopes_are_the_owners():
    assert access.kind_of("tool:browser") == "tool"
    assert access.kind_of("tool:computer") == "tool"
    assert access.kind_of("scope:browser:192.168.1.56:8123") == "owner_only"
    assert access.kind_of("scope:browser-profile:github") == "owner_only"
    with pytest.raises(access.AccessError):
        access.kind_of("tool:no_such_tool")


@pytest.fixture
def app(tmp_path):
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    yield client, conn, owner, tmp_path, settings
    conn.close()
    client.__exit__(None, None, None)


def _agent(conn, owner, tmp, name, perms=("tasks:read", "tasks:claim", "approvals:request")):
    made = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", permissions=list(perms),
                               data_dir=tmp)
    conn.commit()
    return made["agent"]["id"], made["api_key"]


def test_seed_grants_once_and_a_revoke_stays(app):
    client, conn, owner, tmp, _ = app
    ha, _ = _agent(conn, owner, tmp, "Home Assistant Specialist")
    sre, _ = _agent(conn, owner, tmp, "SRE")
    ceo, _ = _agent(conn, owner, tmp, "CEO")
    other, _ = _agent(conn, owner, tmp, "Community Manager")
    done = browser.ensure_grants(conn)
    assert "CEO|tool:browser" in done and "SRE|tool:computer" in done
    assert {"tool:browser", "tool:computer", "scope:browser:192.168.1.56:8123"} <= agents.permissions_of(conn, ha)
    assert "tool:computer" not in agents.permissions_of(conn, ceo) and "tool:browser" in agents.permissions_of(conn, ceo)
    assert not {"tool:browser", "tool:computer"} & agents.permissions_of(conn, other)
    assert browser.action_hosts(conn, ha) == ["192.168.1.56:8123", "homeassistant.local:8123"]
    access.revoke(conn, owner, ceo, "tool:browser", "not needed")
    assert browser.ensure_grants(conn) == []
    assert "tool:browser" not in agents.permissions_of(conn, ceo)
    # the Access manager may grant tool:computer to someone else, never an action host
    am = Ctx(access.manager_id(conn), via="mcp")
    access.grant(conn, am, other, "tool:computer", "needs a GUI for the forum admin")
    assert browser.may_use_computer(conn, other)
    with pytest.raises(Exception):
        access.grant(conn, am, other, "scope:browser:forum.example.com", "post freely")


def test_check_endpoint_needs_the_right_grant_and_uses_the_action_hosts(app):
    client, conn, owner, tmp, _ = app
    aid, key = _agent(conn, owner, tmp, "Home Assistant Specialist")
    h = {"Authorization": f"Bearer {key}"}
    body = {"tool": "browser_click", "args": {"element": "Save"}, "url": "http://192.168.1.56:8123/config",
            "dry_run": True}
    assert client.post("/api/worker/browser/check", json=body, headers=h).json()["decision"] == "refuse"
    browser.ensure_grants(conn)
    assert client.post("/api/worker/browser/check", json=body, headers=h).json()["decision"] == "allow"
    foreign = client.post("/api/worker/browser/check", headers=h, json={
        **body, "url": "https://forum.example.com", "dry_run": False, "screenshot": PNG}).json()
    assert foreign["decision"] == "approval" and approvals.get(conn, foreign["approval_id"])["status"] == "pending"
    comp = {"tool": "computer_left_click", "args": {"coordinate": [1, 2]}, "url": "https://x.example",
            "element": "button Send", "dry_run": True}
    assert client.post("/api/worker/browser/check", json=comp, headers=h).json()["decision"] == "approval"
    policy = client.get("/api/worker/browser/policy", headers=h).json()
    assert policy["browser"] and policy["computer"] and "192.168.1.56:8123" in policy["action_hosts"]
    # a log with the run id lands on that run's trace, with the screenshot
    run_id = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', "
                          "'2026-01-01T00:00:00+00:00')", (aid,)).lastrowid
    conn.commit()
    client.post("/api/worker/browser/log", headers=h, json={"tool": "computer_left_click", "args": {"coordinate": [1, 2]},
                                                            "url": "https://x.example", "screenshot": PNG, "run_id": run_id})
    row = conn.execute("SELECT run_id, detail FROM audit_log WHERE action = 'computer:left_click'").fetchone()
    assert row["run_id"] == run_id and json.loads(row["detail"])["screenshot"].endswith(".png")


def test_browser_login_credential_only_on_its_hosts_with_the_run_session(app, monkeypatch):
    client, conn, owner, tmp, _ = app

    class FakeOP:
        def resolve(self, ref):
            return SECRET

    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_test")
    monkeypatch.setenv("POS_OP_VAULT", "PersonalOS Agents")
    onepassword.set_provider(FakeOP())
    try:
        aid, key = _agent(conn, owner, tmp, "Home Assistant Specialist")
        browser.ensure_grants(conn)
        h = {"Authorization": f"Bearer {key}"}
        creds.add(conn, owner, {"name": "router", "op_ref": REF, "allowed_hosts": ["192.168.1.1:443", "router.lan"],
                                "allowed_tools": ["browser"], "max_uses_hour": 10})
        creds.grant(conn, owner, aid, "router", "router admin")
        run_id = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', "
                              "'2026-01-01T00:00:00+00:00')", (aid,)).lastrowid
        conn.commit()
        token = client.post("/api/worker/credentials/session", json={"run_id": run_id}, headers=h).json()["token"]
        body = {"name": "router", "url": "http://router.lan/login", "run_id": run_id}
        assert client.post("/api/worker/browser/credential", json=body, headers=h).status_code == 403
        hs = {**h, "X-POS-Cred-Session": token}
        ok = client.post("/api/worker/browser/credential", json=body, headers=hs)
        assert ok.status_code == 200 and ok.json()["value"] == SECRET
        bad = client.post("/api/worker/browser/credential", json={**body, "url": "https://evil.example.com/login"},
                          headers=hs)
        assert bad.status_code == 403 and "not allowed" in bad.json()["detail"]
        plain = client.post("/api/worker/browser/credential", json={**body, "url": "http://evil.example.com/"},
                            headers=hs)
        assert plain.status_code == 403
        # never in the database (logs, uses, audit)
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            for r in conn.execute(f'SELECT * FROM "{t}"'):
                assert SECRET not in " ".join(str(v) for v in r), t
    finally:
        onepassword.set_provider(None)


# ------------------------------------------------------------------ the worker: mounts and instructions

def test_mounts_only_with_the_grant(monkeypatch):
    monkeypatch.delenv("DESKTOP_URL", raising=False)
    me = {"permissions": ["tasks:read"], "run_id": 7, "task_ref": "T-1"}
    assert mounts.browser_server(me, "http://api", "k", "/work/a") == {}
    assert mounts.computer_server(me, "http://api", "k", "/work/a") == {}
    assert mounts.claude_allowed(me) == []
    me["permissions"] = ["tool:browser", "tool:computer"]
    b = mounts.browser_server(me, "http://api", "k", "/work/a", {"POS_CRED_SESSION": "sess"})["browser"]
    assert b["args"] == ["-m", "pos_worker.browser_guard"]
    assert b["env"]["POS_RUN_ID"] == "7" and b["env"]["POS_CRED_SESSION"] == "sess" and b["env"]["POS_TASK_ID"] == "T-1"
    assert "POS_CRED_SESSION" not in mounts.browser_server(me, "http://api", "k", "/w")["browser"]["env"]
    assert mounts.computer_server(me, "http://api", "k", "/work/a") == {}  # no desktop configured
    monkeypatch.setenv("DESKTOP_URL", "http://desktop:8100")
    monkeypatch.setenv("DESKTOP_TOKEN", "t")
    c = mounts.computer_server(me, "http://api", "k", "/work/a")["computer"]
    assert c["env"]["DESKTOP_URL"] == "http://desktop:8100" and c["args"] == ["-m", "pos_worker.computer"]
    assert mounts.claude_allowed(me) == ["mcp__browser", "mcp__computer"]
    lines = mounts.codex_config({**mounts.browser_server(me, "u", "k", "/w"), **mounts.computer_server(me, "u", "k", "/w")})
    assert any(line.startswith("mcp_servers.computer.args=") for line in lines)
    assert 'mcp_servers.browser.env.POS_RUN_ID="7"' in lines
    assert mounts.has_browser({"permissions": ["browser:use"]})  # the older permission still works


def test_the_guidance_is_only_for_agents_with_the_tools():
    assert prompt.web_guide({"permissions": ["tasks:read"]}) == ""
    g = prompt.web_guide({"permissions": ["tool:browser"]})
    assert "browser_snapshot" in g and "Ú1" in g and "computer_screenshot" not in g
    assert "computer_screenshot" in prompt.web_guide({"permissions": ["tool:browser", "tool:computer"]})
    assert "# Browser and computer use" in prompt.stable_prompt({"name": "X", "permissions": ["tool:browser"]})


# ------------------------------------------------------------------ the browser guard

def _guard(monkeypatch, tmp_path, run_id="11"):
    monkeypatch.setenv("POS_AGENT_KEY", "k")
    monkeypatch.setenv("POS_RUN_ID", run_id)
    monkeypatch.setenv("WORKER_WORKDIR", str(tmp_path))
    monkeypatch.setenv("PLAYWRIGHT_MCP", "playwright-mcp --browser chromium --no-sandbox")
    return browser_guard.Guard()


def test_each_run_gets_a_fresh_isolated_profile_unless_a_profile_is_granted(monkeypatch, tmp_path):
    a = _guard(monkeypatch, tmp_path, "1").playwright().args
    b = _guard(monkeypatch, tmp_path, "2").playwright().args
    assert "--isolated" in a and "--headless" in a and "--user-data-dir" not in a
    assert a[a.index("--output-dir") + 1] != b[b.index("--output-dir") + 1]
    cfg = json.loads(Path(a[a.index("--config") + 1]).read_text())
    assert "--js-flags=--max-old-space-size=256" in cfg["browser"]["launchOptions"]["args"]
    g = _guard(monkeypatch, tmp_path, "3")
    g.profile = "GitHub"
    args = g.playwright().args
    assert "--isolated" not in args and args[args.index("--user-data-dir") + 1].endswith(str(Path(".browser-profiles") / "github"))


class FakePW:
    """Playwright MCP stand-in: records calls; the page echoes what was typed (like a snapshot would)."""

    def __init__(self):
        self.calls, self.typed = [], ""

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "browser_take_screenshot":
            return types.CallToolResult(content=[types.ImageContent(type="image", data=PNG, mime_type="image/png")])
        if name == "browser_type":
            self.typed = args["text"]
            return types.CallToolResult(content=[types.TextContent(
                type="text", text=f"await page.getByRole('textbox').fill('{args['text']}')")])
        if name == "browser_evaluate":
            return types.CallToolResult(content=[types.TextContent(type="text", text='"http://router.lan/login"')])
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"- textbox: {self.typed}")])


class FakePos:
    def __init__(self, decision="allow"):
        self.posts, self.decision = [], decision

    async def post(self, path, json=None, headers=None):
        self.posts.append((path, json, headers))

        class R:
            status_code = 200

            def __init__(self, data):
                self.data = data

            def json(self):
                return self.data

        if path.endswith("/credential"):
            return R({"name": "router", "value": SECRET})
        if path.endswith("/check"):
            return R({"decision": self.decision, "reason": "test", "approval_id": 5})
        return R({})

    async def get(self, path):
        class R:
            def json(self):
                return {"status": "rejected"}

        return R()


def test_browser_login_never_shows_the_value_and_redacts_it_afterwards(monkeypatch, tmp_path):
    g = _guard(monkeypatch, tmp_path)
    monkeypatch.setenv("POS_CRED_SESSION", "sess")
    g.pos, pw = FakePos(), FakePW()
    res = asyncio.run(g.call(pw, "browser_login", {"credential": "router", "element": "Password", "ref": "e3"}))
    shown = " ".join(getattr(c, "text", "") for c in res.content)
    assert not res.is_error and "Filled router" in shown and SECRET not in shown and "[REDACTED:router]" in shown
    assert pw.typed == SECRET  # it did reach the page
    assert any(n == "browser_evaluate" and "TextSecurity" in a.get("function", "") for n, a in pw.calls)  # masked
    snap = asyncio.run(g.call(pw, "browser_snapshot", {}))
    assert SECRET not in snap.content[0].text and "[REDACTED:router]" in snap.content[0].text
    assert 'trust="untrusted"' in snap.content[0].text
    logged = [b for p, b, _ in g.pos.posts if p.endswith("/log")]
    assert logged and SECRET not in json.dumps(logged)
    cred = [(b, h) for p, b, h in g.pos.posts if p.endswith("/credential")]
    assert cred[0][0]["url"] == "http://router.lan/login" and cred[0][1] == {"X-POS-Cred-Session": "sess"}


def test_an_outbound_action_waits_for_the_owner_and_is_not_done_when_rejected(monkeypatch, tmp_path):
    g = _guard(monkeypatch, tmp_path)
    g.pos, pw, g.poll_s = FakePos("approval"), FakePW(), 0
    res = asyncio.run(g.call(pw, "browser_click", {"element": "Post comment", "ref": "e9"}))
    assert res.is_error and "approval #5 is rejected" in res.content[0].text
    assert not any(n == "browser_click" for n, _ in pw.calls)
    checks = [b for p, b, _ in g.pos.posts if p.endswith("/check")]
    assert checks[0]["dry_run"] and checks[1]["screenshot"] == PNG and checks[1]["run_id"] == "11"


def test_screenshots_are_capped_per_run_and_land_on_the_trace(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_MAX_SCREENSHOTS", "2")
    g = _guard(monkeypatch, tmp_path)
    g.pos, pw = FakePos(), FakePW()
    got = [asyncio.run(g.call(pw, "browser_take_screenshot", {})) for _ in range(3)]
    assert [r.is_error for r in got] == [False, False, True] and "browser_snapshot" in got[2].content[0].text
    assert sum(1 for p, b, _ in g.pos.posts if p.endswith("/log") and b["screenshot"] == PNG) == 2


def test_downloads_go_to_the_workspace_programs_and_big_files_to_quarantine(monkeypatch, tmp_path):
    monkeypatch.setenv("BROWSER_MAX_DOWNLOAD_MB", "1")
    g = _guard(monkeypatch, tmp_path)
    g.out_dir.mkdir(parents=True)
    (g.out_dir / "report.pdf").write_bytes(b"%PDF-1.7 hello")
    (g.out_dir / "setup.exe").write_bytes(b"MZ\x90\x00")
    (g.out_dir / "notes.txt").write_bytes(b"#!/bin/sh\nrm -rf /")  # a script by content
    (g.out_dir / "big.zip").write_bytes(b"0" * (2 * 1024 * 1024))
    (g.out_dir / "page-2026.png").write_bytes(b"png")  # a Playwright screenshot, not a download
    notes = g.scan_downloads()
    assert (tmp_path / "downloads" / "report.pdf").is_file()
    assert {p.name for p in (g.out_dir / "quarantine").iterdir()} == {"setup.exe", "notes.txt", "big.zip"}
    assert len(notes) == 4 and g.scan_downloads() == []


def test_browser_slots_cap_concurrent_browsers(tmp_path):
    pytest.importorskip("fcntl")
    a = browser_guard.Slot(str(tmp_path), 1, 0)
    b = browser_guard.Slot(str(tmp_path), 1, 0)
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


# ------------------------------------------------------------------ the desktop sandbox

def _desktop_module():
    spec = importlib.util.spec_from_file_location("desktop_server", ROOT / "ops" / "desktop" / "desktop_server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_desktop_starts_lazily_one_at_a_time_and_cleans_up(tmp_path, monkeypatch):
    mod = _desktop_module()
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    launched = []
    d = mod.Desktop(launcher=lambda url: launched.append(url))
    assert launched == [] and d.state() == {"busy": False, "holder": None, "waiting": 0}  # nothing runs idle
    s1 = d.open("agent#1", 1)
    assert launched == ["about:blank"] and Path(s1.dir).is_dir()
    assert d.open("agent#2", 0) is None  # busy: one desktop at a time
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("s", d.open("agent#2", 10)))
    t.start()
    time.sleep(0.3)
    assert d.state()["waiting"] == 1
    assert d.close(s1.id)
    t.join(5)
    assert got["s"] and got["s"].holder == "agent#2" and not Path(s1.dir).exists()
    # idle too long: the reaper ends it
    monkeypatch.setattr(mod, "IDLE_S", -1)
    with d.cv:
        d._reap()
    assert d.session is None


def test_the_desktop_service_needs_its_token(tmp_path):
    mod = _desktop_module()
    import http.client
    from http.server import ThreadingHTTPServer

    d = mod.Desktop(launcher=lambda url: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(d, "tok"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def req(method, path, token=None, body=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request(method, path, body=json.dumps(body) if body else None,
                      headers={"Authorization": f"Bearer {token}"} if token else {})
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"{}")

        assert req("GET", "/health")[0] == 200
        assert req("POST", "/session", body={"holder": "x"})[0] == 401
        assert req("POST", "/session", "wrong", {"holder": "x"})[0] == 401
        code, s = req("POST", "/session", "tok", {"holder": "x"})
        assert code == 200 and s["session"]
        assert req("POST", "/session", "tok", {"holder": "y", "wait_s": 0})[0] == 409
        assert req("DELETE", f"/session/{s['session']}", "tok")[1] == {"closed": True}
    finally:
        srv.shutdown()
        d.close(d.session.id) if d.session else None
    assert mod.xkey("ctrl+enter") == "ctrl+Return" and mod.xkey("Page_Down") == "Page_Down"


class FakeDesktop:
    def __init__(self):
        self.started = 0
        self.actions = []
        self.closed = False

    async def ensure(self, wait_s):
        self.started += 1 if not self.actions and self.started == 0 else 0

    async def act(self, action, args):
        self.actions.append((action, args))
        if action in ("screenshot", "zoom"):
            return {"image": PNG, "width": 1280, "height": 800}
        return {"ok": True}

    async def get(self, path, **params):
        return {"url": "https://forum.example.com/t/1", "at": "button[submit] Post reply", "focused": None}

    async def close(self):
        self.closed = True


def test_computer_use_gates_actions_and_caps_screenshots(monkeypatch):
    monkeypatch.setenv("POS_AGENT_KEY", "k")
    monkeypatch.setenv("POS_RUN_ID", "3")
    monkeypatch.setenv("DESKTOP_MAX_SCREENSHOTS", "1")
    desk = FakeDesktop()
    g = computer.ComputerGuard(desk, FakePos("approval"))
    g.poll_s = 0
    assert desk.started == 0  # lazily: nothing before the first call
    shot = asyncio.run(g.call("computer_screenshot", {}))
    assert shot.content[0].type == "image" and desk.started == 1
    assert asyncio.run(g.call("computer_screenshot", {})).is_error  # cap
    click = asyncio.run(g.call("computer_left_click", {"coordinate": [100, 200]}))
    assert click.is_error and "approval #5" in click.content[0].text
    assert ("left_click", {"coordinate": [100, 200]}) not in desk.actions
    check = [b for p, b, _ in g.pos.posts if p.endswith("/check")][0]
    assert check["tool"] == "computer_left_click" and check["element"] == "button[submit] Post reply"
    g.pos = FakePos("allow")
    ok = asyncio.run(g.call("computer_left_click", {"coordinate": [100, 200]}))
    assert not ok.is_error and ("left_click", {"coordinate": [100, 200]}) in desk.actions
    assert [n.name for n in computer.TOOLS][:3] == ["computer_screenshot", "computer_zoom", "computer_left_click"]


# ------------------------------------------------------------------ compose: memory caps

def test_compose_caps_memory_and_isolates_the_desktop():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    assert c["agent-pool"]["mem_limit"] and c["desktop"]["mem_limit"]
    assert c["desktop"]["networks"] == ["desktop"] and "desktop" in c["agent-pool"]["networks"]
    assert c["desktop"]["init"] is True and c["desktop"]["profiles"] == ["agents"]
    env = c["agent-pool"]["environment"]
    assert env["DESKTOP_URL"] == "http://desktop:8100" and "BROWSER_MAX_MB" in env and "BROWSER_MAX_CONCURRENT" in env
    assert c["desktop"]["tmpfs"] == ["/tmp:size=256m"] and "no-new-privileges:true" in c["desktop"]["security_opt"]
