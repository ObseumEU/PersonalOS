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


def test_step_cap_on_a_task_from_the_owners_chat_message_is_reported_in_his_thread(env):
    client, conn, owner, aid, key = env
    msg = chat.send_dm(conn, owner, aid, "investiguj, nepřestávej dokud nenajdeš řešení")
    # the agent made itself a task that cites the owner's message (as the HA Specialist did, T-167)
    t = tasks.create(conn, Ctx(aid), {"title": "Watchdog na síť HA", "notes": f"Owner (chat, zpráva {msg['id']}) chce řešení.",
                                      "assignee": {"type": "agent", "id": aid}})
    tasks.update(conn, Ctx(aid), t["id"], {"status": "working", "progress_note": "Config je validní, restartuji Core."})
    conn.commit()
    h = {"Authorization": f"Bearer {key}"}
    r = client.post(f"/api/worker/tasks/{t['ref']}/handback", headers=h,
                    json={"note": "step limit reached (40 steps); last note: Restarting Core."})
    assert r.status_code == 200
    replies = _thread(conn, msg["id"])
    assert len(replies) == 1 and replies[0]["author_id"] == aid
    body = replies[0]["body"]
    assert t["ref"] in body and "limit 40 kroků" in body and "Config je validní" in body and "Co dál" in body
    # once per task and outcome: a second hand-back does not repeat it
    tasks.assign(conn, owner, t["id"], {"type": "agent", "id": aid})
    conn.commit()
    client.post(f"/api/worker/tasks/{t['ref']}/handback", headers=h,
                json={"note": "step limit reached (40 steps); last note: again"})
    assert len(_thread(conn, msg["id"])) == 1


def test_a_task_the_owner_created_gets_a_comment_and_an_agents_own_task_nothing(env):
    client, conn, owner, aid, key = env
    mine = tasks.create(conn, owner, {"title": "Světlo v garáži", "assignee": {"type": "agent", "id": aid}})
    theirs = tasks.create(conn, Ctx(aid), {"title": "Vlastní úklid", "assignee": {"type": "agent", "id": aid}})
    conn.commit()
    h = {"Authorization": f"Bearer {key}"}
    for t in (mine, theirs):
        assert client.post(f"/api/worker/tasks/{t['ref']}/handback", headers=h, json={"note": "no entity"}).status_code == 200
    rows = conn.execute("SELECT task_id, kind, body FROM task_comments WHERE kind = 'system' AND body LIKE 'Hlášení%'").fetchall()
    assert [r["task_id"] for r in rows] == [mine["id"]] and "úkol vrátil" in rows[0]["body"]
    assert owner_notice.origin(conn, theirs["id"]) is None


def test_the_dm_counterpart_posts_when_another_agent_got_the_work(env, tmp_path):
    client, conn, owner, aid, key = env
    ceo = agents.create_agent(conn, owner, name="Lead Tester", purpose="lead", lifetime="long_lived",
                              data_dir=tmp_path)["agent"]
    msg = chat.send_dm(conn, owner, ceo["id"], "světlo v garáži na pohyb")
    t = tasks.create(conn, Ctx(ceo["id"]), {"title": "HA: garáž", "notes": f"Odkud: zpráva ownera v chatu (DM, zpráva {msg['id']}).",
                                            "assignee": {"type": "agent", "id": aid}})
    conn.commit()
    assert owner_notice.notify(conn, t["id"], aid, "blocked", "no entity found") is not None
    reply = _thread(conn, msg["id"])[0]
    assert reply["author_id"] == ceo["id"] and "je zablokovaný" in reply["body"]
