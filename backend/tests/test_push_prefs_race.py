"""Settings → Oznámení: overlapping prefs writes (prod 2026-10-13 "UNIQUE constraint failed: settings.key"), and the
browser's subscribe steps reported to the server with this device's status."""

import threading

from fastapi.testclient import TestClient

from pos import actors, push, settings_store, versioning
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate
from pos.main import create_app


def _conn(path):
    c = connect(path)
    migrate(c)
    actors.ensure_builtin(c)
    c.commit()
    return c


def test_put_is_an_upsert_and_versioned(tmp_path):
    conn = _conn(tmp_path / "p.db")
    me = Ctx(actors.owner_id(conn))
    settings_store.put(conn, me, "x.y", {"a": 1})
    settings_store.put(conn, me, "x.y", {"a": 2})
    conn.commit()
    assert settings_store.get(conn, "x.y") == {"a": 2}
    sid = conn.execute("SELECT id FROM settings WHERE key = 'x.y'").fetchone()["id"]
    assert [h["action"] for h in versioning.history(conn, "setting", sid)] == ["create", "update"]


def test_a_second_connection_inserting_the_same_new_key_does_not_fail(tmp_path):
    """The race: both requests saw no row; the second one's insert used to raise IntegrityError."""
    a = _conn(tmp_path / "p.db")
    b = connect(tmp_path / "p.db")
    me = Ctx(actors.owner_id(a))
    settings_store.put(a, me, "push.prefs.1", {"chat": False})
    a.commit()
    settings_store.put(b, me, "push.prefs.1", {"chat": True})  # as if b had read "no row" before a committed
    b.commit()
    assert settings_store.get(a, "push.prefs.1") == {"chat": True}


def test_concurrent_prefs_writes_neither_fail_nor_lose_a_switch(tmp_path):
    path = tmp_path / "p.db"
    conn = _conn(path)
    me = Ctx(actors.owner_id(conn))
    keys = ["chat", "needs", "urgent", "preview", "quiet"]
    errors: list[Exception] = []
    start = threading.Barrier(len(keys))

    def flip(k):
        c = connect(path)
        try:
            start.wait()
            push.set_prefs(c, me, {k: False})
            c.commit()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=flip, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    p = push.prefs(conn, me.actor_id)
    assert all(p[k] is False for k in keys), p


def test_client_steps_are_logged_and_failures_feed_health(tmp_path):
    keys = push.generate_vapid()
    s = Settings(data_dir=tmp_path, vapid_private_key=keys[0], vapid_public_key=keys[1], scheduler=False)
    with TestClient(create_app(s)) as client:
        assert client.post("/api/push/client-log", json={"step": "permission", "ok": True, "detail": "granted (already)"}).status_code == 204
        assert client.post("/api/push/client-log", json={"step": "subscribe", "ok": False, "detail": "AbortError: x",
                                                         "standalone": True}).status_code == 204
        cfg = client.get("/api/push/config").json()
    st = cfg["status"]
    assert st["subscription"] is None and st["last_failure"] is None
    conn = connect(tmp_path / "personalos.db")
    rows = conn.execute("SELECT step, ok FROM push_client_log ORDER BY id").fetchall()
    assert [(r["step"], r["ok"]) for r in rows] == [("permission", 1), ("subscribe", 0)]
    assert push.health(conn)["client_errors_24h"] == 1
