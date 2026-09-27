"""GET /api/needs-me: one list and one count of what waits for the owner."""

from fastapi.testclient import TestClient

from pos import actors, approvals, asks, chat, needs_me, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate


def _conn(tmp_path):
    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.execute("UPDATE actors SET name = 'David' WHERE is_owner = 1")
    c.commit()
    return c


def _setup(conn):
    me = Ctx(actors.owner_id(conn))
    ai = Ctx(actors.assistant_id(conn), via="mcp")
    t = tasks.create(conn, me, {"title": "Pick an invoice template", "assignee": "ai", "status": "next"})
    tasks.claim(conn, ai, t["id"])
    return me, ai, t


def test_collects_approvals_asks_reviews_and_mentions_once(tmp_path):
    conn = _conn(tmp_path)
    me, ai, t = _setup(conn)
    ask = asks.ask(conn, ai, title="Choose the invoice template", why="The client wants it today.", task_id=t["id"])
    ap = approvals.request(conn, ai, "email_send", {"why": "Send the invoice"}, task_id=t["id"])
    done = tasks.create(conn, me, {"title": "Draft the offer", "assignee": "ai", "status": "next"})
    tasks.claim(conn, ai, done["id"])
    tasks.update(conn, ai, done["id"], {"status": "review", "progress_note": "Draft ready"})
    cid = chat.ensure_team_channel(conn)
    chat.send(conn, ai, cid, "@David can you look at the numbers?", system=True)
    conn.commit()

    out = needs_me.collect(conn, me)
    kinds = [i["kind"] for i in out["items"]]
    assert out["count"] == len(out["items"]) == sum(out["counts"].values())
    assert out["counts"]["approval"] == 1 and out["items"][0]["id"] == ap["id"]
    assert any(i["kind"] == "ask" and i["ref"] == ask["ref"] for i in out["items"])
    assert any(i["kind"] == "review" and i["id"] == done["id"] for i in out["items"])
    mentions = [i for i in out["items"] if i["kind"] == "mention"]
    # the ask's ping and the approval's ping are not listed twice
    assert [m["detail"] for m in mentions] == ["@David can you look at the numbers?"]
    assert set(kinds) == {"approval", "ask", "review", "mention"}
    # the Tasks count of "to review" agrees with the list
    assert tasks.counts(conn, me)["to_review"] == out["counts"]["review"]

    # reading the channel clears the mention; answering the ask clears the ask
    chat.mark_read(conn, me, cid)
    tasks.complete(conn, me, ask["ticket_id"], "Minimal")
    approvals.decide(conn, me, ap["id"], True)
    conn.commit()
    left = needs_me.collect(conn, me)
    assert left["counts"] == {"approval": 0, "ask": 0, "review": 1, "mention": 0}
    conn.close()


def test_agents_get_no_approvals(tmp_path):
    conn = _conn(tmp_path)
    _, ai, t = _setup(conn)
    approvals.request(conn, ai, "email_send", {}, task_id=t["id"])
    conn.commit()
    assert needs_me.collect(conn, ai)["counts"]["approval"] == 0
    conn.close()


def test_endpoint(tmp_path):
    with TestClient(__import__("pos.main", fromlist=["create_app"]).create_app(Settings(data_dir=tmp_path))) as client:
        r = client.get("/api/needs-me")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"count", "counts", "items"} and body["count"] == 0
