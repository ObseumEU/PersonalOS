import hashlib
import io
import json

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, files, kb_files, mcp_server, notes, scheduler, tasks, topics
from pos.config import Settings
from pos.core import Ctx, Forbidden, NotFound
from pos.db import HAS_FTS5, connect, migrate
from pos.main import create_app


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "f.db")
    migrate(c)
    actors.ensure_builtin(c)
    yield c
    c.close()


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


@pytest.fixture
def ai(conn):
    return Ctx(actors.assistant_id(conn), via="mcp")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_mb=1))) as c:
        yield c


def _up(conn, ctx, tmp_path, data: bytes, name: str, **kw):
    return files.upload(conn, ctx, tmp_path / "files", io.BytesIO(data), name, **kw)


class FakeKnowlage:
    """knowlage over a mock transport: /api/ingest stores items the way knowlage
    ids them, /mcp answers the `search` tool like knowlage's MCP server does."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self.ingested: list[dict] = []
        self.searches: list[dict] = []
        self.down = False
        self.ingest_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content or b"{}")
        if request.url.path == "/api/ingest":
            assert request.headers["authorization"] == "Bearer kb-owner-key"
            if self.ingest_down:
                return httpx.Response(503, text="busy")
            report = {"added": 0, "updated": 0, "unchanged": 0, "deleted": 0, "channels": []}
            for it in body["items"]:
                self.ingested.append(it)
                did = "pe_" + hashlib.sha1(f"{it['source']}:{it['channel']}:{it['key']}".encode()).hexdigest()[:16]
                report["updated" if did in self.docs else "added"] += 1
                self.docs[did] = it
            return httpx.Response(200, json=report)
        if request.url.path == "/mcp":
            args = body["params"]["arguments"]
            self.searches.append({**args, "workspace": request.url.params.get("workspace")})
            words = args["query"].lower().split()
            hits = [did for did, it in self.docs.items() if it["source"] in args["sources"]
                    and all(w in (it["title"] + " " + it["text"]).lower() for w in words)]
            text = "\n\n".join(f'<external source="personalos" trust="untrusted" ref="firma.{did}">\n'
                               f"### {i}. `firma.{did}:c0` · chunk\n{self.docs[did]['text'][:80]}\n</external>"
                               for i, did in enumerate(reversed(hits), 1)) or "Nic nenalezeno."
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"content": [{"type": "text", "text": text}],
                                                        "isError": False}})
        return httpx.Response(404)


@pytest.fixture
def kb(monkeypatch):
    fake = FakeKnowlage()
    monkeypatch.setenv("POS_KNOWLAGE_URL", "http://knowlage.test")
    monkeypatch.setenv("POS_KNOWLAGE_API_KEY", "kb-owner-key")
    monkeypatch.setattr(kb_files, "_transport", httpx.MockTransport(fake.handler))
    return fake


def test_fts5_is_available():
    # The fallback to LIKE exists, but the build we ship with has FTS5.
    assert HAS_FTS5


def test_upload_dedupes_and_sniffs(conn, me, tmp_path):
    a = _up(conn, me, tmp_path, b"hello contract", "a.txt", topic="#Acme", tags="Legal, legal ,2026")
    assert a["duplicate"] is False and a["mime"] == "text/plain" and a["size"] == 14
    assert a["topic"] == "acme" and a["tags"] == ["legal", "2026"]
    b = _up(conn, me, tmp_path, b"hello contract", "copy.txt")
    assert b["duplicate"] is True and b["id"] == a["id"]
    stored = list((tmp_path / "files").rglob("*-a.txt"))
    assert len(stored) == 1 and stored[0].read_bytes() == b"hello contract"
    # The client's name says PDF; the bytes say PNG.
    png = _up(conn, me, tmp_path, b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "evil.pdf")
    assert png["mime"] == "image/png" and png["preview"] == "image"
    blob = _up(conn, me, tmp_path, bytes(range(256)), "x.html")
    assert blob["mime"] == "application/octet-stream" and blob["preview"] == "download"


def test_path_traversal_name_is_sanitised(conn, me, tmp_path):
    f = _up(conn, me, tmp_path, b"x", "../../etc/passwd")
    assert f["name"] == "passwd"
    assert files.safe_name("..\\..\\windows\\system32\\evil.exe") == "evil.exe"
    assert files.safe_name("../..") == "file"
    assert files.safe_name("a\x00b\n<script>.txt") == "ab_script_.txt"
    row = conn.execute("SELECT path FROM files WHERE id = ?", (f["id"],)).fetchone()
    assert ".." not in row["path"]
    path, _ = files.content(conn, me, tmp_path / "files", f["id"])
    assert (tmp_path / "files").resolve() in path.parents
    # A tampered path in the database is refused, never served.
    conn.execute("UPDATE files SET path = '../f.db' WHERE id = ?", (f["id"],))
    with pytest.raises(NotFound):
        files.content(conn, me, tmp_path / "files", f["id"])


def test_size_limit(conn, me, tmp_path):
    with pytest.raises(files.TooLarge):
        _up(conn, me, tmp_path, b"x" * 2048, "big.bin", max_bytes=1024)
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    assert not [p for p in (tmp_path / "files").rglob("*") if p.is_file()]


def test_search_finds_words_in_files_and_notes(conn, me, tmp_path, kb):
    _up(conn, me, tmp_path, "The lease ends in March; the deposit is refundable.".encode(), "lease.txt",
        topic="house")
    assert kb_files.sync_pending(conn) == {"pushed": 1, "failed": 0}
    notes.create(conn, me, {"title": "Landlord call", "body": "Ask about the **deposit** return.", "topic": "house"})
    tasks.create(conn, me, {"title": "Find the deposit receipt", "topic": "house", "status": "next"})
    found = topics.search(conn, me, "deposit")
    assert [f["name"] for f in found["files"]] == ["lease.txt"] and found["files_mode"] == "knowlage"
    assert [n["title"] for n in found["notes"]] == ["Landlord call"]
    assert [t["title"] for t in found["tasks"]] == ["Find the deposit receipt"]
    assert topics.search(conn, me, "refund")["files"]
    assert not topics.search(conn, me, "nothinglikethis")["files"]
    assert topics.search(conn, me, '") OR *')["files"] == []  # hostile query text is harmless


def test_upload_pushes_into_knowlage_and_stores_the_document_id(client, kb, tmp_path):
    r = client.post("/api/files", files={"file": ("offer.txt", b"Price offer for Acme: 110 000 CZK", "text/plain")},
                    data={"topic": "acme", "tags": "sales"})
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert len(kb.ingested) == 1
    it = kb.ingested[0]
    assert (it["source"], it["channel"], it["key"], it["title"]) == ("personalos", "files", f"file-{fid}", "offer.txt")
    assert "Price offer for Acme" in it["text"] and "Topic: acme" in it["text"] and "Tags: sales" in it["text"]
    f = client.get(f"/api/files/{fid}").json()
    assert f["kb_doc_id"] == kb_files.doc_id(fid) and f["kb_doc_id"] in kb.docs
    assert f["kb_status"] == "ok" and f["kb_error"] is None
    # Renaming marks knowlage's copy stale; the retry job pushes it again under the same key.
    client.patch(f"/api/files/{fid}", json={"name": "offer-v2.txt"})
    assert client.get(f"/api/files/{fid}").json()["kb_status"] == "pending"
    conn = connect(tmp_path / "personalos.db")
    try:
        assert kb_files.sync_pending(conn) == {"pushed": 1, "failed": 0}
    finally:
        conn.close()
    assert kb.ingested[-1]["title"] == "offer-v2.txt" and len(kb.docs) == 1
    assert client.get(f"/api/files/{fid}").json()["kb_status"] == "ok"
    s = client.get("/api/files/search", params={"q": "acme offer"}).json()
    assert s["mode"] == "knowlage" and [f["id"] for f in s["files"]] == [fid]


def test_knowlage_failure_never_fails_the_upload(client, kb):
    kb.ingest_down = True
    r = client.post("/api/files", files={"file": ("a.txt", b"alpha", "text/plain")})
    assert r.status_code == 201, r.text
    f = client.get(f"/api/files/{r.json()['id']}").json()
    assert f["kb_status"] == "error" and "503" in f["kb_error"] and f["kb_doc_id"] is None
    kb.down = True
    r = client.post("/api/files", files={"file": ("b.txt", b"beta", "text/plain")})
    assert r.status_code == 201, r.text
    f = client.get(f"/api/files/{r.json()['id']}").json()
    assert f["kb_status"] == "error" and "unreachable" in f["kb_error"]


def test_retry_job_pushes_what_failed(conn, me, tmp_path, kb):
    kb.down = True
    f = _up(conn, me, tmp_path, b"gamma", "g.txt")
    assert kb_files.ingest_file(conn, f["id"])["ok"] is False
    kb.down = False
    job = {"id": 0, "name": "x", "action": "knowlage_files", "schedule": "every 15m"}
    assert scheduler.ACTIONS[job["action"]](conn) == {"pushed": 1, "failed": 0}
    row = conn.execute("SELECT kb_status, kb_doc_id, kb_attempts FROM files WHERE id = ?", (f["id"],)).fetchone()
    assert (row["kb_status"], row["kb_doc_id"], row["kb_attempts"]) == ("ok", kb_files.doc_id(f["id"]), 2)
    assert kb_files.sync_pending(conn) == {"pushed": 0, "failed": 0}


def test_search_goes_through_knowlage_and_maps_ids(conn, me, ai, tmp_path, kb):
    a = _up(conn, me, tmp_path, b"Invoice from the plumber, 4 200 CZK", "scan-001.txt", topic="house")
    b = _up(conn, me, tmp_path, b"Invoice for Acme consulting", "acme.txt", topic="acme")
    secret = _up(conn, me, tmp_path, b"Invoice for the clinic", "clinic-invoice.txt", visibility="private")
    kb_files.sync_pending(conn)
    # A private file stays out of the shared knowledge base; its name is still matched here.
    assert all(it["key"] != kb_files.item_key(secret["id"]) for it in kb.ingested)
    found = files.search(conn, me, "invoice")
    assert found["mode"] == "knowlage"
    # knowlage's order (best first) is kept; its workspace-prefixed ids map back to local files.
    assert [f["id"] for f in found["files"]] == [b["id"], a["id"], secret["id"]]
    assert kb.searches[-1]["sources"] == ["personalos"] and kb.searches[-1]["workspace"] == "firma"
    assert [f["id"] for f in files.search(conn, me, "invoice", topic="house")["files"]] == [a["id"]]
    assert [f["id"] for f in files.list_files(conn, me, q="plumber")] == [a["id"]]  # content, not the name
    # Visibility still applies to what knowlage finds.
    assert secret["id"] not in [f["id"] for f in files.search(conn, ai, "invoice")["files"]]
    files.archive(conn, me, b["id"])
    assert b["id"] not in [f["id"] for f in files.search(conn, me, "invoice")["files"]]


def test_search_falls_back_to_file_names_when_knowlage_is_down(conn, me, tmp_path, kb):
    a = _up(conn, me, tmp_path, b"quarterly numbers", "report_2026.txt")
    _up(conn, me, tmp_path, b"report inside, not in the name", "misc.txt")
    kb_files.sync_pending(conn)
    kb.down = True
    found = files.search(conn, me, "report")
    assert found["mode"] == "filename" and "unreachable" in found["error"]
    assert [f["id"] for f in found["files"]] == [a["id"]]
    assert files.search(conn, me, "report_")["files"] == [found["files"][0]]  # '_' is literal, not a wildcard
    assert files.search(conn, me, "%")["mode"] == "none"


def test_search_without_knowlage_configured_uses_file_names(conn, me, tmp_path):
    a = _up(conn, me, tmp_path, b"hello", "budget.txt")
    found = files.search(conn, me, "budget")
    assert found["mode"] == "filename" and [f["id"] for f in found["files"]] == [a["id"]]
    assert files.search(conn, me, "hello")["files"] == []
    assert kb_files.sync_pending(conn) == {"skipped": "knowlage is not configured"}


def test_backfill_is_idempotent(conn, me, tmp_path, kb):
    old = [_up(conn, me, tmp_path, f"old file {i}".encode(), f"old{i}.txt") for i in range(3)]
    gone = _up(conn, me, tmp_path, b"archived", "gone.txt")
    files.archive(conn, me, gone["id"])
    conn.execute("UPDATE files SET kb_status = NULL")  # files from before knowlage
    conn.commit()
    assert kb_files.backfill(conn, apply=False) == {"would_push": 3, "configured": True}
    assert kb.ingested == []
    assert kb_files.backfill(conn) == {"pushed": 3, "failed": 0, "errors": []}
    assert sorted(it["key"] for it in kb.ingested) == sorted(f"file-{f['id']}" for f in old)
    assert kb_files.backfill(conn) == {"pushed": 0, "failed": 0, "errors": []}
    assert len(kb.ingested) == 3
    assert kb_files.main(["backfill", "--db", str(conn.execute("PRAGMA database_list").fetchone()["file"])]) == 0
    assert len(kb.ingested) == 3


def test_files_fts_is_gone(conn):
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'files_fts%'").fetchone() is None
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'notes_fts'").fetchone() is not None


def test_topic_aggregates_tasks_files_and_notes(conn, me, tmp_path):
    tasks.create(conn, me, {"title": "Renew contract", "topic": "acme", "status": "next"})
    tasks.create(conn, me, {"title": "Kickoff", "topic": "acme", "status": "done"})
    _up(conn, me, tmp_path, b"terms", "terms.txt", topic="acme")
    notes.create(conn, me, {"title": "Meeting notes", "body": "- one\n- two", "topic": "Acme"})
    listed = {t["slug"]: t for t in topics.list_topics(conn, me)}
    assert listed["acme"]["id"] is None  # known from tasks, files and notes only
    assert (listed["acme"]["open_tasks"], listed["acme"]["done_tasks"]) == (1, 1)
    assert (listed["acme"]["file_count"], listed["acme"]["note_count"]) == (1, 1)
    t = topics.get(conn, me, "#ACME")
    assert [x["title"] for x in t["open"]] == ["Renew contract"]
    assert [x["title"] for x in t["done"]] == ["Kickoff"]
    assert [f["name"] for f in t["files"]] == ["terms.txt"] and [n["title"] for n in t["notes"]] == ["Meeting notes"]
    described = topics.update(conn, me, "acme", {"name": "Acme Corp", "color": "#6cc4dc"})
    assert described["id"] and described["name"] == "Acme Corp"
    topics.create(conn, me, {"name": "Health"})
    assert "health" in {t["slug"] for t in topics.list_topics(conn, me)}
    topics.archive(conn, me, "health")
    assert "health" not in {t["slug"] for t in topics.list_topics(conn, me)}
    with pytest.raises(tasks.Invalid):
        topics.create(conn, me, {"name": "acme"})


def test_archive_then_restore(conn, me, tmp_path):
    f = _up(conn, me, tmp_path, b"keep me", "keep.txt")
    n = notes.create(conn, me, {"title": "Draft", "body": "v1"})
    files.archive(conn, me, f["id"])
    notes.archive(conn, me, n["id"])
    assert files.list_files(conn, me) == [] and notes.list_notes(conn, me) == []
    assert [x["id"] for x in files.list_files(conn, me, archived=True)] == [f["id"]]
    assert list((tmp_path / "files").rglob("*-keep.txt"))  # the bytes stay on disk
    files.unarchive(conn, me, f["id"])
    notes.unarchive(conn, me, n["id"])
    assert [x["id"] for x in files.list_files(conn, me)] == [f["id"]]
    notes.update(conn, me, n["id"], {"body": "v2"})
    assert notes.restore(conn, me, n["id"], 1)["body"] == "v1"
    actions = [h["action"] for h in notes.history(conn, me, n["id"])]
    assert actions == ["create", "archive", "unarchive", "update", "restore:v1"]
    assert all("text_extract" not in h["data"] for h in files.history(conn, me, f["id"]))


def test_private_file_hidden_from_agent(conn, me, ai, tmp_path):
    secret = _up(conn, me, tmp_path, b"blood test results", "lab.txt", visibility="private", topic="health")
    team = _up(conn, me, tmp_path, b"gym plan", "gym.txt", topic="health")
    note = notes.create(conn, me, {"title": "Doctor", "body": "blood pressure", "visibility": "private"})
    with pytest.raises(Forbidden):
        files.get(conn, ai, secret["id"])
    with pytest.raises(Forbidden):
        notes.get(conn, ai, note["id"])
    assert [f["id"] for f in files.list_files(conn, ai)] == [team["id"]]
    assert topics.search(conn, ai, "blood") == {"q": "blood", "tasks": [], "files": [], "files_mode": "filename",
                                                "notes": []}
    assert [f["id"] for f in topics.get(conn, ai, "health")["files"]] == [team["id"]]
    # The same bytes uploaded by the agent do not reveal the private file.
    again = _up(conn, ai, tmp_path, b"blood test results", "copy.txt")
    assert again["duplicate"] is False and again["id"] != secret["id"]


def _call(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def test_mcp_search_and_notes(tmp_path):
    db = tmp_path / "m.db"
    conn = connect(db)
    migrate(conn)
    ids = actors.ensure_builtin(conn)
    agents.seed_builtin_permissions(conn)
    me = Ctx(actors.owner_id(conn))
    f = files.upload(conn, me, tmp_path / "files", io.BytesIO(b"Quarterly invoice for Acme"), "invoice.txt",
                     topic="acme")
    files.upload(conn, me, tmp_path / "files", io.BytesIO(b"private invoice"), "mine.txt", visibility="private")
    conn.commit()
    conn.close()
    server = mcp_server.build(db, default_actor=lambda c: ids["Nexus"])
    reader = mcp_server.build(db, default_actor=lambda c: ids["Knowledge agent"])

    async def scenario():
        async with Client(server) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert {"search", "file_get", "note_create", "note_update", "topic_get"} <= names
            found = _call(await c.call_tool("search", {"q": "invoice"}))
            assert [x["name"] for x in found["files"]] == ["invoice.txt"]
            got = _call(await c.call_tool("file_get", {"file_id": f["id"]}))
            assert "Quarterly invoice" in got["text_extract"] and "<external" in got["text_extract"]
            n = _call(await c.call_tool("note_create", {"title": "Acme summary", "body": "Invoice is due.",
                                                        "topic": "acme"}))
            n = _call(await c.call_tool("note_update", {"note_id": n["id"], "body": "Invoice paid."}))
            assert n["body"] == "Invoice paid."
            t = _call(await c.call_tool("topic_get", {"slug": "acme"}))
            assert [x["title"] for x in t["notes"]] == ["Acme summary"]
        async with Client(reader) as c:
            # tasks:read only: may search, may not write notes.
            assert _call(await c.call_tool("search", {"q": "paid"}))["notes"]
            assert (await c.call_tool("note_create", {"title": "x"})).is_error

    anyio.run(scenario)


def test_files_api(client, kb):
    r = client.post("/api/files", files={"file": ("notes.md", b"# Plan\nbuy paint", "text/html")},
                    data={"topic": "house", "tags": "diy"})
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["mime"] == "text/markdown" and f["preview"] == "markdown"
    c = client.get(f"/api/files/{f['id']}/content")
    assert c.status_code == 200 and c.text.startswith("# Plan")
    assert c.headers["content-type"].startswith("text/plain")
    assert c.headers["x-content-type-options"] == "nosniff"
    assert c.headers["content-disposition"].startswith("inline;")
    d = client.get(f"/api/files/{f['id']}/content?download=1")
    assert d.headers["content-disposition"].startswith("attachment;")
    assert client.get("/api/files?q=paint").json()[0]["id"] == f["id"]
    assert client.get("/api/files?tag=diy").json()[0]["id"] == f["id"]
    assert client.patch(f"/api/files/{f['id']}", json={"tags": ["diy", "Paint"]}).json()["tags"] == ["diy", "paint"]
    assert client.patch(f"/api/files/{f['id']}", json={"path": "/etc/passwd"}).status_code == 422
    big = client.post("/api/files", files={"file": ("big.bin", b"x" * (1024 * 1024 + 10), "application/pdf")})
    assert big.status_code == 413
    assert client.post(f"/api/files/{f['id']}/archive").json()["archived_at"]
    assert client.post(f"/api/files/{f['id']}/restore").json()["archived_at"] is None
    assert [h["action"] for h in client.get(f"/api/files/{f['id']}/history").json()][0] == "create"

    n = client.post("/api/notes", json={"title": "Paint", "body": "white", "topic": "house"}).json()
    client.patch(f"/api/notes/{n['id']}", json={"body": "grey"})
    assert client.post(f"/api/notes/{n['id']}/restore", json={"version": 1}).json()["body"] == "white"
    # Unarchiving: the web posts no body; an empty {} means the same, not a 422.
    assert client.post(f"/api/notes/{n['id']}/archive").json()["archived_at"]
    assert client.post(f"/api/notes/{n['id']}/restore").json()["archived_at"] is None
    client.post(f"/api/notes/{n['id']}/archive")
    r = client.post(f"/api/notes/{n['id']}/restore", json={})
    assert r.status_code == 200 and r.json()["archived_at"] is None and r.json()["body"] == "white"
    assert client.post("/api/topics", json={"name": "House", "color": "#aabbcc"}).status_code == 201
    t = client.get("/api/topics/house").json()
    assert t["name"] == "House" and len(t["files"]) == 1 and len(t["notes"]) == 1
    s = client.get("/api/search?q=paint").json()
    assert len(s["files"]) == 1 and len(s["notes"]) == 1
    assert client.get("/api/files/999").status_code == 404


def test_topics_rename_merge_and_private(tmp_path, monkeypatch):
    from pos import accounts, actors, tasks, topics
    from pos.core import Ctx
    from pos.db import connect, migrate

    c = connect(tmp_path / "t.db")
    migrate(c)
    actors.ensure_builtin(c)
    me = Ctx(actors.owner_id(c))
    tasks.create(c, me, {"title": "A", "topic": "acme"})
    tasks.create(c, me, {"title": "B", "topic": "acme-corp"})
    out = topics.rename(c, me, "acme-corp", "acme")
    assert out["merged"] is True and out["moved"]["tasks"] == 1
    assert {t["slug"] for t in topics.list_topics(c, me)} == {"acme"}
    renamed = topics.rename(c, me, "acme", "acme-sro")
    assert renamed["merged"] is False and renamed["slug"] == "acme-sro"
    topics.update(c, me, "acme-sro", {"visibility": "private"})
    inv = accounts.invite(c, me, email="x@firma.cz", name="Xena")
    xena = Ctx(accounts.accept(c, inv["token"], "xena-password-1"))
    assert all(t["slug"] != "acme-sro" for t in topics.list_topics(c, xena))
    c.close()


def test_mcp_file_upload_and_list(tmp_path, monkeypatch):
    import anyio
    from mcp.client import Client

    from pos import actors, agents, mcp_server
    from pos.db import connect, migrate

    db = tmp_path / "personalos.db"
    c = connect(db)
    migrate(c)
    ids = actors.ensure_builtin(c)
    agents.seed_builtin_permissions(c)
    c.close()
    server = mcp_server.build(db, default_actor=lambda conn: ids["Executive Assistant"])

    async def scenario():
        async with Client(server) as cl:
            up = await cl.call_tool("file_upload", {"name": "zapis.md", "text": "# Porada\nrozhodnuti", "topic": "acme"})
            assert not up.is_error, up.content
            got = await cl.call_tool("file_list", {"topic": "acme"})
            assert "zapis.md" in str(got.structured_content or got.content)

    anyio.run(scenario)
