""""Schválit" / "Zamítnout" on an approval notification (pos.push action tokens, POST /api/push/action):
per device, per approval, expiring, single use, and only from the device's own signed-in session."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, approvals, devices, push
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app

PHONE = "https://fcm.googleapis.com/fcm/send/phone"
PC = "https://updates.push.services.mozilla.com/wpush/v2/pc"
DAY = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


def _sub(endpoint):
    return {"endpoint": endpoint, "keys": {"p256dh": "BPk", "auth": "au"}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    keys = push.generate_vapid()
    settings = Settings(data_dir=tmp_path, vapid_private_key=keys[0], vapid_public_key=keys[1], scheduler=False,
                        password="pw", session_secret="s")
    calls = []
    monkeypatch.setattr(push, "TRANSPORT",
                        lambda sub, data, **kw: calls.append({"endpoint": sub["endpoint"], **json.loads(data)}) or 201)
    devices._cache.clear()
    with TestClient(create_app(settings)) as phone, TestClient(create_app(settings)) as pc, \
            TestClient(create_app(settings)) as stranger:
        for c, ep in ((phone, PHONE), (pc, PC)):
            assert c.post("/api/auth/login", json={"password": "pw"}).status_code == 200
            assert c.post("/api/push/subscribe", json=_sub(ep)).status_code == 201
        conn = connect(settings.db_path)
        ai = Ctx(actors.assistant_id(conn), via="mcp")
        push.tick(conn, settings, now=DAY)  # the cursor starts now
        yield {"phone": phone, "pc": pc, "stranger": stranger, "conn": conn, "ai": ai, "settings": settings,
               "calls": calls}
        conn.close()


def _approval(w, why="Send the offer"):
    a = approvals.request(w["conn"], w["ai"], "email_send", {"why": why})
    w["conn"].commit()
    return a["id"]


def _sub_row(conn, endpoint):
    return conn.execute("SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()


def _act(client, approval_id, token, endpoint, decision="approve"):
    return client.post("/api/push/action", json={"approval_id": approval_id, "token": token, "endpoint": endpoint,
                                                 "decision": decision})


def test_each_device_gets_its_own_buttons_and_a_valid_token_works_once(world):
    w = world
    aid = _approval(w)
    push.tick(w["conn"], w["settings"], now=DAY)
    by_ep = {c["endpoint"]: c for c in w["calls"] if c.get("approval_id") == aid}
    assert set(by_ep) == {PHONE, PC}
    assert [a["action"] for a in by_ep[PHONE]["actions"]] == ["approve", "reject"]
    assert by_ep[PHONE]["action_token"] != by_ep[PC]["action_token"]
    # only a hash is stored
    stored = [r["token_hash"] for r in w["conn"].execute("SELECT token_hash FROM push_actions")]
    assert by_ep[PHONE]["action_token"] not in stored

    r = _act(w["phone"], aid, by_ep[PHONE]["action_token"], PHONE)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert approvals.get(w["conn"], aid)["status"] == "approved"
    # the same token again: refused
    assert _act(w["phone"], aid, by_ep[PHONE]["action_token"], PHONE).status_code == 403
    # the other device's token for a decided approval: valid token, but nothing left to decide
    assert _act(w["pc"], aid, by_ep[PC]["action_token"], PC).status_code == 409


def test_reject_from_the_notification(world):
    w = world
    aid = _approval(w)
    tok = push.action_token(w["conn"], _sub_row(w["conn"], PHONE), aid)
    w["conn"].commit()
    assert _act(w["phone"], aid, tok, PHONE, "reject").json()["status"] == "rejected"


def test_wrong_device_wrong_approval_wrong_endpoint_and_expired_tokens_fail(world):
    w = world
    conn = w["conn"]
    aid, other = _approval(w), _approval(w, "Another")
    phone_sub = _sub_row(conn, PHONE)

    def fresh(approval_id=aid, now=None):
        tok = push.action_token(conn, phone_sub, approval_id, now=now)
        conn.commit()
        return tok

    # the phone's token from the PC's session (even with the phone's endpoint): refused
    assert _act(w["pc"], aid, fresh(), PHONE).status_code == 403
    # the phone's token for one approval used on another: refused
    assert _act(w["phone"], other, fresh(), PHONE).status_code == 403
    # the wrong subscription endpoint: refused
    assert _act(w["phone"], aid, fresh(), PC).status_code == 403
    # expired
    old = fresh(now=datetime.now(timezone.utc) - timedelta(seconds=push.TTL_S + 60))
    assert _act(w["phone"], aid, old, PHONE).status_code == 403
    # made up
    assert _act(w["phone"], aid, "x" * 43, PHONE).status_code == 403
    # no session at all
    assert _act(w["stranger"], aid, fresh(), PHONE).status_code == 401
    # a token refused once is spent: the right device cannot use it after a failed try either
    tok = fresh()
    assert _act(w["phone"], other, tok, PHONE).status_code == 403
    assert _act(w["phone"], aid, tok, PHONE).status_code == 403
    assert approvals.get(conn, aid)["status"] == approvals.get(conn, other)["status"] == "pending"
    # and the right one still works
    assert _act(w["phone"], aid, fresh(), PHONE).status_code == 200


def test_a_signed_out_device_loses_its_buttons(world):
    w = world
    conn = w["conn"]
    aid = _approval(w)
    tok = push.action_token(conn, _sub_row(conn, PHONE), aid)
    conn.commit()
    sid = _sub_row(conn, PHONE)["device_id"]
    devices.revoke(conn, actors.owner_id(conn), sid)  # its subscription goes with it
    conn.commit()
    devices._cache.clear()
    assert _act(w["phone"], aid, tok, PHONE).status_code == 401
    assert approvals.get(conn, aid)["status"] == "pending"
