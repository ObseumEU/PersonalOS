"""The "last seen" bookkeeping never fails the call it rides on, and never holds the write lock through a tool
(prod 2026-10-11/13: complete_task, sandbox_run_python, /api/worker/next answered "database is locked" from it)."""
import sqlite3

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import actors, agents, db, mcp_server
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate
from pos.main import create_app


@pytest.fixture(autouse=True)
def short_busy_timeout(monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 300)


def _locker(path):
    c = sqlite3.connect(path, timeout=0.1, isolation_level=None)
    c.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    return c


def test_an_mcp_read_tool_answers_while_another_writer_holds_the_lock(tmp_path):
    path = tmp_path / "m.db"
    conn = connect(path)
    migrate(conn)
    agent = actors.ensure_builtin(conn)["Nexus"]
    agents.seed_builtin_permissions(conn)
    conn.close()
    server = mcp_server.build(path, default_actor=lambda c: agent)

    async def scenario():
        async with Client(server) as c:
            locker = _locker(path)
            try:
                res = await c.call_tool("list_tasks", {"view": "board"})
            finally:
                locker.rollback()
                locker.close()
            assert not res.is_error, res.content

    anyio.run(scenario)


def test_the_tool_call_is_audited_and_committed_before_the_tool_runs(tmp_path):
    path = tmp_path / "m.db"
    conn = connect(path)
    migrate(conn)
    agent = actors.ensure_builtin(conn)["Nexus"]
    agents.seed_builtin_permissions(conn)
    conn.close()
    server = mcp_server.build(path, default_actor=lambda c: agent)
    seen = {}

    real = mcp_server.tasks.list_tasks

    def spy(conn, *a, **kw):
        seen["in_transaction"] = conn.in_transaction  # the bookkeeping is committed: no write lock held here
        return real(conn, *a, **kw)

    mp = pytest.MonkeyPatch()
    mp.setattr(mcp_server.tasks, "list_tasks", spy)
    try:
        async def scenario():
            async with Client(server) as c:
                assert not (await c.call_tool("list_tasks", {"view": "board"})).is_error

        anyio.run(scenario)
    finally:
        mp.undo()
    assert seen["in_transaction"] is False
    c2 = connect(path)
    assert c2.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'mcp:list_tasks'").fetchone()[0] == 1
    c2.close()


def test_a_worker_call_answers_while_another_writer_holds_the_lock(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        conn = connect(settings.db_path)
        out = agents.create_agent(conn, Ctx(actors.owner_id(conn)), name="Scout", purpose="x", lifetime="long_lived",
                                  permissions=["tasks:read"], data_dir=tmp_path)
        conn.execute("UPDATE actors SET last_seen_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                     (out["agent"]["id"],))
        conn.commit()
        conn.close()
        locker = _locker(settings.db_path)
        try:
            r = client.get("/api/worker/feedback", headers={"Authorization": f"Bearer {out['api_key']}"})
        finally:
            locker.rollback()
            locker.close()
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), (list, dict))

