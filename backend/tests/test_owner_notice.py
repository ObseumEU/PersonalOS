"""Never silent toward the owner (pos.owner_notice): a task he asked for that an
agent hands back, caps out on, or cannot run tells him so in his thread."""

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, chat, owner_notice, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CODEX_DISABLED", "1")
    settings = Settings(data_dir=tmp_path)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    out = agents.create_agent(conn, owner, name="HA Tester", purpose="home", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)
    conn.commit()
    yield client, conn, owner, out["agent"]["id"], out["api_key"]
    conn.close()
    client.__exit__(None, None, None)


def _thread(conn, root):
    # In a DM the notice quotes the message (a DM has no threads); in a channel it is in its thread.
    return [dict(r) for r in conn.execute("SELECT * FROM chat_messages WHERE reply_to = ? OR quote_of = ? ORDER BY id",
                                          (root, root))]


def _dms_to(conn, to):
    return [dict(r) for r in conn.execute(
        """SELECT m.* FROM chat_messages m JOIN channels c ON c.id = m.channel_id AND c.kind = 'dm'
           JOIN channel_members cm ON cm.channel_id = c.id AND cm.actor_id = ?
           WHERE m.author_id != ? ORDER BY m.id""", (to, to))]


def _lead(conn, owner, aid, tmp_path, name="Lead Tester"):
    lead = agents.create_agent(conn, owner, name=name, purpose="lead", lifetime="long_lived",
                               data_dir=tmp_path)["agent"]
    conn.execute("UPDATE actors SET reports_to = ? WHERE id = ?", (lead["id"], aid))
    conn.commit()
    return lead["id"]


def test_step_cap_on_the_owners_request_goes_to_the_lead_in_czech_signed_by_the_platform(env, tmp_path):
    """Prod 2026-09/10: platform failure notices reached the owner, in English, under his or the CEO's
    name. Now the lead hears it, from PersonalOS; the owner's thread stays clean."""
    client, conn, owner, aid, key = env
    lead = _lead(conn, owner, aid, tmp_path)
    msg = chat.send_dm(conn, owner, aid, "investiguj, nepřestávej dokud nenajdeš řešení")
    t = tasks.create(conn, Ctx(aid), {"title": "Watchdog na síť HA", "notes": f"Owner (chat, zpráva {msg['id']}) chce řešení.",
                                      "assignee": {"type": "agent", "id": aid}})
    tasks.update(conn, Ctx(aid), t["id"], {"status": "working", "progress_note": "Config je validní, restartuji Core."})
    conn.commit()
    h = {"Authorization": f"Bearer {key}"}
    r = client.post(f"/api/worker/tasks/{t['ref']}/handback", headers=h,
                    json={"note": "step limit reached (40 steps); last note: Restarting Core."})
    assert r.status_code == 200
    assert not [m for m in _thread(conn, msg["id"]) if m["author_id"] != aid or "Hlášení" in m["body"]]
    notes = [m for m in _dms_to(conn, lead) if t["ref"] in m["body"] and "kroků" in m["body"]]
    assert len(notes) == 1
    assert notes[0]["author_id"] == actors.system_id(conn)
    body = notes[0]["body"]
    assert "limit 40 kroků" in body and "Config je validní" in body and "Co dál (pro tebe)" in body
    assert "Owner tohle hlášení nedostal" in body
    assert not [m for m in _dms_to(conn, owner.actor_id) if t["ref"] in m["body"]]  # never the owner
    # once per task and outcome: a second hand-back does not repeat it
    tasks.assign(conn, owner, t["id"], {"type": "agent", "id": aid})
    conn.commit()
    client.post(f"/api/worker/tasks/{t['ref']}/handback", headers=h,
                json={"note": "step limit reached (40 steps); last note: again"})
    assert len([m for m in _dms_to(conn, lead) if t["ref"] in m["body"] and "kroků" in m["body"]]) == 1


def test_a_task_the_owner_created_gets_a_comment_and_an_agents_own_task_nothing(env):
    client, conn, owner, aid, key = env
    mine = tasks.create(conn, owner, {"title": "Světlo v garáži", "assignee": {"type": "agent", "id": aid}})
    theirs = tasks.create(conn, Ctx(aid), {"title": "Vlastní úklid", "assignee": {"type": "agent", "id": aid}})
    conn.commit()
    h = {"Authorization": f"Bearer {key}"}
    for t in (mine, theirs):
        assert client.post(f"/api/worker/tasks/{t['ref']}/handback", headers=h, json={"note": "no entity"}).status_code == 200
    rows = conn.execute("SELECT task_id, kind, body, author_id FROM task_comments WHERE kind = 'system' "
                        "AND body LIKE '%úkol vrátil%'").fetchall()
    assert [r["task_id"] for r in rows] == [mine["id"]] and rows[0]["author_id"] == actors.system_id(conn)
    assert owner_notice.origin(conn, theirs["id"]) is None


def test_a_platform_fault_goes_to_the_sre(env, tmp_path):
    client, conn, owner, aid, key = env
    lead = _lead(conn, owner, aid, tmp_path)
    sre = agents.create_agent(conn, owner, name="SRE", purpose="platform", lifetime="long_lived",
                              data_dir=tmp_path)["agent"]["id"]
    msg = chat.send_dm(conn, owner, aid, "světlo v garáži na pohyb")
    t = tasks.create(conn, Ctx(aid), {"title": "HA: garáž", "notes": f"Odkud: zpráva {msg['id']}.",
                                      "assignee": {"type": "agent", "id": aid}})
    conn.commit()
    assert owner_notice.notify(conn, t["id"], aid, "blocked",
                               "unexpected status 401 Unauthorized: Missing bearer") is not None
    assert [m for m in _dms_to(conn, sre) if t["ref"] in m["body"] and "platformy" in m["body"]]
    assert not [m for m in _dms_to(conn, lead) if t["ref"] in m["body"]]
    assert not [m for m in _thread(conn, msg["id"]) if m["author_id"] != aid]
