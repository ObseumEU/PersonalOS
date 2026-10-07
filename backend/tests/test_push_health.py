"""Web Push health (pos.push): per-device results, the failure log, the Settings test, browser-side failures,
the sentinel's numbers and the improve signals. prod 2026-09-29..10-06: no notification ever reached the
owner's phone, its subscription never arrived and nothing noticed."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, devices, monitor, push
from pos.config import Settings
from pos.core import Ctx
from pos.db import MIGRATIONS, connect, migrate
from pos.improve import signals
from pos.main import create_app

FCM = "https://fcm.googleapis.com/fcm/send/"
APPLE = "https://web.push.apple.com/"


@pytest.fixture
def keys():
    return push.generate_vapid()


def _settings(tmp_path, keys):
    return Settings(data_dir=tmp_path, vapid_private_key=keys[0], vapid_public_key=keys[1], scheduler=False)


@pytest.fixture
def service(monkeypatch):
    """The push services, mocked: the status per endpoint host (default 201)."""
    box = {"status": {}, "calls": []}

    def transport(sub, data, *, urgency, ttl, settings):
        box["calls"].append({"endpoint": sub["endpoint"], "payload": json.loads(data)})
        st = box["status"].get(push._host(sub["endpoint"]), 201)
        if isinstance(st, Exception):
            raise st
        return st

    monkeypatch.setattr(push, "TRANSPORT", transport)
    return box


def _owner_with_two_devices(tmp_path):
    conn = connect(tmp_path / "personalos.db")
    migrate(conn)
    actors.ensure_builtin(conn)
    me = actors.owner_id(conn)
    phone = devices.create(conn, me, owner_login=True, app=True,
                           user_agent="Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 Chrome/154.0 Mobile")
    iphone = devices.create(conn, me, owner_login=True, app=True,
                            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1")
    push.subscribe(conn, me, {"endpoint": FCM + "phone", "keys": {"p256dh": "k", "auth": "a"}}, device_id=phone)
    push.subscribe(conn, me, {"endpoint": APPLE + "iphone", "keys": {"p256dh": "k", "auth": "a"}}, device_id=iphone)
    conn.commit()
    return conn, me, phone, iphone


def test_send_test_reports_each_device_and_logs_the_failures(tmp_path, keys, service):
    conn, me, phone, iphone = _owner_with_two_devices(tmp_path)
    service["status"]["web.push.apple.com"] = 403  # e.g. a VAPID subject Apple refuses
    out = push.send_test(conn, _settings(tmp_path, keys), me)
    assert out["sent"] == 1 and out["devices"] == 2
    by = {r["device_id"]: r for r in out["results"]}
    assert by[phone]["ok"] and by[phone]["label"] == "Android · Chrome · aplikace"
    assert not by[iphone]["ok"] and by[iphone]["status"] == 403 and "VAPID" in by[iphone]["error"]
    assert service["calls"][0]["payload"]["title"] == push.TEST_TITLE == "Test notifikací – PersonalOS"
    assert all(c["payload"]["kind"] == "test" for c in service["calls"])
    fail = conn.execute("SELECT * FROM push_failures").fetchall()
    assert len(fail) == 1 and fail[0]["status"] == 403 and fail[0]["host"] == "web.push.apple.com"
    h = push.health(conn)
    assert h["failed_24h"] == 1 and h["ok_devices_24h"] == 1 and h["owner_devices"] == 2


def test_gone_and_unreachable_subscriptions(tmp_path, keys, service):
    conn, me, phone, iphone = _owner_with_two_devices(tmp_path)
    service["status"]["fcm.googleapis.com"] = 410
    service["status"]["web.push.apple.com"] = ConnectionError("no route")
    res = push.send_each(conn, _settings(tmp_path, keys), me, {"title": "x", "tag": "t", "kind": "chat"})
    by = {r["device_id"]: r for r in res}
    assert by[phone]["removed"] and not by[phone]["ok"]
    assert by[iphone]["status"] is None and "ConnectionError" in by[iphone]["error"]
    assert [s["device_id"] for s in push.subscriptions(conn, me)] == [iphone]  # 410 pruned, the other kept
    kinds = {r["status"] for r in conn.execute("SELECT status FROM push_failures")}
    assert kinds == {410, None}


def test_the_settings_button_over_the_api(tmp_path, keys, service):
    with TestClient(create_app(_settings(tmp_path, keys))) as client:
        assert client.post("/api/push/test", json={"all": True}).json() == {"sent": 0, "devices": 0, "results": []}
        client.post("/api/push/subscribe", json={"endpoint": FCM + "x", "keys": {"p256dh": "k", "auth": "a"}})
        assert client.get("/api/push/config").json()["devices"] == 1  # (this_device needs a signed-in device)
        out = client.post("/api/push/test", json={"all": True}).json()
        assert out["sent"] == 1 and out["results"][0]["ok"] and out["results"][0]["label"]
        assert client.post("/api/push/test").json() == {"sent": 1}  # this device only, as before


def test_a_browser_failure_is_recorded_and_is_a_signal(tmp_path, keys):
    with TestClient(create_app(_settings(tmp_path, keys))) as client:
        r = client.post("/api/push/client-error", json={"step": "subscribe", "standalone": True,
                                                         "error": "AbortError: Registration failed - push service error"},
                        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 10; K) Chrome/154.0 Mobile"})
        assert r.status_code == 204
        assert client.post("/api/push/client-error", json={"step": "x" * 50, "error": "e"}).status_code == 422
    conn = connect(tmp_path / "personalos.db")
    devices.create(conn, actors.owner_id(conn), owner_login=True, app=True, user_agent="Android Chrome/154.0")
    row = conn.execute("SELECT * FROM push_failures").fetchone()
    assert row["source"] == "client" and row["host"] == "Android · Chrome" and "[aplikace]" in row["error"]
    h = push.health(conn)
    assert h["client_errors_24h"] == 1 and h["owner_devices"] == 0
    assert monitor.run_stats(conn)["push"]["client_errors_24h"] == 1  # the sentinel's numbers
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    items = signals.events(conn, (now - timedelta(days=1)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds"))
    assert "push:client.subscribe" in items and items["push:client.subscribe"]["category"] == "push"
    snap = signals.snapshot(conn, now)
    assert snap["push:owner_no_device"]["count"] == 1  # nothing can reach his phone


def test_a_send_failure_is_an_improve_signal(tmp_path, keys, service):
    conn, me, phone, iphone = _owner_with_two_devices(tmp_path)
    service["status"]["fcm.googleapis.com"] = 403
    push.send(conn, _settings(tmp_path, keys), me, {"title": "x", "tag": "t", "kind": "needs"})
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    items = signals.events(conn, (now - timedelta(days=1)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds"))
    assert items["push:send.403"]["count"] == 1
    assert "push:owner_no_device" not in signals.snapshot(conn, now)


def test_a_new_subscription_of_the_same_device_replaces_its_old_endpoint(tmp_path):
    conn, me, phone, iphone = _owner_with_two_devices(tmp_path)
    push.subscribe(conn, me, {"endpoint": FCM + "phone-renewed", "keys": {"p256dh": "k", "auth": "a"}}, device_id=phone)
    eps = sorted(s["endpoint"] for s in push.subscriptions(conn, me))
    assert eps == [FCM + "phone-renewed", APPLE + "iphone"]


def test_migration_drops_the_flood_devices_of_the_minute_before(tmp_path):
    conn = connect(tmp_path / "f.db")
    devices.ensure_schema(conn)
    push.ensure_schema(conn)
    rows = [("f1", "2026-10-05T19:44:40+00:00", "2026-10-05T19:44:40+00:00", "Neznámé zařízení"),
            ("f-used", "2026-10-05T19:44:41+00:00", "2026-10-05T20:00:00+00:00", "Neznámé zařízení"),
            ("phone", "2026-10-05T19:44:45+00:00", "2026-10-05T19:44:45+00:00", "Android · Chrome"),
            ("before", "2026-10-05T19:43:59+00:00", "2026-10-05T19:43:59+00:00", "Neznámé zařízení")]
    for sid, created, seen, label in rows:
        conn.execute("""INSERT INTO auth_devices (id, actor_id, label, created_at, last_seen_at)
                        VALUES (?, 1, ?, ?, ?)""", (sid, label, created, seen))
    sql = next(m for m in MIGRATIONS if "2026-10-05T19:44:00" in m)
    conn.executescript(sql)
    assert {r[0] for r in conn.execute("SELECT id FROM auth_devices")} == {"f-used", "phone", "before"}


def test_no_vapid_no_test(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, scheduler=False))) as client:
        assert client.post("/api/push/test", json={"all": True}).status_code == 503
    assert push.send_each(connect(tmp_path / "personalos.db"), Settings(data_dir=tmp_path), 1, {"title": "x"}) == []
    assert Ctx(1).actor_id == 1
