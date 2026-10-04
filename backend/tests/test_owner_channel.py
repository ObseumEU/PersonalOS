"""The owner channel (fix package 2): his message answers only the question he answers; messages to
archived agents reach their successor; one answer per owner message; platform notices from
PersonalOS; ids he can open; the CEO's promise ledger; decision cards with a default; hiring
reports "ready" only after a test run."""

from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, asks, chat, hiring, integrations, needs_me, owner_channel, promises, tasks
from pos.core import TZ, Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "o.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    integrations.install()
    agents.seed_builtin_permissions(c)
    c.execute("UPDATE actors SET name = 'David' WHERE is_owner = 1")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def me(conn):
    return Ctx(actors.owner_id(conn))


def make(conn, me, tmp_path, name, role=None, lead=None):
    aid = agents.create_agent(conn, me, name=name, purpose=f"{name} work", lifetime="long_lived",
                              permissions=["tasks:read", "tasks:claim", "tasks:write", "messages:send",
                                           "approvals:request"], data_dir=tmp_path)["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ? WHERE id = ?", (role, lead, aid))
    conn.commit()
    return aid


def _ask_in_dm(conn, me, aid, text, minutes_ago=0):
    """The agent asks the owner in their DM and its task waits (chat_send blocking=true)."""
    t = tasks.create(conn, me, {"title": f"Work of {aid}: {text[:20]}", "assignee": {"type": "agent", "id": aid},
                                "status": "next"})
    tasks.claim(conn, Ctx(aid, via="mcp"), t["id"])
    q = chat.send_dm(conn, Ctx(aid, via="mcp"), me.actor_id, text)
    a = asks.from_chat(conn, Ctx(aid, via="mcp"), q, t["id"])
    if minutes_ago:
        at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (at, q["id"]))
    conn.commit()
    return q, a, t


def _ticket_done(conn, me, a):
    return tasks.get(conn, me, a["ticket_id"])["status"] == "done"


# ------------------------------------------------------------------ 1. his message answers only its question

def test_a_new_request_does_not_close_an_older_question(conn, me, tmp_path):
    """#1787: 'pribylo topeni Air Conditioner …' closed T-523 (the sensors question) as its answer."""
    ha = make(conn, me, tmp_path, "HA Tester")
    q, a, t = _ask_in_dm(conn, me, ha, "Které čidlo je ve sklepě?", minutes_ago=90)
    chat.send(conn, me, q["channel_id"], "přibylo topení Air Conditioner, zapracuj ho do dashboardu")
    assert not _ticket_done(conn, me, a)
    assert tasks.get(conn, me, t["id"])["status"] == "waiting"
    # his reply to that very question answers it, however late
    chat.send(conn, me, q["channel_id"], "To u okna.", reply_to=q["id"])
    assert _ticket_done(conn, me, a) and tasks.get(conn, me, t["id"])["status"] == "next"
    # the asker hears it from PersonalOS, not "from the owner" in his own DM
    told = conn.execute("""SELECT author_id FROM chat_messages WHERE body LIKE '%resolved your ask%'""").fetchall()
    assert told and all(r["author_id"] == actors.system_id(conn) for r in told)


def test_the_next_message_answers_the_single_fresh_question_only(conn, me, tmp_path):
    ha = make(conn, me, tmp_path, "HA Tester")
    q, a, _ = _ask_in_dm(conn, me, ha, "Mám to nasadit hned?")
    chat.send(conn, me, q["channel_id"], "jo, nasaď")
    assert _ticket_done(conn, me, a)
    # two open questions: a plain message answers neither
    q1, a1, _ = _ask_in_dm(conn, me, ha, "Které čidlo?")
    q2, a2, _ = _ask_in_dm(conn, me, ha, "Jaký interval?")
    chat.send(conn, me, q1["channel_id"], "stáhni data a dej mi file s daty")
    assert not _ticket_done(conn, me, a1) and not _ticket_done(conn, me, a2)
    chat.send(conn, me, q1["channel_id"], "každých 5 minut", reply_to=q2["id"])
    assert _ticket_done(conn, me, a2) and not _ticket_done(conn, me, a1)


# ------------------------------------------------------------------ 2. archived agents

def test_a_message_to_an_archived_agent_reaches_its_successor(conn, me, tmp_path):
    """#903 'at to dodelaj!!' went to the archived Asistent vedení and was never answered."""
    old = make(conn, me, tmp_path, "Asistent vedení")
    new = make(conn, me, tmp_path, "Chief of Staff")
    dm = chat.dm_channel(conn, me.actor_id, old, me)
    conn.execute("UPDATE actors SET archived_at = ? WHERE id = ?", ("2026-09-26T00:00:00+00:00", old))
    conn.commit()
    view = chat.channel_view(conn, dm["id"], me.actor_id)
    assert view["read_only"] and view["archived_dm"]["successor_name"] == "Chief of Staff"
    out = chat.send(conn, me, dm["id"], "ať to dodělají!!")
    assert out["rerouted"]["to"] == new and out["channel_id"] != dm["id"]
    assert "archivovaný" in out["body"] and "Chief of Staff" in out["body"]
    assert conn.execute("SELECT 1 FROM tasks WHERE assignee_id = ? AND title LIKE 'Chat: answer%'", (new,)).fetchone()
    # no successor: the CEO
    ceo = make(conn, me, tmp_path, "CEO", role="ceo")
    lone = make(conn, me, tmp_path, "Test nábor")
    dm2 = chat.dm_channel(conn, me.actor_id, lone, me)
    conn.execute("UPDATE actors SET archived_at = ? WHERE id = ?", ("2026-09-26T00:00:00+00:00", lone))
    assert chat.send(conn, me, dm2["id"], "hotovo?")["rerouted"]["to"] == ceo


# ------------------------------------------------------------------ 3. one answer per owner message

def test_one_answer_per_owner_message(conn, me, tmp_path):
    """21 owner messages got 2+ replies from the same agent (5 to #299; #358/#359 identical)."""
    ha = make(conn, me, tmp_path, "HA Tester")
    m = chat.send_dm(conn, me, ha, "teď to zkus")
    first = chat.send(conn, Ctx(ha), m["channel_id"], "Zkouším, restartuji.", reply_to=m["id"])
    second = chat.send(conn, Ctx(ha), m["channel_id"], "Hotovo: světlo reaguje na pohyb.", reply_to=m["id"])
    assert second["merged_from"] == first["id"] and "platform_note" in second
    visible = chat.messages(conn, me.actor_id, m["channel_id"])
    mine = [x for x in (visible.get("messages") if isinstance(visible, dict) else visible) if x["author_id"] == ha]
    assert len(mine) == 1 and "Zkouším" in mine[0]["body"] and "Hotovo" in mine[0]["body"]
    same = chat.send(conn, Ctx(ha), m["channel_id"], "Hotovo: světlo reaguje na pohyb.")  # identical again
    assert same["merged"] and same["id"] == second["id"]
    # after his next message the next answer is a new one
    m2 = chat.send_dm(conn, me, ha, "a v garáži?")
    third = chat.send(conn, Ctx(ha), m2["channel_id"], "V garáži taky.", reply_to=m2["id"])
    assert "merged_from" not in third


# ------------------------------------------------------------------ 4. notices from PersonalOS

def test_platform_identity_is_a_service_off_the_chart(conn):
    sid = actors.system_id(conn)
    assert actors.system_id(conn) == sid and actors.is_system(conn, sid)
    row = actors.get(conn, sid)
    assert row["name"] == "PersonalOS" and row["runtime"] == "service" and not row["is_owner"]


# ------------------------------------------------------------------ 5. hiring: ready only after a test run

def test_a_hire_is_ready_only_when_its_test_run_passed(conn, me, tmp_path, monkeypatch):
    cto = make(conn, me, tmp_path, "CTO", role="cto")
    ceo = make(conn, me, tmp_path, "CEO", role="ceo")
    monkeypatch.setattr(hiring, "_data_dir", lambda: tmp_path)
    req = hiring.request(conn, Ctx(ceo, via="mcp"), name="Nabu Tester", purpose="Nabu server", lead=cto,
                         permissions=["tasks:read", "tasks:claim"])
    out = hiring.decide(conn, Ctx(cto, via="mcp"), req["id"], True, data_dir=tmp_path)
    assert out["ready"] is False and out["probe_task"]
    told = [r["body"] for r in conn.execute(
        """SELECT m.body FROM chat_messages m JOIN channel_members cm ON cm.channel_id = m.channel_id
           AND cm.actor_id = ? WHERE m.author_id != ?""", (ceo, ceo))]
    assert any("NENÍ připravený" in b for b in told) and not any("is hired" in b for b in told)
    aid = out["agent_id"]
    assert actors.get(conn, aid)["engine"] == "claude"  # not the Codex default that failed on a 401
    probe = tasks.parse_id(out["probe_task"])
    from pos import head_alerts

    assert head_alerts.run_ended(conn, aid, probe, "error",
                                 "unexpected status 401 Unauthorized: Missing bearer") is None
    msgs = [r["body"] for r in conn.execute(
        """SELECT m.body FROM chat_messages m JOIN channel_members cm ON cm.channel_id = m.channel_id
           AND cm.actor_id = ? WHERE m.author_id = ?""", (cto, actors.system_id(conn)))]
    assert any("Zkušební běh" in b and "selhal" in b and "401" in b for b in msgs)
    tasks.complete(conn, Ctx(aid, via="mcp"), probe, "nástroje fungují")
    team = chat.ensure_team_channel(conn)
    assert conn.execute("SELECT 1 FROM chat_messages WHERE channel_id = ? AND body LIKE '%Nabu Tester%připravený%'",
                        (team,)).fetchone()


# ------------------------------------------------------------------ 6. promises and ids

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=TZ)  # a Saturday morning in Prague


@pytest.mark.parametrize("text,local", [
    ("Daily report ti pošlu dnes v 16:00.", "2026-10-03 16:00"),
    ("daily report dnes v 16:00", "2026-10-03 16:00"),
    ("Zítra ráno to bude hotové.", "2026-10-04 09:00"),
    ("Návrh ceny ti dám v pátek.", "2026-10-09 17:00"),
    ("Pošlu do 7. 10. ve 12:00 souhrn.", "2026-10-07 12:00"),
    ("Výsledky dodám 2026-10-12.", "2026-10-12 17:00"),
    ("Ozvu se za 2 hodiny.", "2026-10-03 11:00"),
])
def test_dated_commitments_are_read_by_the_rules(text, local):
    found = promises.extract(text, NOW)
    assert len(found) == 1
    got = datetime.fromisoformat(found[0]["due_at"]).astimezone(TZ).strftime("%Y-%m-%d %H:%M")
    assert got == local


def test_questions_and_the_past_are_not_promises():
    assert promises.extract("Mám ti to poslat zítra?", NOW) == []
    assert promises.extract("Včera v 16:00 proběhl deploy.", NOW) == []


def test_the_ceos_promise_becomes_its_task_and_the_1600_run_starts_with_the_missed_ones(conn, me, tmp_path):
    ceo = make(conn, me, tmp_path, "CEO", role="ceo")
    chat.send_dm(conn, me, ceo, "kdy bude daily report?")
    sent = chat.send_dm(conn, Ctx(ceo), me.actor_id, "Daily report ti pošlu dnes v 23:59. Také pošlu souhrn brzy.")
    row = conn.execute("SELECT * FROM owner_promises WHERE message_id = ? AND task_id IS NOT NULL",
                       (sent["id"],)).fetchone()
    t = tasks.get(conn, me, row["task_id"])
    assert t["assignee_id"] == ceo and t["title"].startswith("Slib Ownerovi") and t["deadline"]
    pending = conn.execute("SELECT * FROM owner_promises WHERE status = 'pending'").fetchall()
    assert [p["text"] for p in pending] == ["Také pošlu souhrn brzy."]  # left for the haiku fallback
    later = datetime.now(timezone.utc) + timedelta(days=2)
    out = promises.tick(conn, later, ask=lambda c, a, s, n: {"promise": True,
                                                             "due": (later + timedelta(hours=3)).astimezone(TZ)
                                                             .strftime("%Y-%m-%dT%H:%M")})
    assert out["recorded"] and t["ref"] in out["missed"]
    assert promises.tick(conn, later, ask=lambda *a: None).get("missed") is None  # told once
    # the 16:00 routine (pos.schedules.fire) starts with the promises past their time
    conn.execute("UPDATE owner_promises SET due_at = '2020-01-01T00:00:00+00:00' WHERE task_id = ?", (t["id"],))
    tpl = promises.with_missed(conn, ceo, "weekdays 16:00", {"title": "CEO: denní přehled", "notes": "Přehled."})
    assert tpl["notes"].startswith("### Nejdřív: nesplněné sliby Ownerovi") and t["ref"] in tpl["notes"]
    assert promises.with_missed(conn, ceo, "weekly mon 08:00", {"notes": "x"})["notes"] == "x"


def test_ids_to_the_owner_become_links_and_the_agent_is_warned(conn, me, tmp_path):
    ceo = make(conn, me, tmp_path, "CEO", role="ceo")
    t = tasks.create(conn, me, {"title": "Ceník Knihy", "status": "next"})
    chat.send_dm(conn, me, ceo, "jak to vypadá?")
    out = chat.send_dm(conn, Ctx(ceo), me.actor_id,
                       f"Hotovo, viz {t['ref']}, poznámka id 19 a /report/{t['ref']}; graf je na http://192.168.1.108:8123/x")
    assert "poznámka 19" in out["body"] and f"[report {t['ref']}](/report/{t['ref']})" in out["body"]
    note = out["platform_note"]
    assert "192.168.1.108" in note and t["ref"] in note and "without saying what it is" in note
    body, warn = owner_channel.rewrite_refs(conn, f"Ceník je hotový ({t['ref']}).")
    assert not warn  # named in words: no warning


# ------------------------------------------------------------------ 7. decision cards

def test_a_decision_card_takes_its_recommendation_after_the_default_time(conn, me, tmp_path):
    from pos import scheduler

    ceo = make(conn, me, tmp_path, "CEO", role="ceo")
    src = tasks.create(conn, me, {"title": "Pilot Knihy", "assignee": {"type": "agent", "id": ceo}, "status": "next"})
    out = asks.ask(conn, Ctx(ceo, via="mcp"), title="Cena Knihy pro pilot", why="Bez ceny nelze prodávat.",
                   options=["490 Kč", "690 Kč"], recommendation="690 Kč — nejlepší poměr", task_id=src["id"],
                   default_after_hours=72)
    assert out["default_at"]
    item = next(i for i in needs_me.collect(conn, me)["items"] if i["kind"] == "ask")
    assert item["options"] == ["490 Kč", "690 Kč"] and item["recommendation"] == "690 Kč"
    assert asks.adopt_defaults(conn) == {}  # not yet
    assert asks.adopt_defaults(conn, datetime.now(timezone.utc) + timedelta(hours=73)) == {"adopted": [out["ref"]]}
    assert tasks.get(conn, me, out["ticket_id"])["status"] == "done"
    assert tasks.get(conn, me, src["id"])["status"] == "next"  # the CEO goes on with the recommendation
    brief = tasks.get(conn, me, tasks.parse_id(scheduler.morning_brief(conn)["task"]))
    assert "Rozhodnuto za tebe" in brief["notes"] and "690 Kč" in brief["notes"]
    assert asks.default_digest(conn) == []  # told once
    with pytest.raises(tasks.Invalid):
        asks.choose(conn, me, out["ticket_id"], "490 Kč")  # already decided


# ------------------------------------------------------------------ the one-off seeding

def test_owner_seed_makes_the_cards_and_goal_proposals_once(conn, me, tmp_path):
    from pos import goals, owner_seed

    ceo = make(conn, me, tmp_path, "CEO", role="ceo")
    for name in ("Kniha Lead", "Head of Growth"):
        make(conn, me, tmp_path, name)
    assert all(x.startswith(("card:", "skip")) for x in owner_seed.decisions(conn))  # a dry run writes nothing
    assert not conn.execute("SELECT 1 FROM owner_asks").fetchone() if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'owner_asks'").fetchone() else True
    made = owner_seed.decisions(conn, apply=True)
    assert len([x for x in made if x.startswith("made")]) == 3  # the SRE card: T-376 is not open here
    assert all(x.startswith(("exists", "skip")) for x in owner_seed.decisions(conn, apply=True))
    cards = [i for i in needs_me.collect(conn, me)["items"] if i["kind"] == "ask" and i.get("options")]
    assert len(cards) == 3 and all(c["from_name"] == "CEO" for c in cards)
    price = next(c for c in cards if c["title"].startswith("Cena Knihy"))
    assert price["default_at"] is None  # a price: only his click
    out = owner_seed.seed_goals(conn, apply=True)
    assert len([x for x in out if x.startswith("proposed")]) == 6
    assert {g["status"] for g in goals.list_goals(conn, "all")} == {"proposed"}
    assert all(x.startswith("exists") for x in owner_seed.seed_goals(conn, apply=True))
    assert conn.execute("""SELECT 1 FROM chat_messages m JOIN channel_members cm ON cm.channel_id = m.channel_id
                           AND cm.actor_id = ? WHERE m.body LIKE '%navrhuje firemní cíl%'""", (ceo,)).fetchone()
