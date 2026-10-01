"""Agents' files and visuals (pos.agent_files, pos.file_render) and their own computer (pos.sandbox):
create, version, restore, share in chat with an inline preview, the SVG sanitiser, DOT rendering,
limits, permissions, knowlage visibility, and the sandbox tools against a fake manager."""

import base64
import hashlib
import importlib.util
import io
import json
import socket
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, chat, files, kb_files, mcp_server, sandbox, tasks
from pos.config import Settings, get_settings
from pos.core import Ctx, Forbidden
from pos.db import connect, migrate
from pos.file_render import RenderError, check_mermaid, check_vegalite, dot_available, render_dot, sanitize_svg
from pos.main import create_app

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
DOT = "digraph LAN {\n  subgraph cluster_lan { label=\"192.168.1.0/24\"; router -> svr03; router -> ha; }\n}\n"


def _call(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def _error(result) -> str:
    assert result.is_error, result.content
    return result.content[0].text


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A database with the built-in members; the files live next to it (as in production)."""
    monkeypatch.setenv("POS_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    db = tmp_path / "personalos.db"
    conn = connect(db)
    migrate(conn)
    ids = actors.ensure_builtin(conn)
    agents.seed_builtin_permissions(conn)
    conn.commit()
    yield {"db": db, "ids": ids, "conn": conn, "owner": actors.owner_id(conn), "files": tmp_path / "files"}
    conn.close()
    get_settings.cache_clear()


def _server(env, name: str):
    return mcp_server.build(env["db"], default_actor=lambda c: env["ids"][name])


def _run(server, steps):
    async def scenario():
        async with Client(server) as c:
            return await steps(c)
    return anyio.run(scenario)


ASSISTANT = actors.ASSISTANT_NAME


# ------------------------------------------------------------------ create, version, restore, share

def test_create_update_version_read_and_restore(env):
    async def steps(c):
        f = _call(await c.call_tool("file_create", {"name": "lan", "content": DOT, "mime": "text/vnd.graphviz",
                                                    "description": "LAN topology"}))
        assert f["name"] == "lan.dot" and f["preview"] == "dot" and f["version"] == 1
        g = _call(await c.call_tool("file_update", {"file_id": f["id"], "content": DOT.replace("ha;", "ha; nas;")}))
        assert g["id"] == f["id"] and g["version"] == 2
        old = _call(await c.call_tool("file_read", {"file_id": f["id"], "version": 1}))
        new = _call(await c.call_tool("file_read", {"file_id": f["id"]}))
        assert "nas" not in old["content"] and "nas" in new["content"]
        mine = _call(await c.call_tool("file_list", {}))
        assert [x["id"] for x in mine] == [f["id"]] and mine[0]["description"] == "LAN topology"
        return f["id"]

    fid = _run(_server(env, ASSISTANT), steps)
    conn, me = env["conn"], Ctx(env["owner"])
    hist = files.history(conn, me, fid)
    assert [h["action"] for h in hist] == ["create", "update:content"]
    files.restore_version(conn, me, fid, 1)
    text, meta = files.read_text(conn, me, env["files"], fid)
    assert "nas" not in text and files.get(conn, me, fid)["version"] == 3
    # every version's bytes stay: the second is still readable
    assert "nas" in files.read_text(conn, me, env["files"], fid, version=2)[0]


def test_share_posts_a_dm_to_the_owner_with_the_file_inline(env):
    async def steps(c):
        f = _call(await c.call_tool("file_create", {"name": "lan.mmd", "content": "flowchart LR\n  a-->b\n"}))
        s = _call(await c.call_tool("file_share", {"file_id": f["id"], "message": "Naše LAN jako diagram."}))
        return f, s

    f, s = _run(_server(env, ASSISTANT), steps)
    conn = env["conn"]
    view = chat.messages(conn, env["owner"], s["channel_id"])["messages"]
    m = view[-1]
    assert m["author_id"] == env["ids"][ASSISTANT] and m["body"].startswith("Naše LAN jako diagram.")
    att = m["attachments"][0]
    assert att == {**att, "type": "file", "id": f["id"], "name": "lan.mmd", "preview": "mermaid", "version": 1}
    assert chat._channel(conn, s["channel_id"])["kind"] == "dm"


def test_share_into_a_thread_and_about_a_task(env):
    conn, owner = env["conn"], Ctx(env["owner"])
    ch = chat.create_channel(conn, owner, "infra", [ASSISTANT])
    root = chat.send(conn, owner, ch["id"], "Jak vypadá síť?")
    task = tasks.create(conn, owner, {"title": "Network map"})
    conn.commit()

    async def steps(c):
        f = _call(await c.call_tool("file_create", {"name": "net.csv", "content": "host,ip\nsvr03,192.168.1.108\n"}))
        a = _call(await c.call_tool("file_share", {"file_id": f["id"], "thread_or_task_ref": str(root["id"])}))
        b = _call(await c.call_tool("file_share", {"file_id": [f["id"]], "to": "#infra",
                                                   "thread_or_task_ref": tasks.display_id(task["id"])}))
        bad = await c.call_tool("file_share", {"file_id": f["id"], "thread_or_task_ref": "nonsense"})
        return a, b, bad

    a, b, bad = _run(_server(env, ASSISTANT), steps)
    assert a["channel_id"] == ch["id"]
    reply = conn.execute("SELECT reply_to, attachments FROM chat_messages WHERE id = ?", (a["message_id"],)).fetchone()
    assert reply["reply_to"] == root["id"] and json.loads(reply["attachments"])[0]["preview"] == "csv"
    atts = json.loads(conn.execute("SELECT attachments FROM chat_messages WHERE id = ?",
                                   (b["message_id"],)).fetchone()[0])
    assert {x["type"] for x in atts} == {"file", "task"}
    from pos import comments

    assert any("net.csv" in x["body"] for x in comments.list_for(conn, owner, task["id"]))
    assert "message id" in _error(bad)


def test_chat_send_with_attachments(env):
    async def steps(c):
        f = _call(await c.call_tool("file_create", {"name": "dot.png", "content": base64.b64encode(PNG).decode(),
                                                    "encoding": "base64"}))
        m = _call(await c.call_tool("chat_send", {"body": "Graf", "to": "David", "attachments": [f["id"]]}))
        return f, m

    env["conn"].execute("UPDATE actors SET name = 'David' WHERE id = ?", (env["owner"],))
    env["conn"].commit()
    f, m = _run(_server(env, ASSISTANT), steps)
    assert f["preview"] == "image" and f["mime"] == "image/png"
    assert m["attachments"][0]["id"] == f["id"] and "soubor #" in m["body"]


# ------------------------------------------------------------------ permissions, limits, names

def test_an_agent_cannot_read_the_owners_private_files_unless_shared(env):
    conn, owner = env["conn"], Ctx(env["owner"])
    secret = files.upload(conn, owner, env["files"], io.BytesIO(b"salary 1 000 000"), "pay.txt", visibility="private")
    conn.commit()

    async def denied(c):
        return (await c.call_tool("file_read", {"file_id": secret["id"]}),
                await c.call_tool("file_share", {"file_id": secret["id"]}),
                await c.call_tool("chat_send", {"body": "x", "channel": "team", "attachments": [secret["id"]]}))

    for r in _run(_server(env, ASSISTANT), denied):
        assert "private" in _error(r)
    from pos.visibility import share

    share(conn, "file", secret["id"], env["ids"][ASSISTANT])
    conn.commit()

    async def allowed(c):
        return _call(await c.call_tool("file_read", {"file_id": secret["id"]}))

    assert "salary" in _run(_server(env, ASSISTANT), allowed)["content"]


def test_an_agents_private_file_stays_visible_to_it_and_reaches_the_dm(env):
    async def steps(c):
        f = _call(await c.call_tool("file_create", {"name": "notes.md", "content": "# Private", "visibility": "private"}))
        assert _call(await c.call_tool("file_read", {"file_id": f["id"]}))["content"] == "# Private"
        _call(await c.call_tool("file_share", {"file_id": f["id"]}))
        return f

    f = _run(_server(env, ASSISTANT), steps)
    conn = env["conn"]
    assert files.get(conn, Ctx(env["owner"]), f["id"])["visibility"] == "private"
    with pytest.raises(Forbidden):
        files.get(conn, Ctx(env["ids"]["Nexus"]), f["id"])


def test_size_limit_quota_and_safe_names(env, monkeypatch):
    monkeypatch.setenv("POS_AGENT_FILE_MAX_MB", "1")
    monkeypatch.setenv("POS_AGENT_FILES_QUOTA_MB", "2")
    get_settings.cache_clear()
    big = "x" * (1024 * 1024 + 10)
    part = "y" * (900 * 1024)

    async def steps(c):
        too_big = await c.call_tool("file_create", {"name": "big.txt", "content": big})
        a = _call(await c.call_tool("file_create", {"name": "../../etc/passwd", "content": part}))
        b = _call(await c.call_tool("file_create", {"name": "..\\..\\b.txt", "content": part + "b"}))
        over = await c.call_tool("file_create", {"name": "c.txt", "content": part + "c"})
        # replacing a file's content counts only the difference
        same = _call(await c.call_tool("file_update", {"file_id": a["id"], "content": part + "z"}))
        return too_big, a, b, over, same

    too_big, a, b, over, same = _run(_server(env, ASSISTANT), steps)
    assert "at most 1 MB" in _error(too_big)
    assert a["name"] == "passwd" and b["name"] == "b.txt"
    assert "quota" in _error(over)
    assert same["version"] == 2
    row = env["conn"].execute("SELECT path FROM files WHERE id = ?", (a["id"],)).fetchone()
    assert ".." not in row["path"] and files.resolve(env["files"], row["path"]).is_file()


def test_every_agent_gets_the_tools(env):
    conn = env["conn"]
    new = agents.create_agent(conn, Ctx(env["owner"]), name="Fresh", purpose="test",
                              permissions=list(agents.DEFAULT_AGENT_PERMISSIONS), data_dir=env["db"].parent)
    allowed = set(mcp_server.allowed_tools(conn, new["agent"]["id"]))
    assert {"file_create", "file_update", "file_read", "file_list", "file_share", "sandbox_exec",
            "sandbox_run_python", "sandbox_write_file", "sandbox_read_file", "sandbox_list", "sandbox_reset",
            "sandbox_share"} <= allowed
    from pos_worker.prompt import FILES_GUIDE, files_guide
    from pos_worker.tools import pos_tools

    me = {"pos_tools": sorted(allowed), "profile": {"pos_tools": "get_task complete_task"}}
    shown, _ = pos_tools(me)
    assert {"sandbox_exec", "sandbox_share", "file_create", "file_share"} <= set(shown)  # never narrowed away
    assert files_guide(me) == FILES_GUIDE and "sandbox_share" in FILES_GUIDE


# ------------------------------------------------------------------ visuals

def test_svg_sanitizer_strips_scripts_foreign_objects_and_outside_references():
    dirty = b"""<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" onload="alert(1)" width="10">
  <script>alert(1)</script>
  <foreignObject><div xmlns="http://www.w3.org/1999/xhtml">hi</div></foreignObject>
  <style>@import url(https://evil.example/x.css); rect { fill: url(https://evil.example/p.svg#a) }</style>
  <rect width="5" height="5" onclick="steal()" style="fill:url(http://evil.example/a)" fill="url(#g)"/>
  <use xlink:href="https://evil.example/sprite.svg#icon"/>
  <use href="#local"/>
  <image href="http://evil.example/track.png"/>
  <image xlink:href="data:image/png;base64,iVBORw0KGgo="/>
  <a xlink:href="javascript:alert(1)"><text>link</text></a>
  <animate attributeName="href" to="javascript:alert(1)"/>
  <text x="1" y="2">LAN</text>
</svg>"""
    out = sanitize_svg(dirty).decode()
    for bad in ("script", "foreignObject", "onload", "onclick", "evil.example", "javascript", "animate", "@import",
                "<a "):
        assert bad not in out, bad
    assert 'fill="url(#g)"' in out and 'href="#local"' in out and "data:image/png" in out and ">LAN<" in out
    assert out.startswith("<svg") and 'xmlns="http://www.w3.org/2000/svg"' in out
    with pytest.raises(RenderError):
        sanitize_svg(b'<!DOCTYPE svg [<!ENTITY x "y">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>')
    with pytest.raises(RenderError):
        sanitize_svg(b"<html><body/></html>")
    # no namespace on the root: still an SVG, served with one
    assert b'xmlns="http://www.w3.org/2000/svg"' in sanitize_svg(b'<svg width="1"><circle r="1"/></svg>')


def test_uploaded_svg_is_stored_sanitised_and_served_as_an_image(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>x()</script><circle r="4"/></svg>'
        f = client.post("/api/files", files={"file": ("pic.svg", svg, "image/svg+xml")}).json()
        assert f["mime"] == "image/svg+xml" and f["preview"] == "svg"
        r = client.get(f"/api/files/{f['id']}/content")
        assert r.headers["content-type"].startswith("image/svg+xml") and b"script" not in r.content
        assert "sandbox" in r.headers["content-security-policy"]
        bad = client.post("/api/files", files={"file": ("bad.svg", b"<svg><unclosed></svg>", "image/svg+xml")})
        assert bad.status_code == 422


def test_html_pages_are_served_sandboxed_and_text_never_as_html(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        page = b"<!doctype html><script>document.body.textContent='hi'</script>"
        f = client.post("/api/files", files={"file": ("demo.html", page, "text/html")}).json()
        assert f["preview"] == "html"
        raw = client.get(f"/api/files/{f['id']}/content")
        assert raw.headers["content-type"].startswith("text/plain")
        r = client.get(f"/api/files/{f['id']}/page")
        csp = r.headers["content-security-policy"]
        assert r.headers["content-type"].startswith("text/html") and "sandbox allow-scripts" in csp
        assert "allow-same-origin" not in csp and "connect-src 'none'" in csp
        other = client.post("/api/files", files={"file": ("a.txt", b"plain", "text/plain")}).json()
        assert client.get(f"/api/files/{other['id']}/page").status_code == 400


def test_preview_kinds():
    k = files.preview_kind
    assert [k("text/vnd.mermaid", "a.mmd"), k("text/vnd.graphviz", "a.dot"), k(files.VEGALITE, "c.vl.json"),
            k("text/csv", "t.csv"), k("text/markdown", "r.md"), k("text/html", "p.html"), k("text/plain", "x.py"),
            k("text/plain", "x.txt"), k("application/pdf", "d.pdf"), k("image/svg+xml", "i.svg"),
            k("application/zip", "z.zip")] == ["mermaid", "dot", "vegalite", "csv", "markdown", "html", "code",
                                                "text", "pdf", "svg", "download"]
    assert files.sniff_mime(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "x.txt") == "image/svg+xml"
    assert files.sniff_mime(b'{"mark": "bar"}', "c.vl.json") == files.VEGALITE


def test_vegalite_and_mermaid_checks():
    check_vegalite(b'{"mark": "bar", "data": {"values": [{"a": 1}]}, "encoding": {}}')
    with pytest.raises(RenderError, match="inline"):
        check_vegalite(b'{"mark": "bar", "data": {"url": "https://evil.example/d.csv"}}')
    with pytest.raises(RenderError, match="JSON"):
        check_vegalite(b"{nope")
    assert check_mermaid("%% LAN\nflowchart LR\n a-->b") is None
    assert "not a Mermaid" in check_mermaid("graphviz LR")


@pytest.mark.skipif(not dot_available(), reason="graphviz (dot) is not installed here; the api image has it")
def test_dot_is_rendered_to_a_safe_svg(env, tmp_path):
    svg = render_dot(DOT.encode()).decode()
    assert svg.startswith("<svg") and "svr03" in svg and "<script" not in svg
    with pytest.raises(RenderError, match="read files"):
        render_dot(b'digraph { a [image="/etc/passwd"] }')
    with pytest.raises(RenderError, match="graphviz"):
        render_dot(b"digraph { a -> }")

    async def steps(c):
        return _call(await c.call_tool("file_create", {"name": "broken.dot", "content": "digraph { a -> "}))

    assert "render_error" in _run(_server(env, ASSISTANT), steps)
    with TestClient(create_app(Settings(data_dir=tmp_path / "web"))) as client:
        f = client.post("/api/files", files={"file": ("lan.dot", DOT.encode(), "text/plain")}).json()
        r = client.get(f"/api/files/{f['id']}/render.svg")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and b"svr03" in r.content
        g = client.post("/api/files", files={"file": ("bad.dot", b"digraph { a -> }", "text/plain")}).json()
        assert client.get(f"/api/files/{g['id']}/render.svg").status_code == 422


def test_an_earlier_version_is_served_by_number(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path))) as client:
        f = client.post("/api/files", files={"file": ("r.md", b"# One", "text/markdown")}).json()
        conn = connect(tmp_path / "personalos.db")
        files.update_content(conn, Ctx(actors.owner_id(conn)), tmp_path / "files", f["id"], io.BytesIO(b"# Two"))
        conn.commit()
        conn.close()
        assert client.get(f"/api/files/{f['id']}/content?v=1").text == "# One"
        assert client.get(f"/api/files/{f['id']}/content").text == "# Two"
        assert client.get(f"/api/files/{f['id']}").json()["version"] == 2


# ------------------------------------------------------------------ knowlage

class Kb:
    def __init__(self):
        self.items, self.deletes = [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.items += body.get("items", [])
        self.deletes += body.get("deletes", [])
        return httpx.Response(200, json={"added": len(body.get("items", [])), "updated": 0, "unchanged": 0})


def test_agent_files_go_to_knowlage_but_private_ones_do_not(env, monkeypatch):
    kb = Kb()
    monkeypatch.setenv("POS_KNOWLAGE_API_KEY", "k")
    monkeypatch.setenv("POS_KNOWLAGE_URL", "http://kb.test")
    monkeypatch.setattr(kb_files, "_transport", httpx.MockTransport(kb.handler))
    conn, agent = env["conn"], Ctx(env["ids"][ASSISTANT])
    settings = Settings(data_dir=env["db"].parent)
    from pos import agent_files

    team = agent_files.create(conn, agent, settings, name="plan.md", data=b"# Plan", description="the plan")
    private = agent_files.create(conn, agent, settings, name="mine.md", data=b"# Mine", visibility="private")
    conn.commit()
    kb_files.sync_pending(conn)
    keys = [i["key"] for i in kb.items]
    assert keys == [kb_files.item_key(team["id"])] and "Description: the plan" in kb.items[0]["text"]
    assert conn.execute("SELECT kb_status FROM files WHERE id = ?", (private["id"],)).fetchone()[0] == "private"
    files.update(conn, Ctx(env["owner"]), team["id"], {"visibility": "private"})
    conn.commit()
    kb_files.sync_pending(conn)
    assert kb.deletes == [{"source": "personalos", "channel": "files", "key": kb_files.item_key(team["id"])}]


# ------------------------------------------------------------------ the sandbox (against a fake manager)

class FakeManager:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.files: dict[str, bytes] = {"/workspace/lan.png": PNG}

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer sbx-token"
        op = request.url.path.strip("/")
        body = json.loads(request.content)
        self.calls.append((op, body))
        if op == "exec":
            return httpx.Response(200, json={"exit_code": 0, "stdout": f"ran {body['command']}", "stderr": "",
                                             "log": "/workspace/.logs/x.log", "timed_out": False})
        if op == "write":
            p = body["path"] if body["path"].startswith("/") else "/workspace/" + body["path"]
            self.files[p] = base64.b64decode(body["content_b64"])
            return httpx.Response(200, json={"path": p, "size": len(self.files[p])})
        if op == "read":
            p = body["path"] if body["path"].startswith("/") else "/workspace/" + body["path"]
            if p not in self.files:
                return httpx.Response(404, json={"error": f"no such file: {p}"})
            return httpx.Response(200, json={"path": p, "size": len(self.files[p]),
                                             "content_b64": base64.b64encode(self.files[p]).decode()})
        return httpx.Response(200, json={"ok": True})


def test_sandbox_tools_run_as_the_calling_agent_and_share_results(env, monkeypatch):
    fake = FakeManager()
    monkeypatch.setenv("POS_SANDBOX_TOKEN", "sbx-token")
    monkeypatch.setenv("POS_SANDBOX_URL", "http://sandbox.test")
    monkeypatch.setattr(sandbox, "_transport", httpx.MockTransport(fake.handler))

    async def steps(c):
        r = _call(await c.call_tool("sandbox_exec", {"command": "python3 -V", "timeout": 99999}))
        p = _call(await c.call_tool("sandbox_run_python", {"code": "print(1)"}))
        w = _call(await c.call_tool("sandbox_write_file", {"path": "notes.txt", "content": "hello"}))
        t = _call(await c.call_tool("sandbox_read_file", {"path": "notes.txt"}))
        b = _call(await c.call_tool("sandbox_read_file", {"path": "/workspace/lan.png"}))
        s1 = _call(await c.call_tool("sandbox_share", {"path": "/workspace/lan.png", "message": "LAN"}))
        fake.files["/workspace/lan.png"] = PNG + b"\x00"
        s2 = _call(await c.call_tool("sandbox_share", {"path": "/workspace/lan.png"}))
        missing = await c.call_tool("sandbox_share", {"path": "/workspace/nope.png"})
        return r, p, w, t, b, s1, s2, missing

    r, p, w, t, b, s1, s2, missing = _run(_server(env, ASSISTANT), steps)
    agent = str(env["ids"][ASSISTANT])
    assert r["stdout"] == "ran python3 -V"
    op, body = fake.calls[0]
    assert op == "exec" and body["agent"] == agent and body["timeout"] == 3600  # capped
    assert p["script"].startswith("/workspace/.runs/") and fake.files[p["script"]] == b"print(1)"
    assert t["content"] == "hello" and b["binary"] is True
    assert s1["file"]["preview"] == "image" and s1["file"]["version"] == 1
    assert s2["file"]["id"] == s1["file"]["id"] and s2["file"]["version"] == 2  # same path: a new version
    assert "no such file" in _error(missing)
    row = env["conn"].execute("SELECT origin, tags FROM files WHERE id = ?", (s1["file"]["id"],)).fetchone()
    assert row["origin"] == f"sandbox:{agent}:/workspace/lan.png" and "sandbox" in row["tags"]


def test_sandbox_not_set_up_says_so(env):
    async def steps(c):
        return await c.call_tool("sandbox_exec", {"command": "ls"})

    assert "not set up" in _error(_run(_server(env, ASSISTANT), steps))


# ------------------------------------------------------------------ the manager's own rules (no Docker needed)

def _manager():
    path = Path(__file__).resolve().parents[2] / "ops" / "sandbox" / "manager.py"
    spec = importlib.util.spec_from_file_location("sandbox_manager", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_proxy_reaches_only_public_addresses_and_the_api(monkeypatch):
    m = _manager()
    table = {"pypi.org": ["151.101.0.223"], "router": ["192.168.1.1"], "pos-api": ["172.20.0.5"],
             "mixed.example": ["93.184.216.34", "10.0.0.1"], "meta": ["169.254.169.254"], "v6": ["::ffff:192.168.1.1"],
             "lo": ["127.0.0.1"]}

    def fake(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port)) for ip in table[host]]

    monkeypatch.setattr(m.socket, "getaddrinfo", fake)
    assert m.resolve_allowed("pypi.org", 443) == ("151.101.0.223", 443)
    assert m.resolve_allowed("pos-api", 8000) == ("172.20.0.5", 8000)
    for host, port in [("router", 80), ("pos-api", 22), ("mixed.example", 443), ("meta", 80), ("v6", 80), ("lo", 80)]:
        with pytest.raises(PermissionError):
            m.resolve_allowed(host, port)


def test_manager_paths_and_output_clipping():
    m = _manager()
    assert m._abs("out/chart.png") == "/workspace/out/chart.png" and m._abs("/tmp/x") == "/tmp/x"
    assert m.cname("12") == "pos-sbx-12" and m.cname("../Evil Name") == "pos-sbx-evil-name"
    text, cut = m._clip(b"a" * 20000)
    assert cut and "cut" in text and len(text) < 13000
    assert m._clip(b"short") == ("short", False)
    assert hashlib.sha256(PNG).hexdigest()  # PNG fixture is valid bytes
