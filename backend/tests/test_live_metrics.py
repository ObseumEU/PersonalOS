"""GET /api/stream (pos.live) and GET /metrics (pos.metrics)."""

import anyio
from fastapi.testclient import TestClient

from pos import actors, approvals, killswitch, live, metrics, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate
from pos.main import create_app


def _db(tmp_path):
    db = tmp_path / "l.db"
    c = connect(db)
    migrate(c)
    actors.ensure_builtin(c)
    c.commit()
    return db, c


def _events(chunks: list[str]) -> list[tuple[str, str]]:
    out = []
    for ch in chunks:
        if ch.startswith("event: "):
            head, data = ch.split("\n", 1)
            out.append((head[7:], data))
    return out


async def _collect(gen, until, limit=40):
    seen = []
    for _ in range(limit):
        chunk = await gen.__anext__()
        seen.append(chunk)
        if until(_events(seen)):
            break
    return seen


def test_stream_sends_a_snapshot_then_only_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "SUBSYSTEMS_S", 3600)
    monkeypatch.setattr(live.Hub, "_subsystems", lambda self: [{"name": "Knowledge base", "ok": True}])
    monkeypatch.setattr(live, "NEEDS_MIN_S", 0)
    db, c = _db(tmp_path)
    me = Ctx(actors.owner_id(c))
    ai = Ctx(actors.assistant_id(c), via="mcp")

    async def run():
        gen = live.stream(db, me.actor_id, timeout=10)
        assert await gen.__anext__() == "retry: 3000\n\n"
        first = await _collect(gen, lambda ev: {"hello", "freeze", "needs", "presence", "subsystems"}
                               <= {n for n, _ in ev})
        names = [n for n, _ in _events(first)]
        assert names.count("needs") == 1 and '"count": 0' in dict(_events(first))["needs"]
        # an approval request: "Čeká na tebe" changes and pages hear "approval" changed
        t = tasks.create(c, me, {"title": "Send it", "assignee": "ai", "status": "next"})
        approvals.request(c, ai, "email_send", {"why": "Send the invoice"}, task_id=t["id"])
        c.commit()
        more = await _collect(gen, lambda ev: any(n == "needs" for n, _ in ev)
                              and any(n == "changed" for n, _ in ev))
        ev = _events(more)
        assert '"count": 1' in dict(ev)["needs"]
        assert "approval" in dict(ev)["changed"]
        assert not any(n == "freeze" for n, _ in ev)  # unchanged parts are not repeated
        killswitch.freeze(c, me, "test")
        live.poke()
        frozen = await _collect(gen, lambda ev: any(n == "freeze" for n, _ in ev))
        assert '"frozen": true' in dict(_events(frozen))["freeze"]
        await gen.aclose()

    anyio.run(run)
    assert live.subscriber_count() == 0
    c.close()


def test_stream_endpoint_requires_login(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, scheduler=False, password="pw", session_secret="s"))
    with TestClient(app) as client:
        assert client.get("/api/stream").status_code == 401


def test_metrics_count_routes_by_template_and_read_the_business_numbers(tmp_path, monkeypatch):
    # The counters live in the process: start from zero, whatever other tests requested.
    monkeypatch.setattr(metrics, "_requests", {})
    monkeypatch.setattr(metrics, "_hist", {})
    monkeypatch.setattr(metrics, "_locked", 0)
    settings = Settings(data_dir=tmp_path, scheduler=False)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        me = Ctx(actors.owner_id(conn))
        t = tasks.create(conn, me, {"title": "Check", "assignee": "me", "status": "review"})
        conn.execute("INSERT INTO engine_usage (at, engine, cost_usd) VALUES (strftime('%Y-%m-%dT%H:%M:%S','now'), "
                     "'codex', 0.25)")
        conn.commit()
        conn.close()
        client.get(f"/api/tasks/{t['id']}")
        client.get("/api/nothing-here")
        body = client.get("/metrics").text
    assert 'pos_http_requests_total{method="GET",route="/api/tasks/{task_id}",status="2xx"}' in body
    assert 'route="unmatched"' in body
    assert 'pos_http_request_duration_seconds_bucket{method="GET",route="/api/tasks/{task_id}",le="+Inf"} 1' in body
    assert "pos_review_queue 1" in body
    assert "pos_cost_usd_today 0.250000" in body
    assert "pos_sqlite_locked_total 0" in body


def test_metrics_are_not_served_through_a_proxy(tmp_path, monkeypatch):
    with TestClient(create_app(Settings(data_dir=tmp_path, scheduler=False))) as client:
        assert client.get("/metrics", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 404
        monkeypatch.setenv("POS_METRICS_TOKEN", "t0k")
        assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 404
        r = client.get("/metrics", headers={"Authorization": "Bearer t0k", "X-Forwarded-For": "1.2.3.4"})
        assert r.status_code == 200
    assert metrics.allowed({}, "8.8.8.8") is False
    assert metrics.allowed({}, "172.18.0.5") is True
