"""Hardening after the 2026-10-13 outage ("database is locked", /api/auth/me 500 after 10 s, /api/health 200 all
along): the deep health (pos.health), connections that always roll back (pos.db.Conn), the long-transaction
warning, atomic upserts, and the sentinel's platform alert endpoint (pos.platform_alert)."""

import logging
import sqlite3
import threading
import time

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from pos import actors, agents, chat, db, health, owner_fallback
from pos.api_tasks import get_db
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app

TOKEN = "s" * 40


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_SENTINEL_TOKEN", TOKEN)
    health.reset()
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    client.__enter__()
    yield {"client": client, "db": settings.db_path, "settings": settings}
    client.__exit__(None, None, None)
    health.reset()


def _hold_write_lock(path):
    holder = connect(path)
    holder.execute("BEGIN IMMEDIATE")
    return holder


# ------------------------------------------------------------------ connections

def test_every_connection_has_wal_a_5s_busy_timeout_and_normal_sync(tmp_path):
    conn = connect(tmp_path / "x.db")
    assert isinstance(conn, db.Conn)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db.BUSY_TIMEOUT_MS == 5000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    conn.close()


def test_closing_a_connection_mid_write_rolls_back_and_frees_the_lock(tmp_path):
    path = tmp_path / "x.db"
    a = connect(path)
    a.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
    a.commit()
    a.execute("INSERT INTO t VALUES ('half')")  # a write transaction is open
    a.close()                                    # no commit: an exception path
    b = connect(path, busy_timeout_ms=0)         # would fail at once if anything still held the lock
    b.execute("INSERT INTO t VALUES ('next')")
    b.commit()
    assert [r[0] for r in b.execute("SELECT k FROM t")] == ["next"]
    b.close()


def test_a_session_rolls_back_on_an_exception(tmp_path):
    path = tmp_path / "x.db"
    with db.session(path) as c:
        c.execute("CREATE TABLE t (k TEXT PRIMARY KEY)")
        c.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with db.session(path) as c:
            c.execute("INSERT INTO t VALUES ('a')")
            c.execute("INSERT INTO t VALUES ('a')")  # the 2026-10-13 IntegrityError, mid-transaction
    with db.session(path, busy_timeout_ms=0) as c:
        c.execute("INSERT INTO t VALUES ('b')")
        c.commit()
        assert [r[0] for r in c.execute("SELECT k FROM t")] == ["b"]


def test_a_request_that_fails_mid_write_never_keeps_the_lock(tmp_path):
    """The rollback guarantee for request-scoped connections (pos.api_tasks.get_db): a handler that wrote and then
    raised, or forgot to commit, leaves no transaction behind."""
    settings = Settings(data_dir=tmp_path, scheduler=False)
    db.init_db(settings.db_path)
    api = FastAPI()
    api.dependency_overrides[__import__("pos.config", fromlist=["get_settings"]).get_settings] = lambda: settings
    seen = {}

    @api.post("/boom")
    def boom(conn=Depends(get_db)):
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('boom', '1')")
        seen["conn"] = conn
        raise sqlite3.IntegrityError("UNIQUE constraint failed: settings.key")

    @api.post("/forgot")
    def forgot(conn=Depends(get_db)):
        conn.execute("INSERT INTO app_meta (key, value) VALUES ('forgot', '1')")
        return {"ok": True}

    with TestClient(api, raise_server_exceptions=False) as client:
        assert client.post("/boom").status_code == 500
        assert client.post("/forgot").status_code == 200
    other = connect(settings.db_path, busy_timeout_ms=0)
    other.execute("BEGIN IMMEDIATE")  # free at once
    other.rollback()
    assert other.execute("SELECT COUNT(*) FROM app_meta WHERE key IN ('boom', 'forgot')").fetchone()[0] == 0
    other.close()
    with pytest.raises(sqlite3.ProgrammingError):
        seen["conn"].execute("SELECT 1")  # closed


def test_a_lock_error_is_counted(tmp_path):
    path = tmp_path / "x.db"
    holder = _hold_write_lock(path)
    before = db.totals()["locked"]
    c = connect(path, busy_timeout_ms=50)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        c.execute("BEGIN IMMEDIATE")
    assert db.totals()["locked"] == before + 1 and db.counts(60)["locked"] >= 1
    c.close()
    holder.close()


def test_a_long_transaction_is_logged_with_the_code_that_opened_it(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(db, "LONG_TX_S", 0.2)
    caplog.set_level(logging.WARNING, logger="pos.db")
    path = tmp_path / "x.db"
    c = connect(path)
    c.execute("CREATE TABLE t (x)")
    c.commit()

    def open_a_write_and_wait():
        c.execute("INSERT INTO t VALUES (1)")

    open_a_write_and_wait()
    time.sleep(0.3)
    held = db.watch_once()  # the watchdog's pass while it is still held
    assert held and held[0]["age_s"] >= 0.2
    assert db.held_transactions()
    assert "long transaction: a write has been open" in caplog.text
    assert "open_a_write_and_wait" in caplog.text  # the stack that opened it
    db.watch_once()
    assert caplog.text.count("has been open") == 1  # once per transaction
    c.commit()
    assert "long transaction released after" in caplog.text
    assert not db.held_transactions()
    c.close()


def test_a_short_transaction_is_not_logged(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="pos.db")
    c = connect(tmp_path / "x.db")
    c.execute("CREATE TABLE t (x)")
    c.execute("INSERT INTO t VALUES (1)")
    c.commit()
    c.close()
    assert "long transaction" not in caplog.text


# ------------------------------------------------------------------ the deep health

def test_deep_health_does_a_real_round_trip(app):
    r = app["client"].get("/api/health/deep")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"]["ok"] is True
    assert body["db"]["write_ms"] is not None
    assert set(body["last_minute"]) >= {"requests", "errors_5xx", "rate_5xx", "p95_ms", "db_locked", "long_tx"}


def test_deep_health_is_degraded_while_the_write_lock_is_held(app, monkeypatch):
    """The 2026-10-13 incident in small: a transaction holds the writer lock. /api/health still says ok;
    /api/health/deep says degraded (503) within its short timeout, and ok again once the lock is released."""
    monkeypatch.setattr(health, "PROBE_BUSY_MS", 300)
    monkeypatch.setattr(health, "CACHE_S", 0)
    real_connect, timeouts = db.connect, []

    def spy(path, *a, **k):
        timeouts.append(k.get("busy_timeout_ms"))
        return real_connect(path, *a, **k)

    holder = _hold_write_lock(app["db"])
    monkeypatch.setattr(health.db, "connect", spy)
    try:
        r = app["client"].get("/api/health/deep")
        # its own short timeout, not the 5 s default (a wall-clock bound flaked on the 1-CPU deployer, T-1650)
        assert 300 in timeouts
        assert r.status_code == 503
        assert r.json()["status"] == "degraded" and "locked" in r.json()["reason"]
        assert app["client"].get("/api/health").status_code == 200  # liveness alone would have said ok
    finally:
        holder.close()
    r = app["client"].get("/api/health/deep")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_deep_health_reports_5xx_rate_and_latency_of_the_last_minute(app, monkeypatch):
    from pos import metrics

    monkeypatch.setattr(metrics, "_recent", __import__("collections").deque(maxlen=1000))
    monkeypatch.setattr(health, "CACHE_S", 0)

    def broken(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(actors, "get", broken)  # /api/auth/me fails like on 2026-10-13
    for _ in range(6):
        assert app["client"].get("/api/auth/me").status_code == 500
    app["client"].get("/api/tasks")
    body = app["client"].get("/api/health/deep").json()
    lm = body["last_minute"]
    assert lm["errors_5xx"] == 6 and lm["requests"] == 7 and lm["rate_5xx"] == pytest.approx(6 / 7, abs=0.01)
    assert lm["p95_ms"] is not None
    assert any("5xx" in w for w in body["warnings"])
    assert body["status"] == "ok"  # the database itself answers: the sentinel judges the numbers


def test_deep_health_shows_where_a_held_transaction_began_only_to_internal_callers(app, monkeypatch):
    monkeypatch.setattr(db, "LONG_TX_S", 0.1)
    monkeypatch.setattr(health, "CACHE_S", 0)
    monkeypatch.setattr(health, "PROBE_BUSY_MS", 100)
    holder = _hold_write_lock(app["db"])
    try:
        time.sleep(0.2)
        inside = app["client"].get("/api/health/deep").json()
        outside = app["client"].get("/api/health/deep", headers={"X-Forwarded-For": "1.2.3.4"}).json()
    finally:
        holder.close()
    assert isinstance(inside["held_transactions"], list) and inside["held_transactions"][0]["age_s"] >= 0.1
    assert isinstance(outside["held_transactions"], int) and outside["held_transactions"] >= 1


def test_deep_health_probes_at_most_once_per_cache_window(app, monkeypatch):
    calls = []
    real = health.probe
    monkeypatch.setattr(health, "probe", lambda p, *a, **k: calls.append(1) or real(p, *a, **k))
    for _ in range(5):
        assert app["client"].get("/api/health/deep").status_code == 200
    assert len(calls) == 1


# ------------------------------------------------------------------ atomic upserts

def test_a_parallel_first_dm_returns_the_channel_instead_of_a_unique_error(app, monkeypatch):
    conn = connect(app["db"])
    owner = actors.owner_id(conn)
    other = actors.assistant_id(conn)
    first = chat.dm_channel(conn, owner, other)
    conn.commit()
    # the race: our read saw nothing, a parallel request created it before our insert
    monkeypatch.setattr(chat, "find_dm", lambda c, a, b, _real=chat.find_dm, _n=[0]: (
        None if not _n.__setitem__(0, _n[0] + 1) and _n[0] == 1 else _real(c, a, b)))
    again = chat.dm_channel(conn, owner, other)
    assert again["id"] == first["id"]
    conn.close()


def test_reactions_toggle_atomically(app):
    conn = connect(app["db"])
    me = Ctx(actors.owner_id(conn))
    msg = chat.send(conn, me, chat.ensure_team_channel(conn), "ahoj")
    conn.commit()
    mid = msg["id"]
    chat.react(conn, me, mid, "👍")
    on = conn.execute("SELECT archived_at FROM chat_reactions WHERE message_id = ?", (mid,)).fetchone()
    assert on["archived_at"] is None
    chat.react(conn, me, mid, "👍")
    assert conn.execute("SELECT archived_at FROM chat_reactions WHERE message_id = ?", (mid,)).fetchone()[0]
    chat.react(conn, me, mid, "👍")
    assert conn.execute("SELECT archived_at FROM chat_reactions WHERE message_id = ?", (mid,)).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM chat_reactions WHERE message_id = ?", (mid,)).fetchone()[0] == 1
    conn.close()


def test_the_system_member_is_created_once_even_when_two_ask_at_once(app, monkeypatch):
    conn = connect(app["db"])
    a = actors.system_id(conn)
    conn.commit()
    # the race: the read saw nothing, a parallel call created it before the insert
    real = conn.execute

    class Racing:
        def __init__(self, c):
            self.c, self.first = c, True

        def execute(self, sql, *args):
            if self.first and sql.startswith("SELECT id FROM actors WHERE name"):
                self.first = False
                return real("SELECT id FROM actors WHERE 0")
            return real(sql, *args)

    assert actors.system_id(Racing(conn)) == a
    assert conn.execute("SELECT COUNT(*) FROM actors WHERE name = ?", (actors.SYSTEM_NAME,)).fetchone()[0] == 1
    conn.close()


# ------------------------------------------------------------------ the sentinel's platform alert

def _alert(client, level="alert", ref="sentinel:abc-1", **extra):
    return client.post("/api/sentinel/alert", headers={"Authorization": f"Bearer {TOKEN}"},
                       json={"ref": ref, "level": level, "title": "deep health selhal 2x po sobě",
                             "body": "- deep health: FAIL", **extra})


@pytest.fixture
def alerting(app, monkeypatch, tmp_path):
    mails = []
    monkeypatch.setattr(owner_fallback, "SENDER", lambda subject, text: mails.append((subject, text)) or "m1")
    conn = connect(app["db"])
    owner = Ctx(actors.owner_id(conn))
    sre = agents.create_agent(conn, owner, name="SRE", purpose="platform", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)["agent"]["id"]
    conn.close()
    return {**app, "mails": mails, "sre": sre}


def test_a_platform_alert_reaches_the_owner_by_email_and_the_sre_by_a_task(alerting):
    r = _alert(alerting["client"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["owner"] == "email" and out["recorded"] is True
    assert len(alerting["mails"]) == 1 and alerting["mails"][0][0].startswith("PersonalOS nefunguje")
    conn = connect(alerting["db"])
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (out["task_id"],)).fetchone()
    assert t["assignee_id"] == alerting["sre"] and t["title"].startswith("Výpadek platformy")
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'sentinel_alert:alert'").fetchone()
    conn.close()


def test_one_alert_per_incident_level_and_a_recovery_notice(alerting):
    c = alerting["client"]
    first = _alert(c).json()
    assert _alert(c).json().get("duplicate") is True  # the sentinel's retry: nothing twice
    healed = _alert(c, "healed", logs="11:20 sqlite3.OperationalError: database is locked").json()
    assert healed["owner"] == "not for the owner" and healed["task_id"] == first["task_id"]  # a comment, same task
    rec = _alert(c, "recovered").json()
    assert rec["owner"] == "email" and rec["task_id"] == first["task_id"]
    assert [m[0].split(":")[0] for m in alerting["mails"]] == ["PersonalOS nefunguje", "PersonalOS znovu funguje"]


def test_the_owner_is_told_even_while_the_database_is_locked(alerting, monkeypatch):
    from pos import platform_alert

    monkeypatch.setattr(platform_alert, "BUSY_MS", 100)
    holder = _hold_write_lock(alerting["db"])
    try:
        r = _alert(alerting["client"])
    finally:
        holder.close()
    assert r.status_code == 503  # not recorded: the sentinel retries ...
    assert r.json()["owner"] == "email" and r.json()["owner_done"] is True  # ... but he already knows
    assert len(alerting["mails"]) == 1
    again = _alert(alerting["client"], owner_done=True)  # the retry says so
    assert again.status_code == 200 and again.json()["recorded"] is True
    assert len(alerting["mails"]) == 1


def test_only_the_sentinel_may_raise_a_platform_alert(alerting):
    r = alerting["client"].post("/api/sentinel/alert", headers={"Authorization": "Bearer " + "x" * 40},
                                json={"ref": "r", "level": "alert"})
    assert r.status_code == 401
    assert _alert(alerting["client"], level="nonsense").status_code == 422


def test_concurrent_writers_do_not_starve_the_deep_health(app, monkeypatch):
    """Short writes from several threads (the handoff polls at ~4 Hz): the probe still gets the lock in time."""
    monkeypatch.setattr(health, "CACHE_S", 0)
    stop = threading.Event()

    def writer():
        c = connect(app["db"])
        while not stop.is_set():
            c.execute("INSERT OR REPLACE INTO app_meta (key, value) VALUES ('poll', ?)", (str(time.time()),))
            c.commit()
            time.sleep(0.01)
        c.close()

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        codes = [app["client"].get("/api/health/deep").status_code for _ in range(5)]
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert codes == [200] * 5
