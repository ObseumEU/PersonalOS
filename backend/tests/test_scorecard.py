"""The company scorecard (pos.scorecard), the owner frustration detector (pos.frustration) and the
platform self-improvement loop (pos.platform_loop): calculations on a fixture DB, snapshots and
week-over-week deltas, measured goals, the CEO's routines on the scorecard, the weekly meeting and retro."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from pos import (actors, agents, business, chat, frustration, goals, integrations, meetings, platform_loop, schedules,
                 scorecard, tasks, weekly_packet)
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture(autouse=True)
def no_outside(monkeypatch):
    monkeypatch.delenv("POS_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(weekly_packet, "http_get", None)
    monkeypatch.setenv("POS_KNIHA_DIR", "/nonexistent-kniha")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "s.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    c.execute("UPDATE actors SET name = 'David' WHERE is_owner = 1")
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role, lead=None, team=None) -> int:
    made = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                               permissions=["tasks:read", "tasks:write", "tasks:claim", "tasks:review",
                                            "messages:send", "approvals:request"])
    aid = made["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ?, team = ?, runtime = 'codex_worker' WHERE id = ?",
                 (role, lead, team, aid))
    conn.commit()
    return aid


@pytest.fixture
def company(conn, owner, tmp_path):
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo, "engineering")
    out = {"ceo": ceo, "cto": cto}
    for name, role in (("Software Engineer", "developer"), ("QA Reviewer", "qa"), ("SRE", "sre"),
                       ("Security Engineer", "security")):
        out[name] = _agent(conn, owner, tmp_path, name, role, cto, "engineering")
    out["cs"] = _agent(conn, owner, tmp_path, "Head of Customer Success", "customer_success", ceo, "customers")
    return out


NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _task(conn, ctx, title, *, status="next", created=None, completed=None, **kw) -> int:
    t = tasks.create(conn, ctx, {"title": title, **kw})
    conn.execute("UPDATE tasks SET status = ?, created_at = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                 (status, iso(created or NOW - timedelta(days=2)), iso(completed) if completed else None,
                  iso(completed or created or NOW - timedelta(days=2)), t["id"]))
    return t["id"]


def _usage(conn, task_id, usd, at=None, actor=None):
    conn.execute("INSERT INTO engine_usage (at, engine, actor_id, task_id, cost_usd) VALUES (?, 'claude', ?, ?, ?)",
                 (iso(at or NOW - timedelta(days=1)), actor, task_id, usd))


def _run(conn, actor, status, at=None):
    conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', ?, ?)",
                 (actor, status, iso(at or NOW - timedelta(days=1))))


# ------------------------------------------------------------------ the calculations

def test_scorecard_numbers_on_a_fixture_db(conn, owner, company):
    # The owner's requests: 3 done (4 h, 10 h, 30 h), 1 open; an agent's own task does not count.
    for h in (4, 10, 30):
        start = NOW - timedelta(days=5)
        _task(conn, owner, f"Udělej {h}", status="done", created=start, completed=start + timedelta(hours=h),
              assignee="CTO")
    _task(conn, owner, "Ještě otevřené", status="working", created=NOW - timedelta(days=3), assignee="CTO")
    _task(conn, Ctx(company["cto"]), "Interní věc agenta")
    # Spend: business 2 $ (customer mail), platform 6 $ (an incident).
    mail = _task(conn, owner, "Dotaz zákazníka", status="done", source="event:gmail", assignee="Head of Customer Success",
                 completed=NOW - timedelta(days=1))
    incident = _task(conn, owner, "API down", source="event:sentinel", topic="provoz")
    _usage(conn, mail, 2.0)
    _usage(conn, incident, 6.0)
    # Runs: 8 ok, 2 failed; 25 results in review (the oldest 3 days); one loop caught.
    for i in range(10):
        _run(conn, company["Software Engineer"], "ok" if i < 8 else "error")
    for i in range(25):
        _task(conn, Ctx(company["cto"]), f"Revize {i}", status="review", created=NOW - timedelta(days=3))
    conn.execute("INSERT INTO audit_log (at, via, action) VALUES (?, 'system', 'chat_loop')", (iso(NOW - timedelta(hours=5)),))
    conn.execute("INSERT INTO deploys (old_sha, new_sha, status, created_at) VALUES ('a', 'b', 'ok', ?)",
                 (iso(NOW - timedelta(days=1)),))
    conn.commit()

    card = scorecard.compute(conn, NOW)
    o = card["owner"]
    assert (o["total"], o["done"], o["open"]) == (4, 3, 1)  # mail (event:gmail) and the agent's own task are not his
    assert o["delivered_pct"] == 75 and o["median_hours"] == 10.0
    assert o["oldest_open"][0]["title"] == "Ještě otevřené"
    sp = card["spend"]
    assert sp["business_share"] == 0.25 and sp["platform_share"] == 0.75 and sp["total_usd"] == 8.0
    assert sp["usd_per_delivered"] is not None
    a = card["agents"]
    assert (a["runs_ok"], a["runs_failed"], a["fail_rate"]) == (8, 2, 0.2)
    assert a["review_queue"] == 25 and a["review_oldest_hours"] >= 72 and a["loops"] == 1
    w = card["world"]
    assert w["outbound"]["sent"] == 0 and w["deploys_ok"] == 1 and w["customers_helped"] == 1
    texts = [p["text"] for p in card["problems"]]
    assert len(texts) == 3
    assert "0 odeslaných zpráv za 7 dní" in texts
    assert any(t.startswith("Byznys jen 25 %") for t in texts)
    md = scorecard.render(card)
    assert "Top problémy" in md and "revize ve frontě **25**" in md


def test_outbound_comes_from_package_a_when_it_exists(conn, owner, company, monkeypatch):
    from pos import outbound

    conn.execute("INSERT INTO audit_log (at, via, action, detail) VALUES (?, 'mcp', 'outbound:email.send', ?)",
                 (iso(datetime.now(timezone.utc) - timedelta(hours=1)), json.dumps({"status": "sent"})))
    real = outbound.outbound_stats
    monkeypatch.delattr(outbound, "outbound_stats")  # without package A: the audit log
    assert scorecard.outbound(conn)["sent"] == 1 and scorecard.outbound(conn)["source"] == "audit"
    monkeypatch.setattr(outbound, "outbound_stats", real, raising=False)  # package A's ledger (empty here)
    assert scorecard.outbound(conn)["source"] == "outbound_stats" and scorecard.outbound(conn)["sent"] == 0
    monkeypatch.setattr(outbound, "outbound_stats", lambda days: {"sent": 7, "replies": 2, "failed": 1}, raising=False)
    got = scorecard.outbound(conn)
    assert (got["sent"], got["replies"], got["failed"], got["source"]) == (7, 2, 1, "outbound_stats")


# ------------------------------------------------------------------ snapshots and deltas

def test_snapshot_once_a_day_and_week_over_week_deltas(conn, owner, company):
    g = goals.create(conn, owner, {"title": "Platforma: podíl byznysu na nákladech", "metric": "%", "baseline": 0,
                                   "current": 10, "target_value": 50, "owner": "CEO"})
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    old = scorecard.compute(conn, week_ago)
    old["agents"]["review_queue"] = 40
    scorecard.store(conn, old)
    goals.update(conn, owner, g["id"], {"current": 30})
    for i in range(5):
        _task(conn, owner, f"R{i}", status="review", created=datetime.now(timezone.utc) - timedelta(hours=1))
    conn.commit()

    card = scorecard.view(conn)
    assert card["compared_with"] == old["day"]
    k = card["kpis"]["review_queue"]
    assert (k["value"], k["prev"], k["delta"], k["good"]) == (5, 40, -35, True)
    goal = next(x for x in card["goals"] if x["id"] == g["id"])
    assert goal["delta"] == 20 and goal["progress"] == 60 and goal["progress_delta"] == 40
    assert [p[1] for p in goal["trend"]] == [10, 30]
    # Stored once: a second read takes the stored slow parts, the live counters are fresh.
    first = conn.execute("SELECT created_at FROM scorecard_snapshots WHERE day = ?", (card["day"],)).fetchone()[0]
    _task(conn, owner, "R-new", status="review", created=datetime.now(timezone.utc))
    conn.commit()
    again = scorecard.view(conn)
    assert again["agents"]["review_queue"] == 6
    assert conn.execute("SELECT created_at FROM scorecard_snapshots WHERE day = ?", (card["day"],)).fetchone()[0] == first


def test_measured_goals_update_from_kniha_files_and_spend(conn, owner, company, tmp_path, monkeypatch):
    kniha = tmp_path / "kniha"
    (kniha / "provoz").mkdir(parents=True)
    (kniha / "marketing").mkdir()
    (kniha / "provoz" / "partneri.csv").write_text(
        "kategorie,firma,email,stav,datum_osloveni,odpoved\n"
        "a,A,a@x.cz,osloveno,2026-10-01,\n"
        "b,B,b@x.cz,rozhovor domluven,2026-10-02,ano\n"
        "c,C,c@x.cz,,,\n", encoding="utf-8")
    (kniha / "marketing" / "warm-outreach-pilot-tabulka.md").write_text(
        "# Tabulka\n\n| src | Kontakt | Datum oslovení | Osloveno | Odpověď | Rozhovor | Rezervace |\n"
        "|---|---|---|---|---|---|---|\n| wo-01 | – | 2.10. | ano | ano | ano | ne |\n"
        "| wo-02 | – | – | ne | – | ne | ne |\n", encoding="utf-8")
    monkeypatch.setenv("POS_KNIHA_DIR", str(kniha))
    assert scorecard.kniha_counts() == {"contacts": 3, "replies": 2, "interviews": 2}
    for spec in [{"title": "Kniha pilot: oslovené kontakty", "baseline": 0, "current": 0, "target_value": 50},
                 {"title": "Kniha pilot: rozhovory se zájemci", "baseline": 0, "current": 0, "target_value": 10},
                 {"title": "Platforma: podíl byznysu na nákladech", "baseline": 39, "current": 39, "target_value": 50},
                 {"title": "Něco neměřitelného", "current": 3, "target_value": 5}]:
        goals.create(conn, owner, spec)
    mail = _task(conn, owner, "Dotaz zákazníka", status="done", source="event:gmail",
                 completed=datetime.now(timezone.utc) - timedelta(hours=2))
    _usage(conn, mail, 3.0, at=datetime.now(timezone.utc) - timedelta(hours=2))
    conn.commit()
    changed = {c["title"]: c["to"] for c in scorecard.update_goals(conn)}
    assert changed == {"Kniha pilot: oslovené kontakty": 3.0, "Kniha pilot: rozhovory se zájemci": 2.0,
                       "Platforma: podíl byznysu na nákladech": 100.0}
    assert scorecard.update_goals(conn) == []  # nothing new: nothing written
    out = scorecard.daily(conn)
    assert out["goals_updated"] == 0 and out["day"]


def test_the_ceo_monday_plan_and_friday_board_carry_the_scorecard(conn, owner, company):
    for name in scorecard.CEO_SCHEDULES:
        s = schedules.create(conn, owner, {"name": name, "schedule": "weekly mon 08:00", "title": name,
                                           "notes": "Follow the section.", "visibility": "team",
                                           "assignee": {"type": "agent", "id": company["ceo"]}})
        out = schedules.fire(conn, s["id"])
        t = tasks.get(conn, owner, tasks.parse_id(out["task"]))
        assert "## Scorecard firmy" in t["notes"] and "### Top problémy" in t["notes"]
    other = schedules.create(conn, owner, {"name": "CTO: něco", "schedule": "daily 07:00", "notes": "x",
                                           "visibility": "team", "assignee": {"type": "agent", "id": company["cto"]}})
    t = tasks.get(conn, owner, tasks.parse_id(schedules.fire(conn, other["id"])["task"]))
    assert "Scorecard" not in t["notes"]


# ------------------------------------------------------------------ the frustration detector

@pytest.mark.parametrize("text,flagged", [
    ("tvl!!!", True),
    ("Hele nefunguje HA co stim je over a oprav to prosim", True),
    ("TVL to ma nekdo orece hlidat ty vypadky", True),
    ("Dokonči to konečně", True),
    ("kolikrát to mám psát???", True),
    ("Už zase to nejede", True),
    ("zase to nejde!", True),
    ("pošli to zase Petrovi", False),
    ("Díky, super práce.", False),
    ("Proč je to modré?", False),
])
def test_frustration_markers(text, flagged):
    assert bool(frustration.markers(text)) is flagged


def test_a_frustrated_owner_message_reaches_the_ceo_with_context(conn, owner, company, monkeypatch):
    from pos import wake

    woken = []
    monkeypatch.setattr(wake, "wake", lambda aid: woken.append(aid) or 1)
    chat.send_dm(conn, owner, company["cto"], "Prosím zkontroluj zálohy serveru svr03 a pošli mi výsledek")
    chat.send_dm(conn, owner, company["cto"], "Díky.")
    m = chat.send_dm(conn, owner, company["cto"], "zkontroluj zálohy serveru svr03 a pošli výsledek!!!")
    row = conn.execute("SELECT * FROM owner_frustration WHERE message_id = ?", (m["id"],)).fetchone()
    found = json.loads(row["markers"])
    assert "opakovaný požadavek" in found and "!!!" in found and row["repeat_of"]
    t = tasks.get(conn, owner, row["task_id"])
    assert t["title"].startswith("Frustrace majitele") and t["assignee_id"] == company["ceo"] and t["priority"] == 1
    assert "DM s CTO" in t["notes"] and "Opakuje požadavek" in t["notes"] and "Díky." in t["notes"]
    assert company["ceo"] in woken
    # A second one the same day: a comment on the same task, not a new task.
    m2 = chat.send_dm(conn, owner, company["cto"], "tvl nefunguje to")
    again = conn.execute("SELECT task_id FROM owner_frustration WHERE message_id = ?", (m2["id"],)).fetchone()
    assert again["task_id"] == row["task_id"]
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE 'Frustrace majitele%'").fetchone()[0] == 1
    # A calm message is not flagged.
    m3 = chat.send_dm(conn, owner, company["cto"], "Dobře, díky za opravu.")
    assert conn.execute("SELECT 1 FROM owner_frustration WHERE message_id = ?", (m3["id"],)).fetchone() is None
    assert frustration.stats(conn)["frustrations"] == 2


def test_double_answers_and_unanswered_asks(conn, owner, company):
    now = datetime.now(timezone.utc)
    a = chat.send_dm(conn, owner, company["cto"], "Jak jsme na tom s deployem?")
    b = chat.send_dm(conn, owner, company["ceo"], "Co plán na týden?")
    ch_a = conn.execute("SELECT channel_id FROM chat_messages WHERE id = ?", (a["id"],)).fetchone()[0]
    for who, body in ((company["cto"], "Deploy běží."), (company["ceo"], "Doplním: testy prošly.")):  # two answers
        conn.execute("INSERT INTO chat_messages (channel_id, author_id, body, trust, created_at) VALUES (?, ?, ?, 'agent', ?)",
                     (ch_a, who, body, iso(now)))
    old = iso(now - timedelta(hours=3))
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (old, b["id"]))
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id IN (SELECT id FROM chat_messages WHERE channel_id = ? "
                 "AND id > ?)", (iso(now - timedelta(hours=2, minutes=58)), ch_a, a["id"]))
    conn.execute("UPDATE chat_messages SET created_at = ? WHERE id = ?", (iso(now - timedelta(hours=3)), a["id"]))
    conn.commit()
    st = frustration.stats(conn, now)
    assert st["double_answers"] == 1 and st["unanswered"] == 1


# ------------------------------------------------------------------ the platform loop

def test_platform_channel_project_meeting_and_retro(conn, owner, company):
    where = platform_loop.ensure(conn)
    assert platform_loop.ensure(conn) == where  # idempotent
    ch = conn.execute("SELECT * FROM channels WHERE id = ?", (where["channel_id"],)).fetchone()
    assert ch["name"] == "platform"
    members = {r[0] for r in conn.execute("SELECT actor_id FROM channel_members WHERE channel_id = ?", (ch["id"],))}
    assert {company["cto"], company["Software Engineer"], company["QA Reviewer"], company["SRE"],
            company["Security Engineer"]} <= members
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (where["project_id"],)).fetchone()
    assert p["name"] == "PersonalOS zlepšení" and p["channel_id"] == ch["id"]

    _run(conn, company["Software Engineer"], "error", datetime.now(timezone.utc) - timedelta(hours=3))
    conn.commit()
    out = platform_loop.start_meeting(conn)
    m = meetings.view(conn, out["meeting"])
    assert m["topic"] == "Platforma: zlepšení týdne"
    agenda = conn.execute("SELECT agenda, facilitator_id, project_id FROM meetings WHERE id = ?", (out["meeting"],)).fetchone()
    assert agenda["facilitator_id"] == company["cto"] and agenda["project_id"] == p["id"]
    assert "Výstup" in agenda["agenda"] and "selhané běhy Software Engineer" in agenda["agenda"]
    assert len(agenda["agenda"]) <= 3000
    # A second start while it runs: skipped, not an error.
    assert "skipped" in platform_loop.start_meeting(conn)

    # The CTO decides: the backlog items land in the project; one gets done; the retro reports it.
    d = meetings.decide(conn, Ctx(company["cto"], via="mcp"), out["meeting"], "Tři položky",
                        tasks_=[{"title": "Zkrátit frontu revizí", "assignee": "Software Engineer",
                                 "notes": "Důkaz: fronta 25. Metrika: fronta < 10. Oblast: pos/review_policy.py"}])
    tid = tasks.parse_id(d["tasks"][0])
    assert conn.execute("SELECT project_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == p["id"]
    conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (iso(datetime.now(timezone.utc)), tid))
    conn.commit()
    r = platform_loop.retro(conn)
    assert r["done"] == 1
    body = conn.execute("SELECT body FROM chat_messages WHERE id = ?", (r["posted"],)).fetchone()[0]
    assert "retro" in body and "Zkrátit frontu revizí" in body
    packet = weekly_packet.build(conn, outside=False)
    sec = packet["platform_improvement"]
    assert sec["available"] and [t["title"] for t in sec["backlog"]["done"]] == ["Zkrátit frontu revizí"]
    assert "review_queue" in sec["metrics"]
