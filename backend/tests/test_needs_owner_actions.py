""""Čeká na tebe" items the owner acts on in one click: access requests only he decides, approved LinkedIn
posts to publish, and Gmail drafts that wait for him (pos.needs_me), each with its endpoint, and the push."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, approvals, needs_me, outbound_drafts, outbound_linkedin, push
from pos import outbound_ledger as ledger
from pos.access import service as access
from pos.config import Settings
from pos.core import Ctx, now_iso
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENT_RUNTIME", "codex")
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    made = agents.create_agent(conn, owner, name="Writer", purpose="writes", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim", "approvals:request"], data_dir=tmp_path)
    conn.commit()
    yield {"client": client, "conn": conn, "owner": owner, "agent": made["agent"]["id"], "settings": settings}
    conn.close()
    client.__exit__(None, None, None)


DAY = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)  # 12:00 in Prague, outside quiet hours


def _items(conn, owner, kind):
    return [i for i in needs_me.collect(conn, owner)["items"] if i["kind"] == kind]


# ------------------------------------------------------------------ access requests

def test_owner_only_access_requests_wait_in_needs_me_with_grant_and_deny(app):
    conn, owner, agent, client = app["conn"], app["owner"], app["agent"], app["client"]
    # most requests are granted at once and never wait for him
    assert access.request_access(conn, Ctx(agent), what="capability", capability="tasks:write",
                                 why="x")["status"] == "granted"
    assert _items(conn, owner, "access") == []

    first = access.request_access(conn, Ctx(agent), what="capability", capability="secrets:smtp", why="send mail")
    second = access.request_access(conn, Ctx(agent), what="capability", capability="browser:profile", why="log in")
    items = _items(conn, owner, "access")
    assert [i["id"] for i in items] == [second["request_id"], first["request_id"]]
    it = items[1]
    assert it["key"] == f"access:{first['request_id']}" and it["from_name"] == "Writer"
    assert "secrets:smtp" in it["title"] and it["detail"] == "send mail"
    assert it["decide_url"] == f"/api/access/requests/{first['request_id']}/decide"
    assert needs_me.collect(conn, owner)["counts"]["access"] == 2

    # the owner's buttons: grant one, deny the other; both leave the list
    r = client.post(it["decide_url"], json={"decision": "grant", "note": "Schváleno majitelem."})
    assert r.status_code == 200, r.text
    r = client.post(items[0]["decide_url"], json={"decision": "deny", "note": "Zamítnuto majitelem."})
    assert r.status_code == 200, r.text
    assert _items(conn, owner, "access") == []
    # nobody else sees them
    assert _items(conn, Ctx(agent), "access") == []


def test_a_credential_request_is_decided_through_the_credentials_endpoint(app):
    conn, owner, agent = app["conn"], app["owner"], app["agent"]
    rid = access._insert_request(conn, agent_id=agent, requested_by=agent, trigger="request", what="capability",
                                 capability="cred:github-deploy", why="push the release", needs_owner=1)
    conn.commit()
    (it,) = _items(conn, owner, "access")
    assert it["decide_url"] == f"/api/credentials/requests/{rid}/decide"
    assert it["link"] == f"/credentials?request={rid}"


def test_an_owner_only_access_request_is_pushed_once_and_its_chat_ping_is_not(app, monkeypatch):
    conn, owner, agent = app["conn"], app["owner"], app["agent"]
    keys = push.generate_vapid()
    settings = Settings(data_dir=app["settings"].data_dir, vapid_private_key=keys[0], vapid_public_key=keys[1],
                        scheduler=False)
    calls = []
    monkeypatch.setattr(push, "TRANSPORT", lambda sub, data, **kw: calls.append(json.loads(data)) or 201)
    push.subscribe(conn, owner.actor_id, {"endpoint": "https://fcm.googleapis.com/fcm/send/x",
                                          "keys": {"p256dh": "k", "auth": "a"}}, device_id="dev-1")
    conn.commit()
    push.tick(conn, settings, now=DAY)  # the cursor starts now
    out = access.request_access(conn, Ctx(agent), what="capability", capability="secrets:smtp", why="send mail")
    sent = push.tick(conn, settings, now=DAY)
    assert [s["payload"]["tag"] for s in sent] == [f"needs-access:{out['request_id']}"]
    assert sent[0]["payload"]["title"].startswith("Žádost o přístup")
    assert push.tick(conn, settings, now=DAY) == []


# ------------------------------------------------------------------ LinkedIn posts to publish

def _approved_post(conn, agent, text, result):
    a = approvals.request(conn, Ctx(agent), "linkedin.post", {"payload": {"text": text}})
    conn.execute("UPDATE approvals SET status = 'approved', decided_at = ?, result = ? WHERE id = ?",
                 (now_iso(), json.dumps(result), a["id"]))
    conn.commit()
    return a["id"]


def test_an_approved_linkedin_post_waits_with_a_publish_button(app, monkeypatch):
    conn, owner, agent, client = app["conn"], app["owner"], app["agent"], app["client"]
    ready = _approved_post(conn, agent, "Nový článek o PersonalOS", {"status": "ready_to_publish", "text": "x"})
    _approved_post(conn, agent, "Already out", {"status": "sent", "post": "urn:li:share:1"})
    (it,) = _items(conn, owner, "publish")
    assert it["id"] == ready and it["title"] == "LinkedIn: Nový článek o PersonalOS"
    assert it["publish_url"] == f"/api/integrations/linkedin/publish/{ready}"
    assert it["connected"] is False and it["connect_url"] == "/api/integrations/linkedin/start"

    monkeypatch.setattr(outbound_linkedin, "publish", lambda conn, payload: {"status": "sent", "post": "urn:li:share:2"})
    r = client.post(it["publish_url"])
    assert r.status_code == 200 and r.json()["status"] == "sent"
    assert _items(conn, owner, "publish") == []


# ------------------------------------------------------------------ Gmail drafts

def _draft(conn, agent, to, subject, campaign):
    ledger.ensure_schema(conn)
    rid = conn.execute(
        """INSERT INTO outbound_sends (key, actor_id, action, status, result, created_at, campaign, recipient)
           VALUES (?, ?, 'email.send', 'drafted', ?, ?, ?, ?)""",
        (f"k-{to}", agent, json.dumps({"draft_id": f"d-{to}", "to": to, "subject": subject,
                                       "link": f"https://mail.google.com/mail/u/0/#drafts/{to}"}),
         now_iso(), campaign, to)).lastrowid
    conn.commit()
    return rid


def test_waiting_drafts_list_with_gmail_links_and_sent_or_discarded_buttons(app):
    conn, owner, agent, client = app["conn"], app["owner"], app["agent"], app["client"]
    a = _draft(conn, agent, "a@acme.cz", "Nabídka", "campaign:jaro")
    b = _draft(conn, agent, "b@acme.cz", "Nabídka", "campaign:jaro")
    outbound_drafts.owner_item(conn, a)
    outbound_drafts.owner_item(conn, b)
    lone = _draft(conn, agent, "c@beta.cz", "Faktura", None)  # no campaign item: its own row
    conn.commit()

    (ask,) = [i for i in _items(conn, owner, "ask") if i.get("drafts")]
    assert [d["id"] for d in ask["drafts"]] == [a, b]
    assert ask["drafts"][0]["link"].startswith("https://mail.google.com/")
    assert ask["drafts"][0]["mark_url"] == f"/api/outbound/drafts/{a}"
    (it,) = _items(conn, owner, "draft")
    assert it["id"] == lone and it["title"] == "Koncept e-mailu pro c@beta.cz: Faktura"
    assert it["links"] == [{"label": "Otevřít v Gmailu", "href": "https://mail.google.com/mail/u/0/#drafts/c@beta.cz"}]

    assert client.post(f"/api/outbound/drafts/{lone}", json={"state": "discarded"}).status_code == 200
    assert _items(conn, owner, "draft") == []
    assert client.post(f"/api/outbound/drafts/{a}", json={"state": "sent"}).status_code == 200
    (ask,) = [i for i in _items(conn, owner, "ask") if i.get("drafts")]
    assert [d["id"] for d in ask["drafts"]] == [b]
    assert client.post(f"/api/outbound/drafts/{b}", json={"state": "sent"}).status_code == 200
    assert not [i for i in _items(conn, owner, "ask") if i.get("drafts")]  # the campaign item closed itself
    # a draft is only ever another agent's link into Gmail, never an arbitrary URL
    bad = _draft(conn, agent, "d@x.cz", "Hi", None)
    conn.execute("UPDATE outbound_sends SET result = ? WHERE id = ?",
                 (json.dumps({"to": "d@x.cz", "draft_id": "z", "link": "javascript:alert(1)"}), bad))
    conn.commit()
    (it,) = _items(conn, owner, "draft")
    assert it["links"] == [] and it["draft"]["link"] is None
