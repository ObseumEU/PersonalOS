"""The chat audit (prod 2026-09-27): unaddressed questions reach someone, role and team mentions,
acknowledgements wake nobody, ping-pong loops stop and go to the lead, duplicates, length, cheap
reads, project teams stay in their channel."""


import pytest

from pos import actors, agents, chat, integrations, workers
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "c.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    integrations.install()
    yield c
    c.close()


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


def make(conn, me, tmp_path, name, role=None, team=None, reports_to=None,
         perms=("tasks:read", "tasks:claim", "messages:send")):
    aid = agents.create_agent(conn, me, name=name, purpose=f"{name} work", lifetime="long_lived",
                              permissions=list(perms), data_dir=tmp_path)["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, team = ?, reports_to = ?, runtime = 'codex_worker' WHERE id = ?",
                 (role, team, reports_to, aid))
    conn.commit()
    return aid


def ceo_of(conn, me, tmp_path):
    return chat.role_member(conn, "ceo") or make(conn, me, tmp_path, "Chief", role="ceo")


def answer_tasks(conn, aid):
    return conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND title LIKE 'Chat: answer%' "
                        "AND archived_at IS NULL", (aid,)).fetchall()


def test_unaddressed_owner_question_in_team_goes_to_the_ceo_once(conn, me, tmp_path):
    ceo = ceo_of(conn, me, tmp_path)
    team = chat.ensure_team_channel(conn)
    out = chat.send(conn, me, team, "Kdo má na starost zálohy?")
    assert out["inbox"] == [ceo]
    assert conn.execute("SELECT reason FROM chat_inbox WHERE message_id = ?", (out["id"],)).fetchone()[0] == "mention"
    [t] = answer_tasks(conn, ceo)
    assert f"reply_to={out['id']}" in t["notes"] and "addressed nobody" in t["notes"]
    # The watch counts it as a message waiting for the CEO (never silently unanswered).
    pending = workers._owner_messages(conn, "2000-01-01", "2999-01-01")
    assert (out["id"], ceo) in [(m["id"], a) for m, a in pending]
    # Its answer in the thread closes it; the double submit of the same question is one message.
    again = chat.send(conn, me, team, "Kdo má na starost zálohy?")
    assert again["duplicate"] and again["id"] == out["id"] and len(answer_tasks(conn, ceo)) == 1


def test_project_channel_goes_to_its_team_lead_and_thread_replies_to_the_agent(conn, me, tmp_path):
    ceo = ceo_of(conn, me, tmp_path)
    lead = make(conn, me, tmp_path, "Book Lead", role="product_lead", team="kniha", reports_to=ceo)
    dev = make(conn, me, tmp_path, "Book Dev", role="developer", team="kniha", reports_to=lead)
    ch = chat.create_channel(conn, me, "kniha", [lead, dev])
    out = chat.send(conn, me, ch["id"], "Ale ne, můžete dělat force push, to je ok")
    assert out["inbox"] == [lead] and len(answer_tasks(conn, lead)) == 1
    # A reply in the dev's thread goes to the dev, without a mention.
    post = chat.send(conn, Ctx(dev), ch["id"], "Nasadil jsem první verzi webu.")
    reply = chat.send(conn, me, ch["id"], "Proč bez testů", reply_to=post["id"])
    assert reply["inbox"] == [dev] and len(answer_tasks(conn, dev)) == 1
    # #system and #weekly keep their own handling.
    sysch = chat.ensure_system_channel(conn)
    assert chat.send(conn, me, sysch, "Poznámka pro sebe")["inbox"] == []


def test_role_and_team_mentions(conn, me, tmp_path):
    ceo = ceo_of(conn, me, tmp_path)
    cto = make(conn, me, tmp_path, "Tech Chief", role="cto", reports_to=ceo)
    lead = make(conn, me, tmp_path, "Book Lead", role="product_lead", team="kniha")
    dev = make(conn, me, tmp_path, "Book Dev", role="developer", team="kniha", reports_to=lead)
    chat.create_channel(conn, me, "kniha", [lead, dev])
    team = chat.ensure_team_channel(conn)
    out = chat.send(conn, me, team, "@CTO podívej se na zálohy, a @tým-kniha taky")
    assert set(out["mentions"]) == {cto, lead, dev}
    assert out["inbox"] == sorted([cto, lead, dev])
    assert chat.role_member(conn, "product-lead") == lead
    assert chat._mentions(conn, "mail david@obseum.cz", None) == []  # an e-mail is no mention


def test_acknowledgements_wake_nobody_and_need_no_answer(conn, me, tmp_path):
    a = make(conn, me, tmp_path, "Alpha")
    b = make(conn, me, tmp_path, "Beta")
    for text in ("Díky, beru na vědomí.", "ok", "👍", "Rozumím, děkuji za info.", "Díky za info k T-074"):
        assert chat.is_ack(text), text
    for text in ("ok, ale proč?", "Díky, přebírám T-074 a zkusím to", "ok 3", "Udělej to"):
        assert not chat.is_ack(text), text
    chat.send_dm(conn, Ctx(a), b, "Hotovo, T-012 je nasazené.")
    out = chat.send_dm(conn, Ctx(b), a, "Díky, beru na vědomí.")
    assert "chat_react" in out["platform_note"] and out["delivered_to_run"] is None
    assert chat.check_inbox(conn, a) == []  # read already: never injected into a run
    # The owner's "díky" asks for no answer; his "ok" to a question is an answer.
    chat.send_dm(conn, me, a, "díky")
    assert answer_tasks(conn, a) == []
    chat.send_dm(conn, Ctx(a), me.actor_id, "Mám to nasadit i na produkci?")
    chat.send_dm(conn, me, a, "ok")
    assert len(answer_tasks(conn, a)) == 1


def test_ping_pong_loop_stops_and_goes_to_the_lead(conn, me, tmp_path, monkeypatch):
    monkeypatch.setenv("POS_CHAT_LOOP", "4/1800")
    lead = make(conn, me, tmp_path, "Lead")
    a = make(conn, me, tmp_path, "Alpha", reports_to=lead)
    b = make(conn, me, tmp_path, "Beta", reports_to=lead)
    outs = [chat.send_dm(conn, Ctx(x), y, f"Zpráva {i}: pokračuju na T-00{i}") for i, (x, y) in
            enumerate([(a, b), (b, a), (a, b), (b, a), (a, b), (b, a)])]
    assert "back and forth" not in (outs[2].get("platform_note") or "")
    assert "back and forth" in outs[3]["platform_note"]
    loops = conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND topic = 'chat-loop'", (lead,)).fetchall()
    assert len(loops) == 1  # once per window, however long it goes on
    assert chat.check_inbox(conn, a) == [] or all(m["id"] < outs[3]["id"] for m in chat.check_inbox(conn, a))


def test_agent_messages_are_short_and_reads_are_cheap(conn, me, tmp_path):
    a = make(conn, me, tmp_path, "Alpha")
    b = make(conn, me, tmp_path, "Beta")
    team = chat.ensure_team_channel(conn)
    with pytest.raises(chat.ChatError):
        chat.send(conn, Ctx(a), team, "x" * 3500)
    out = chat.send(conn, Ctx(a), team, "Dlouhé shrnutí. " * 90)
    assert "Long message" in out["platform_note"]
    root = chat.send(conn, me, team, "@Alpha shrň to")
    chat.send(conn, Ctx(a), team, "Krátce: hotovo.", reply_to=root["id"])
    page = chat.messages(conn, b, team, thread=root["id"], clip=100)
    assert [m["reply_to"] for m in page["messages"]] == [None, root["id"]]
    long = chat.messages(conn, b, team, clip=100)["messages"][0]
    assert "[clipped" in long["body"] and len(long["body"]) < 400
    # A private or foreign group is not an agent's to read.
    other = chat.create_channel(conn, me, "vedeni", [])
    assert not chat._is_member(conn, other["id"], a)


def test_chat_task_carries_its_context(conn, me, tmp_path):
    a = make(conn, me, tmp_path, "Alpha")
    chat.send_dm(conn, Ctx(a), me.actor_id, "Záloha DB proběhla ve 3:00, trvala 4 min.")
    out = chat.send_dm(conn, me, a, "A kam se ukládá")
    [t] = answer_tasks(conn, a)
    assert "Záloha DB proběhla" in t["notes"] and "no chat_read needed" in t["notes"]
    assert f"reply_to={out['id']}" in t["notes"]


def test_project_team_agents_leave_team(conn, me, tmp_path):
    from pos import projects

    lead = make(conn, me, tmp_path, "Book Lead", role="product_lead", team="kniha")
    team = chat.ensure_team_channel(conn)
    assert chat._is_member(conn, team, lead)
    projects.create(conn, me, name="Kniha")
    chat.ensure_team_channel(conn)
    assert not chat._is_member(conn, team, lead)
