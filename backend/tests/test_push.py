"""Web Push (pos.push) and signed-in devices (pos.devices): subscribe, send, what notifies, quiet hours."""

import io
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, approvals, chat, devices, push
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect, migrate
from pos.main import create_app

FCM = "https://fcm.googleapis.com/fcm/send/abc123"
SUB = {"endpoint": FCM, "keys": {"p256dh": "BPk", "auth": "au"}}
DAY = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)  # 12:00 in Prague
NIGHT = datetime(2026, 9, 29, 21, 30, tzinfo=timezone.utc)  # 23:30 in Prague


@pytest.fixture
def keys():
    return push.generate_vapid()


@pytest.fixture
def sent(monkeypatch):
    """The push service, mocked: records what was sent and answers with `status`."""
    box = {"status": 201, "calls": []}

    def transport(sub, data, *, urgency, ttl, settings):
        box["calls"].append({"endpoint": sub["endpoint"], "payload": json.loads(data), "urgency": urgency})
        return box["status"]

    monkeypatch.setattr(push, "TRANSPORT", transport)
    return box


def _settings(tmp_path, keys, **kw):
    return Settings(data_dir=tmp_path, vapid_private_key=keys[0], vapid_public_key=keys[1], scheduler=False, **kw)


def _conn(tmp_path):
    c = connect(tmp_path / "personalos.db")
    migrate(c)
    actors.ensure_builtin(c)
    c.execute("UPDATE actors SET name = 'David' WHERE is_owner = 1")
    c.commit()
    return c


def _world(tmp_path):
    conn = _conn(tmp_path)
    me = Ctx(actors.owner_id(conn))
    ai = Ctx(actors.assistant_id(conn), via="mcp")
    dm = chat.dm_channel(conn, me.actor_id, ai.actor_id)["id"]
    team = chat.ensure_team_channel(conn)
    system = chat.ensure_system_channel(conn)
    chat._add_member(conn, team, me.actor_id)
    chat._add_member(conn, system, me.actor_id)
    push.subscribe(conn, me.actor_id, SUB, device_id=None)
    conn.commit()
    return conn, me, ai, dm, team, system


# ------------------------------------------------------------------ keys and subscriptions

def test_vapid_keys_have_the_web_push_shape(keys):
    import base64

    priv, pub = keys
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    assert len(base64.urlsafe_b64decode(pad(priv))) == 32
    raw = base64.urlsafe_b64decode(pad(pub))
    assert len(raw) == 65 and raw[0] == 4  # an uncompressed P-256 point


def test_subscribe_and_unsubscribe_over_the_api(tmp_path, keys):
    with TestClient(create_app(_settings(tmp_path, keys))) as client:
        cfg = client.get("/api/push/config").json()
        assert cfg["enabled"] and cfg["public_key"] == keys[1] and cfg["devices"] == 0
        assert cfg["prefs"]["quiet_from"] == "22:00" and cfg["prefs"]["quiet_to"] == "07:00"

        assert client.post("/api/push/subscribe", json=SUB).status_code == 201
        assert client.post("/api/push/subscribe", json=SUB).status_code == 201  # the same device again: one row
        assert client.get("/api/push/config").json()["devices"] == 1
        # only browsers' push services: never an arbitrary host (no SSRF through a subscription)
        bad = {**SUB, "endpoint": "https://192.168.1.1/admin"}
        assert client.post("/api/push/subscribe", json=bad).status_code == 422
        assert client.post("/api/push/subscribe", json={**SUB, "endpoint": "http://fcm.googleapis.com/x"}).status_code == 422

        r = client.post("/api/push/unsubscribe", json={"endpoint": FCM}).json()
        assert r["removed"] is True
        assert client.get("/api/push/config").json()["devices"] == 0


def test_subscribe_without_keys_is_503(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, scheduler=False))) as client:
        assert client.get("/api/push/config").json()["enabled"] is False
        assert client.post("/api/push/subscribe", json=SUB).status_code == 503


def test_prefs_are_validated_and_stored(tmp_path, keys):
    with TestClient(create_app(_settings(tmp_path, keys))) as client:
        out = client.put("/api/push/prefs", json={"chat": False, "quiet_from": "23:15"}).json()
        assert out["chat"] is False and out["quiet_from"] == "23:15" and out["needs"] is True
        assert client.get("/api/push/prefs").json()["chat"] is False
        assert client.put("/api/push/prefs", json={"quiet_to": "7"}).status_code == 422
        assert client.put("/api/push/prefs", json={"needs": "yes"}).status_code == 422


def test_send_to_the_test_endpoint_and_gone_subscriptions_are_deleted(tmp_path, keys, sent):
    with TestClient(create_app(_settings(tmp_path, keys))) as client:
        client.post("/api/push/subscribe", json=SUB)
        assert client.post("/api/push/test").json() == {"sent": 1}
        assert sent["calls"][0]["payload"]["title"] == "PersonalOS"
        sent["status"] = 410  # the browser dropped it
        assert client.post("/api/push/test").json() == {"sent": 0}
        assert client.get("/api/push/config").json()["devices"] == 0


def test_a_failing_push_service_is_counted_not_fatal(tmp_path, keys, monkeypatch):
    conn, me, *_ = _world(tmp_path)

    def boom(*a, **kw):
        raise ConnectionError("push service down")

    monkeypatch.setattr(push, "TRANSPORT", boom)
    assert push.send(conn, _settings(tmp_path, keys), me.actor_id, {"title": "x", "tag": "t"}) == 0
    row = push.subscriptions(conn, me.actor_id)[0]
    assert row["failures"] == 1 and "down" in row["last_error"]


# ------------------------------------------------------------------ what notifies

def _tick(conn, tmp_path, keys, now=DAY):
    return push.tick(conn, _settings(tmp_path, keys), now=now)


def test_agent_dm_notifies_once_per_conversation(tmp_path, keys, sent):
    conn, me, ai, dm, team, system = _world(tmp_path)
    _tick(conn, tmp_path, keys)  # first pass: the cursor starts now, nothing old is sent
    chat.send(conn, ai, dm, "Hotovo, návrh je v T-12.", system=True)
    chat.send(conn, ai, dm, "A ještě: token sk-abcdefghijklmnopqrstuvwxyz0123456789 nepatří sem.", system=True)
    out = _tick(conn, tmp_path, keys)
    assert len(out) == 1 and len(sent["calls"]) == 1  # grouped: one notification for the DM
    p = sent["calls"][0]["payload"]
    assert p["tag"] == f"chat-{dm}" and p["url"] == f"/m/chat/{dm}"
    assert p["title"].startswith(actors.get(conn, ai.actor_id)["name"]) and "(2)" in p["title"]
    assert "sk-abc" not in p["body"] and "•••" in p["body"]  # no secrets in the payload
    assert _tick(conn, tmp_path, keys) == []  # nothing new


def test_noise_and_read_messages_do_not_notify(tmp_path, keys, sent):
    conn, me, ai, dm, team, system = _world(tmp_path)
    _tick(conn, tmp_path, keys)
    chat.post_system(conn, ai.actor_id, "Nightly backup finished")  # #system: never
    chat.send(conn, ai, team, "Release is out.", system=True)  # a group message not for me: no
    m = chat.send(conn, ai, dm, "Read already", system=True)
    chat.mark_read(conn, me, dm, m["id"])  # the owner had it open
    conn.commit()
    assert _tick(conn, tmp_path, keys) == []
    chat.send(conn, ai, team, "@David podívej se prosím na rozpočet", system=True)  # a mention: yes
    out = _tick(conn, tmp_path, keys)
    assert [o["payload"]["tag"] for o in out] == [f"chat-{team}"]
    assert "#team" in out[0]["payload"]["title"]


def test_a_reply_in_my_thread_notifies_with_the_thread_address(tmp_path, keys, sent):
    conn, me, ai, dm, team, system = _world(tmp_path)
    root = chat.send(conn, me, team, "Kdo připraví report?", system=True)
    _tick(conn, tmp_path, keys)
    chat.send(conn, ai, team, "Já, do pátku.", reply_to=root["id"], system=True)
    out = _tick(conn, tmp_path, keys)
    assert out[0]["payload"]["tag"] == f"chat-{team}-t{root['id']}"
    assert out[0]["payload"]["url"] == f"/m/chat/{team}?thread={root['id']}"


def test_quiet_hours_hold_chat_but_not_urgent(tmp_path, keys, sent):
    conn, me, ai, dm, team, system = _world(tmp_path)
    _tick(conn, tmp_path, keys, NIGHT)
    chat.send(conn, ai, dm, "Dobrou noc", system=True)
    assert _tick(conn, tmp_path, keys, NIGHT) == []
    chat.send(conn, ai, dm, "Produkce neodpovídá, potřebuju rozhodnutí.", priority="change_plan", system=True)
    out = _tick(conn, tmp_path, keys, NIGHT)
    assert len(out) == 1 and out[0]["payload"]["kind"] == "urgent"
    assert out[0]["payload"]["title"].startswith("Naléhavé")
    assert sent["calls"][-1]["urgency"] == "high"


def test_quiet_hours_window_crosses_midnight():
    p = dict(push.DEFAULT_PREFS)
    assert push.in_quiet(p, NIGHT) and not push.in_quiet(p, DAY)
    assert push.in_quiet(p, datetime(2026, 9, 29, 4, 59, tzinfo=timezone.utc))  # 06:59 Prague
    assert not push.in_quiet(p, datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc))  # 07:00 Prague
    assert not push.in_quiet({**p, "quiet": False}, NIGHT)
    assert push.in_quiet({**p, "quiet_from": "12:00", "quiet_to": "13:00"}, DAY)


def test_categories_can_be_turned_off(tmp_path, keys, sent):
    conn, me, ai, dm, team, system = _world(tmp_path)
    push.set_prefs(conn, me, {"chat": False})
    conn.commit()
    _tick(conn, tmp_path, keys)
    chat.send(conn, ai, dm, "Ahoj", system=True)
    assert _tick(conn, tmp_path, keys) == []


def test_new_needs_items_notify_and_old_ones_do_not(tmp_path, keys, sent):
    conn, me, ai, dm, team, system = _world(tmp_path)
    # already waiting when the device subscribed: not news
    old =approvals.request(conn, ai, "email_send", {"why": "Old one"})
    conn.commit()
    conn.execute("DELETE FROM push_state WHERE key LIKE 'needs_seeded:%'")
    push.subscribe(conn, me.actor_id, SUB, device_id=None)  # seeds what waits now
    conn.commit()
    _tick(conn, tmp_path, keys)
    assert sent["calls"] == []
    new = approvals.request(conn, ai, "email_send", {"why": "Send the offer to ACME"})
    conn.commit()
    out = _tick(conn, tmp_path, keys)
    assert [o["payload"]["tag"] for o in out] == [f"needs-approval:{new['id']}"]
    assert out[0]["payload"]["url"] == f"/m/needs?item=approval:{new['id']}"
    assert out[0]["payload"]["title"].startswith("Ke schválení")
    assert old["id"] != new["id"]
    assert _tick(conn, tmp_path, keys) == []  # once


def test_needs_during_quiet_hours_arrive_in_the_morning_as_one_summary(tmp_path, keys, sent):
    conn, me, ai, dm, team, system = _world(tmp_path)
    _tick(conn, tmp_path, keys, NIGHT)
    for i in range(5):
        approvals.request(conn, ai, "email_send", {"why": f"Offer {i}"})
    conn.commit()
    assert _tick(conn, tmp_path, keys, NIGHT) == []
    out = _tick(conn, tmp_path, keys, DAY)
    assert len(out) == 1 and out[0]["payload"]["tag"] == "needs-summary" and "5" in out[0]["payload"]["title"]
    assert _tick(conn, tmp_path, keys, DAY) == []


def test_preview_strips_code_and_keys():
    text = push.preview("**Hotovo**\n```\npassword=hunter2\n```\nklíč ghp_abcdefghijklmnop1234 a " + "x" * 300)
    assert "hunter2" not in text and "ghp_" not in text and "[kód]" in text
    assert len(text) <= push.PREVIEW_CHARS


# ------------------------------------------------------------------ devices

def test_login_creates_a_device_that_can_be_revoked(tmp_path, keys, sent):
    settings = _settings(tmp_path, keys, password="pw", session_secret="s")
    with TestClient(create_app(settings)) as phone, TestClient(create_app(settings)) as pc:
        assert phone.post("/api/auth/login", json={"password": "pw", "app": True},
                          headers={"user-agent": "Mozilla/5.0 (Linux; Android 15) Chrome/140"}).status_code == 200
        assert pc.post("/api/auth/login", json={"password": "pw"},
                       headers={"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/140 Edg/140"}).status_code == 200
        listed = pc.get("/api/auth/devices").json()
        assert {d["label"] for d in listed} == {"Android · Chrome", "Windows · Edge"}
        phone_dev = next(d for d in listed if d["label"].startswith("Android"))
        assert phone_dev["app"] is True and not phone_dev["current"]
        assert next(d for d in listed if d["current"])["label"] == "Windows · Edge"

        assert phone.post("/api/push/subscribe", json=SUB).status_code == 201
        # the PC signs the phone out: its session ends and its push subscription goes
        assert pc.post(f"/api/auth/devices/{phone_dev['id']}/revoke").json() == {"ok": True}
        devices._cache.clear()
        assert phone.get("/api/system").status_code == 401
        assert pc.get("/api/push/config").json()["devices"] == 0
        assert len(pc.get("/api/auth/devices").json()) == 1


def test_logout_ends_the_device(tmp_path, keys):
    settings = _settings(tmp_path, keys, password="pw", session_secret="s")
    with TestClient(create_app(settings)) as c:
        c.post("/api/auth/login", json={"password": "pw"})
        cookie = c.cookies.get("pos_session")
        c.post("/api/auth/logout")
        devices._cache.clear()
        c.cookies.set("pos_session", cookie)  # a copied old cookie no longer works
        assert c.get("/api/system").status_code == 401


def _session(cookie: str) -> dict:
    import base64

    return json.loads(base64.b64decode(cookie.split(".")[0] + "=="))


def test_a_session_from_before_devices_becomes_one(tmp_path, keys):
    settings = _settings(tmp_path, keys, password="pw", session_secret="s")
    with TestClient(create_app(settings)) as c:
        c.post("/api/auth/login", json={"password": "pw"})
        conn = connect(settings.db_path)
        conn.execute("DELETE FROM auth_devices")  # as if the login happened before this table
        conn.commit()
        conn.close()
        devices._cache.clear()
        assert c.get("/api/system").status_code == 401  # its sid is unknown now: signed out
        # an old cookie without a sid at all is registered as a device on its next request
        import base64

        from itsdangerous import TimestampSigner

        raw = base64.b64encode(json.dumps({"user": "owner"}).encode())
        c.cookies.clear()  # only this cookie (the jar may keep the cleared session's next to it)
        c.cookies.set("pos_session", TimestampSigner("s").sign(raw).decode())
        r = c.get("/api/system")
        assert r.status_code == 200 and "sid" in _session(r.cookies["pos_session"])
        c.cookies.clear()
        c.cookies.set("pos_session", r.cookies["pos_session"])  # the browser keeps the new one
        assert len(c.get("/api/auth/devices").json()) == 1


def test_a_cookie_without_a_device_id_makes_one_device_however_often_it_is_sent(tmp_path, keys):
    """5. 10. 19:45: a script kept sending one old cookie; every request made a new device (~245)."""
    import base64

    from itsdangerous import TimestampSigner

    settings = _settings(tmp_path, keys, password="pw", session_secret="s")
    with TestClient(create_app(settings)) as c:
        old = TimestampSigner("s").sign(base64.b64encode(json.dumps({"user": "owner"}).encode())).decode()
        for _ in range(5):
            devices._cache.clear()
            c.cookies.clear()
            c.cookies.set("pos_session", old)  # never keeps the updated cookie
            assert c.get("/api/system").status_code == 200
        conn = connect(settings.db_path)
        assert conn.execute("SELECT COUNT(*) FROM auth_devices").fetchone()[0] == 1
        conn.close()


def test_sign_out_all_other_devices(tmp_path, keys):
    settings = _settings(tmp_path, keys, password="pw", session_secret="s")
    with TestClient(create_app(settings)) as phone, TestClient(create_app(settings)) as pc:
        phone.post("/api/auth/login", json={"password": "pw"})
        pc.post("/api/auth/login", json={"password": "pw"})
        assert pc.post("/api/auth/devices/revoke-others").json() == {"ok": True, "revoked": 1}
        devices._cache.clear()
        assert phone.get("/api/system").status_code == 401
        assert pc.get("/api/system").status_code == 200
        assert [d["current"] for d in pc.get("/api/auth/devices").json()] == [True]


def test_migration_drops_the_audit_flood_devices(tmp_path):
    from pos.db import MIGRATIONS

    conn = connect(tmp_path / "f.db")
    devices.ensure_schema(conn)
    push.ensure_schema(conn)
    rows = [("flood1", "2026-10-05T19:46:00+00:00", "2026-10-05T19:46:00+00:00"),
            ("flood-used", "2026-10-05T19:47:00+00:00", "2026-10-05T20:30:00+00:00"),  # came back: kept
            ("flood-push", "2026-10-05T19:48:00+00:00", "2026-10-05T19:48:00+00:00"),  # has push: kept
            ("before", "2026-10-05T19:40:00+00:00", "2026-10-05T19:40:00+00:00")]
    for sid, created, seen in rows:
        conn.execute("""INSERT INTO auth_devices (id, actor_id, label, created_at, last_seen_at)
                        VALUES (?, 1, 'Neznámé zařízení', ?, ?)""", (sid, created, seen))
    conn.execute("""INSERT INTO push_subscriptions (actor_id, device_id, endpoint, p256dh, auth, created_at)
                    VALUES (1, 'flood-push', 'https://push/x', 'k', 'a', '2026-10-05T19:48:00+00:00')""")
    sql = next(m for m in MIGRATIONS if "2026-10-05T19:45:00" in m)
    conn.executescript(sql)
    left = {r[0] for r in conn.execute("SELECT id FROM auth_devices")}
    assert left == {"flood-used", "flood-push", "before"}
    conn.close()


# ------------------------------------------------------------------ chat attachments

def test_a_photo_from_the_phone_is_attached_to_the_message(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, scheduler=False))) as c:
        f = c.post("/api/files", files={"file": ("foto.png", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"0" * 64), "image/png")})
        assert f.status_code == 201
        fid = f.json()["id"]
        ch = c.get("/api/chat/channels").json()[0]["id"]
        m = c.post(f"/api/chat/channels/{ch}/messages", json={"body": "", "attachments": [{"type": "file", "id": fid}]})
        assert m.status_code == 201, m.text
        msg = m.json()
        assert msg["attachments"][0]["id"] == fid and msg["attachments"][0]["name"] == "foto.png"
        assert f"soubor #{fid}" in msg["body"]  # agents read the text
        missing = c.post(f"/api/chat/channels/{ch}/messages", json={"body": "x", "attachments": [{"id": 999}]})
        assert missing.status_code == 404


def test_the_real_sender_encrypts_for_the_browser_and_signs_with_vapid(tmp_path, keys, monkeypatch):
    """pywebpush end to end, without the network: the browser's side decrypts the payload."""
    import base64

    import http_ece
    import requests
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    browser = ec.generate_private_key(ec.SECP256R1())  # the browser's subscription key pair
    auth = b"0123456789abcdef"
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    pub = browser.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    sub = {"endpoint": FCM, "p256dh": b64(pub), "auth": b64(auth)}
    seen = {}

    class Resp:
        status_code = 201
        text = ""
        headers = {}

    def post(url, data=None, headers=None, timeout=None, **kw):
        seen.update(url=url, data=data, headers=headers)
        return Resp()

    monkeypatch.setattr(requests, "post", post)
    payload = json.dumps({"title": "CEO", "body": "Ahoj", "tag": "chat-3", "url": "/m/chat/3"}, ensure_ascii=False)
    status = push._webpush(sub, payload, urgency="high", ttl=60, settings=_settings(tmp_path, keys))
    assert status == 201 and seen["url"] == FCM
    assert seen["headers"]["Urgency"] == "high" and seen["headers"]["content-encoding"] == "aes128gcm"
    assert seen["headers"]["authorization"].startswith("vapid t=") and f"k={keys[1]}" in seen["headers"]["authorization"]
    plain = http_ece.decrypt(seen["data"], private_key=browser, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == json.loads(payload)
