"""Business value (pos.business, pos.knowledge_tool): labels, the owner's time, the chain of command,
escalation dedup, the review SLA, idle agents, one cost ledger, business KPIs, the weekly report that
always gets published, knowledge for agents, business routing and the per-task step cap."""

import json
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pos import (actors, agents, agents_code, approvals, business, chat, hiring, integrations, kb_files,
                 knowledge_tool, mcp_server, routing, tasks, weekly, weekly_packet)
from pos.access import service as access
from pos.core import Ctx, now_iso
from pos.db import connect, migrate


@pytest.fixture(autouse=True)
def no_outside(monkeypatch):
    monkeypatch.delenv("POS_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(weekly_packet, "http_get", None)
    monkeypatch.setattr(routing, "github_get", None)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "b.db")
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


def _agent(conn, owner, tmp_path, name, role, lead=None, perms=None) -> Ctx:
    made = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                               permissions=perms or ["tasks:read", "tasks:write", "tasks:claim", "tasks:review",
                                                     "messages:send", "approvals:request"])
    aid = made["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ? WHERE id = ?",
                 (role, lead.actor_id if lead else None, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


@pytest.fixture
def company(conn, owner, tmp_path):
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    cos = _agent(conn, owner, tmp_path, "Chief of Staff", "chief_of_staff", ceo)
    cfo = _agent(conn, owner, tmp_path, "CFO", "cfo", ceo)
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo)
    se = _agent(conn, owner, tmp_path, "Software Engineer", "developer", cto)
    growth = _agent(conn, owner, tmp_path, "Head of Growth", "growth", ceo)
    cs = _agent(conn, owner, tmp_path, "Head of Customer Success", "customer_success", ceo)
    return {"ceo": ceo, "cos": cos, "cfo": cfo, "cto": cto, "se": se, "growth": growth, "cs": cs}


# ------------------------------------------------------------------ business vs platform

def test_tasks_are_labelled_business_platform_or_demo(conn, owner, company):
    mail = tasks.create(conn, owner, {"title": "Dotaz zákazníka", "source": "event:gmail", "assignee": "CFO"})
    incident = tasks.create(conn, owner, {"title": "API down", "source": "event:sentinel", "topic": "provoz"})
    ours = tasks.create(conn, owner, {"title": "ObseumEU/PersonalOS#12: bug", "source": "event:github"})
    product = tasks.create(conn, owner, {"title": "ObseumEU/tesco-chat!3: feature", "source": "event:github"})
    demo = tasks.create(conn, owner, {"title": "Send the signed contract to Acme", "topic": "acme"})
    asked = tasks.create(conn, owner, {"title": "Zjisti cenu serveru", "assignee": "CTO"})
    coord = tasks.create(conn, company["ceo"], {"title": "Standup note", "assignee": "CTO"})
    kinds = {t["title"]: tasks.get(conn, owner, t["id"])["value_kind_effective"]
             for t in (mail, incident, ours, product, demo, asked, coord)}
    assert kinds == {"Dotaz zákazníka": "business", "API down": "platform",
                     "ObseumEU/PersonalOS#12: bug": "platform", "ObseumEU/tesco-chat!3: feature": "business",
                     "Send the signed contract to Acme": "demo", "Zjisti cenu serveru": "business",
                     "Standup note": "platform"}
    # The owner overrides the label; empty goes back to automatic.
    t = tasks.update(conn, owner, coord["id"], {"value_kind": "business"})
    assert t["value_kind"] == "business" and t["value_kind_effective"] == "business"
    assert tasks.update(conn, owner, coord["id"], {"value_kind": ""})["value_kind_effective"] == "platform"
    with pytest.raises(tasks.Invalid):
        tasks.update(conn, owner, coord["id"], {"value_kind": "fun"})
    # labelling is not an intervention
    assert tasks.get(conn, owner, coord["id"])["interventions"] == 0


# ------------------------------------------------------------------ the owner's time

def test_owner_dms_edits_returns_and_approvals_count_as_interventions(conn, owner, company):
    cfo = company["cfo"]
    t = tasks.create(conn, owner, {"title": "Zaplať fakturu", "assignee": "CFO"})
    tasks.claim(conn, cfo, t["id"])
    chat.send_dm(conn, owner, cfo.actor_id, "Pozor, splatnost je zítra")  # about the task it works on
    chat.send_dm(conn, owner, cfo.actor_id, "A ještě jedna věc")  # same kind within 30 min: once
    assert tasks.get(conn, owner, t["id"])["interventions"] == 1
    tasks.update(conn, owner, t["id"], {"deadline": "2026-10-01"})  # an edit
    tasks.update(conn, cfo, t["id"], {"progress": 50})  # the agent's own progress: nothing
    tasks.complete(conn, cfo, t["id"], "hotovo")
    tasks.review(conn, owner, t["id"], accept=False, comment="chybí VS")  # a return
    a = approvals.request(conn, cfo, "payment", {"amount": 100}, t["id"])
    approvals.decide(conn, owner, a["id"], True)
    assert tasks.get(conn, owner, t["id"])["interventions"] == 4
    # the scheduler or a webhook acting as the owner is not the owner's time
    tasks.update(conn, Ctx(owner.actor_id, via="scheduler"), t["id"], {"notes": "x" * 30})
    assert tasks.get(conn, owner, t["id"])["interventions"] == 4
    week = weekly_packet.current_week()
    s, u = (x.isoformat(timespec="seconds") for x in weekly_packet.bounds(week))
    m = business.owner_minutes(conn, s, u)
    assert m["interventions"] == 4 and m["by_kind"] == {"dm": 1, "edit": 1, "return": 1, "approval": 1}
    assert m["minutes"] == 2 + 2 + 5 + 1


def test_an_agents_unsolicited_message_to_the_owner_gets_a_nudge_not_a_block(conn, owner, company):
    se, ceo = company["se"], company["ceo"]
    out = chat.send_dm(conn, se, owner.actor_id, "Ahoj Davide, mám otázku k deployi")
    assert out["id"] and "platform_note" in out and "CEO" in out["platform_note"]
    assert business.nudges(conn, se.actor_id) and "1x" in business.nudges(conn, se.actor_id)[0]
    # the CEO may; replying after the owner wrote is fine
    assert "platform_note" not in chat.send_dm(conn, ceo, owner.actor_id, "Shrnutí dne")
    cfo = company["cfo"]
    chat.send_dm(conn, owner, cfo.actor_id, "Kolik stojí Voyage?")
    assert "platform_note" not in chat.send_dm(conn, cfo, owner.actor_id, "Zhruba $3 měsíčně")
    assert business.nudges(conn, cfo.actor_id) == []


def test_a_chat_task_for_another_agent_says_the_ceo_takes_company_requests(conn, owner, company, monkeypatch):
    from pos import workers

    monkeypatch.setattr(workers, "reply_path", lambda c, t: {"kind": "pool", "name": "pool/x"})
    chat.send_dm(conn, owner, company["cto"].actor_id, "Chci nový web pro firmu")
    t = conn.execute("SELECT notes FROM tasks WHERE assignee_id = ? AND topic = 'chat'",
                     (company["cto"].actor_id,)).fetchone()
    assert "company-level request" in t["notes"] and "CEO" in t["notes"]
    members = {m["name"]: m for m in chat.members_overview(conn)}
    assert members["CEO"]["is_ceo"] and not members["CTO"]["is_ceo"]


# ------------------------------------------------------------------ owner-assigned tasks and the step cap

def test_owner_requests_are_recognised(conn, owner, company):
    ha = company["cto"]
    direct = tasks.create(conn, owner, {"title": "Oprav světlo", "assignee": "CTO"})
    msg = chat.send_dm(conn, owner, ha.actor_id, "ráno nesvítí světlo")
    cited = tasks.create(conn, company["cos"], {"title": "Ranní světlo", "assignee": "CTO",
                                                "notes": f"### Odkud\nDM s Ownerem (zpráva {msg['id']})."})
    split = tasks.create(conn, ha, {"title": "Krok 2", "assignee": "CTO", "notes": f"Z úkolu {direct['ref']}."})
    routine = tasks.create(conn, owner, {"title": "Report", "source": "scheduler", "assignee": "CTO"})
    row = lambda t: conn.execute("SELECT * FROM tasks WHERE id = ?", (t["id"],)).fetchone()  # noqa: E731
    assert business.owner_request(conn, row(direct)) and business.owner_request(conn, row(cited))
    assert business.owner_request(conn, row(split)) and not business.owner_request(conn, row(routine))


def test_step_cap_per_agent_and_higher_for_the_owners_tasks():
    from pos_worker.loop import OWNER_STEP_FACTOR, step_cap

    assert step_cap({}, {}, 40) == 40
    assert step_cap({"max_steps": 80}, {}, 40) == 80
    assert step_cap({"max_steps": 80, "max_steps_owner": 160}, {"owner_request": True}, 40) == 160
    assert step_cap({"max_steps": 30}, {"owner_request": True}, 40) == 30 * OWNER_STEP_FACTOR
    assert step_cap({}, {"owner_request": True}, 0) == 0  # no cap stays no cap


def test_agent_files_give_the_ha_specialist_and_the_engineer_higher_caps():
    ha = agents_code.worker_profile("Home Assistant Specialist")
    se = agents_code.worker_profile("Software Engineer")
    assert ha["max_steps"] >= agents_code.MIN_STEPS and ha["max_steps_owner"] > ha["max_steps"]
    assert se["max_steps"] >= agents_code.MIN_STEPS and se["max_steps_owner"] > se["max_steps"]
    for spec in agents_code.specs():
        # HR may hire on any allowed model (e.g. Sonnet for low-budget agents)
        assert spec.get("model") in hiring.MODELS and spec.get("effort") == hiring.DEFAULT_EFFORT, spec["name"]
        assert spec.get("engine") == "claude", spec["name"]


# ------------------------------------------------------------------ escalation dedup

def test_one_invoice_is_one_escalation(conn, owner, company):
    cfo, ceo = company["cfo"], company["ceo"]
    mail = tasks.create(conn, Ctx(owner.actor_id, via="events:knowlage"),
                        {"title": "Zálohová faktura TKP-N-0088", "source": "event:gmail", "assignee": "CFO"})
    up = tasks.create(conn, cfo, {"title": "Digest: zálohová faktura TKProfi", "assignee": "CEO", "topic": "digest",
                                  "notes": f"**Zdroj:** {mail['ref']} (e-mail od info@tkprofi.cz)"})
    assert not up.get("deduplicated")
    again = tasks.create(conn, ceo, {"title": "Digest: potvrdit objednávku klimatizace", "assignee": "Chief of Staff",
                                     "topic": "digest", "notes": f"### Odkud\n{up['ref']} (eskalace CFO) ← {mail['ref']}"})
    assert again["deduplicated"] and again["id"] == up["id"] and "handoff_task" in again["note"]
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2 + conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE id < ?", (mail["id"],)).fetchone()[0]
    # a legitimate step of a different item is created as usual
    other = tasks.create(conn, ceo, {"title": "Plán týdne", "assignee": "CTO", "notes": "Nový úkol bez odkazu."})
    assert not other.get("deduplicated")
    # the owner's tickets (ask_owner) keep their own dedup
    ticket = tasks.create(conn, cfo, {"title": "Schválit platbu", "assignee": "me", "notes": f"K {mail['ref']}",
                                      "source": "ask_owner"})
    assert not ticket.get("deduplicated")


# ------------------------------------------------------------------ reviews

def test_the_owners_reviews_go_to_the_ceo_who_leaves_him_what_needs_him(conn, owner, company):
    cto, ceo = company["cto"], company["ceo"]
    t = tasks.create(conn, owner, {"title": "Porovnej hosting", "assignee": "CTO"})
    tasks.claim(conn, cto, t["id"])
    t = tasks.complete(conn, cto, t["id"], "hotovo")
    assert t["status"] == "review" and t["reviewer_id"] == ceo.actor_id
    # the CEO hands it to the owner: it stays with him (no bounce back to the CEO)
    tasks.request_review(conn, ceo, t["id"], "David", "Tohle musíš rozhodnout ty: smlouva na 3 roky")
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    business.review_sla(conn, now=later)
    assert tasks.get(conn, owner, t["id"])["reviewer_id"] == owner.actor_id


def test_review_sla_moves_old_reviews_up(conn, owner, company):
    se, cto, ceo = company["se"], company["cto"], company["ceo"]
    t = tasks.create(conn, cto, {"title": "Oprav test", "assignee": "Software Engineer"})
    tasks.claim(conn, se, t["id"])
    t = tasks.complete(conn, se, t["id"], "hotovo")
    assert t["reviewer_id"] == cto.actor_id
    assert business.review_sla(conn) == {}  # not yet 24 h
    out = business.review_sla(conn, now=datetime.now(timezone.utc) + timedelta(hours=25))
    assert out["moved"] == [f"{t['ref']}→CEO"]
    assert tasks.get(conn, owner, t["id"])["reviewer_id"] == ceo.actor_id
    # an older result still waiting for the owner (before the CEO triage existed) goes to the CEO
    old = tasks.create(conn, owner, {"title": "Starý výsledek", "assignee": "CFO"})
    conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ? WHERE id = ?", (owner.actor_id, old["id"]))
    out = business.review_sla(conn, now=datetime.now(timezone.utc) + timedelta(hours=25))
    assert f"{old['ref']}→CEO" in out["moved"]
    # the CEO's own reviews: a reminder (its digest named it), not an escalation to the owner
    assert t["id"] in business._reminded(conn, ceo.actor_id)
    # ... and one reminder only: a later digest does not name it again
    out = business.review_sla(conn, now=datetime.now(timezone.utc) + timedelta(hours=60))
    assert t["ref"] not in out.get("reminded", []) and f"{t['ref']}→" not in str(out.get("moved"))


# ------------------------------------------------------------------ idle agents

def test_idle_agents_are_flagged_to_the_ceo_once_a_week(conn, owner, company):
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(timespec="seconds")
    conn.execute("UPDATE actors SET created_at = ? WHERE kind = 'agent'", (old,))
    tasks.create(conn, owner, {"title": "Práce pro CFO", "assignee": "CFO"})
    conn.commit()
    idle = {a["name"] for a in business.idle_agents(conn)}
    assert "CFO" not in idle and "CEO" not in idle and "Head of Growth" in idle
    out = business.idle_agents_job(conn)
    assert out["task"] and business.idle_agents_job(conn).get("skipped")
    t = tasks.get(conn, owner, tasks.parse_id(out["task"]))
    assert t["assignee_id"] == company["ceo"].actor_id and "Head of Growth" in t["notes"]


# ------------------------------------------------------------------ one cost ledger

def test_engine_usage_is_the_one_ledger(conn, owner, company):
    from pos import engines

    t = tasks.create(conn, owner, {"title": "Odpověz zákazníkovi", "source": "event:gmail", "assignee": "CFO"})
    run = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, engine) VALUES "
                       "(?, ?, 'task', 'ok', ?, 'claude')", (company["cfo"].actor_id, t["id"], now_iso())).lastrowid
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run,)).fetchone()
    triage = json.dumps({"type": "result", "subtype": "triage", "total_cost_usd": 0.02, "usage": {}})
    main = json.dumps({"type": "result", "total_cost_usd": 0.5, "usage": {"input_tokens": 10, "output_tokens": 5}})
    engines.record_claude(conn, row, triage, update_run=False)
    engines.record_claude(conn, row, main)
    assert conn.execute("SELECT cost_usd FROM runs WHERE id = ?", (run,)).fetchone()[0] == pytest.approx(0.52)
    # an old run recorded before runs.cost_usd existed is reconciled from the ledger
    conn.execute("UPDATE runs SET cost_usd = NULL WHERE id = ?", (run,))
    assert business.reconcile_ledger(conn) == {"fixed": 1}
    assert conn.execute("SELECT cost_usd FROM runs WHERE id = ?", (run,)).fetchone()[0] == pytest.approx(0.52)
    assert business.reconcile_ledger(conn) == {"fixed": 0}
    tasks.update(conn, owner, t["id"], {"status": "done"})
    week = weekly_packet.current_week()
    s, u = (x.isoformat(timespec="seconds") for x in weekly_packet.bounds(week))
    split = business.cost_split(conn, s, u)
    assert split["business_usd"] == pytest.approx(0.52) and split["business_outcomes"] == 1
    assert split["usd_per_business_outcome"] == pytest.approx(0.52)


# ------------------------------------------------------------------ business KPIs

SEARCH_HITS = """<external source="mailbox" trust="untrusted" ref="firma.mb_1">
### 1. `firma.mb_1:c0` · chunk · relevance 0.8
Zálohová faktura TKP-N-0088 (2026-09-26) · sekce `firma.mb_1:s0` · https://mail.google.com/x

Od: "TKProfi" <info@tkprofi.cz>
Posíláme zálohovou fakturu, k úhradě 42 350,00 Kč, splatnost 3. 10.
</external>

<external source="gdrive" trust="untrusted" ref="firma.gd_2">
### 2. `firma.gd_2:c0` · chunk · relevance 0.8
Vydaná faktura - 0034202634.pdf (2026-09-25) · sekce `firma.gd_2:s0` · https://drive.google.com/y

Faktura - daňový doklad Dodavatel Obseum s.r.o. Odběratel O2 IT Services s.r.o. 15 600,00 3 276,00 Celkem 18 876,00 Kč
</external>

<external source="mailbox" trust="untrusted" ref="firma.mb_3">
### 3. `firma.mb_3:c0` · chunk · relevance 0.5
Newsletter (2026-09-24) · sekce `firma.mb_3:s0` · https://mail.google.com/z

Od: news@shop.cz
Akce týdne!
</external>"""


def test_invoices_are_read_from_knowlage_search_hits(monkeypatch):
    items = business.parse_invoices(SEARCH_HITS)
    assert [(i["direction"], i["amount"], i["currency"]) for i in items] == [
        ("received", 42350.0, "CZK"), ("sent", 18876.0, "CZK")]
    monkeypatch.setattr(kb_files, "configured", lambda: True)
    monkeypatch.setattr(business, "_kb_search", lambda q, a, b, k=40: SEARCH_HITS)
    out = business.invoices("2026-09-21T00:00:00+00:00", "2026-09-28T00:00:00+00:00")
    assert out["sent"] == 1 and out["received"] == 1
    assert out["totals"] == {"sent": {"CZK": 18876.0}, "received": {"CZK": 42350.0}}


def test_the_packet_has_business_numbers_and_skips_demo_tasks(conn, owner, company):
    real = tasks.create(conn, owner, {"title": "Odeslat nabídku", "assignee": "Head of Growth", "status": "next"})
    demo = tasks.create(conn, owner, {"title": "Send the signed contract to Acme", "topic": "acme", "status": "next"})
    for t in (real, demo):
        tasks.update(conn, owner, t["id"], {"status": "done"})
    tasks.create(conn, owner, {"title": "Zákazník se ptá", "source": "event:gmail",
                               "assignee": "Head of Customer Success"})
    p = weekly_packet.build(conn, outside=False, now=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert p["tasks"]["done"] == 1 and "Acme" not in json.dumps(p["tasks"]["highlights"])
    b = p["business"]
    assert b["customer_threads"]["open"] == 1 and b["pipeline"]["done"] == 1
    assert "Majitel" in b["owner_time"]["line"] and "owner_minutes" in p["kpis"]
    assert "business_outcomes" in p["kpis"] and b["invoices"]["available"] is False


# ------------------------------------------------------------------ the weekly report always gets published

def test_a_draft_nobody_published_is_published_from_its_numbers(conn, owner, company):
    old = weekly_packet.current_week()
    for _ in range(3):  # ended more than a week ago: too late to write it, published from its numbers
        old = weekly_packet.previous_week(old)
    weekly.packet_for(conn, old, outside=False)  # a draft made by reading the packet, no task
    conn.commit()
    out = weekly.publish_overdue(conn)
    assert out["published"] == [old]
    row = weekly.get_report(conn, old)
    assert row["status"] == "published" and row["published_at"] and "## Co se stalo" in row["narrative"]
    assert row["headline"].startswith(f"Týden {old}")
    assert "published" not in weekly.publish_overdue(conn)
    # the current week: only when the Chief of Staff's task is over 20 h old
    out = weekly.weekly_job(conn)
    week = out.get("week") or weekly_packet.current_week()
    assert "published" not in weekly.publish_overdue(conn)
    later = datetime.now(timezone.utc) + timedelta(hours=21)
    assert week in weekly.publish_overdue(conn, now=later)["published"]


def test_a_missed_week_is_caught_up_by_the_chief_of_staff(conn, owner, company):
    """W39: a draft with no author and no task (the Friday job missed it) gets the Chief of Staff's
    task with a fresh packet, at startup or before the next job; it is not published from numbers."""
    past = weekly_packet.previous_week(weekly_packet.current_week())
    weekly.packet_for(conn, past, outside=False)  # the stuck draft: numbers only, nobody asked to write it
    conn.commit()
    _, end = weekly_packet.bounds(past)
    out = weekly.catch_up(conn, now=end + timedelta(hours=2))
    assert [s.split(":")[0] for s in out["started"]] == [past]
    row = weekly.get_report(conn, past)
    assert row["status"] == "draft" and row["task_id"]
    t = tasks.get(conn, owner, row["task_id"])
    assert t["assignee_name"] == "Chief of Staff" and past in t["title"]
    assert weekly.catch_up(conn, now=end + timedelta(hours=3)) == {}  # once
    # the Chief of Staff gets its 20 h; only then the numbers are published
    assert "published" not in weekly.publish_overdue(conn, now=end + timedelta(hours=3))
    # a week whose Friday slot has not come yet is not caught up
    assert weekly.catch_up(conn, now=weekly._slot(weekly_packet.current_week()) - timedelta(hours=1)) == {}


# ------------------------------------------------------------------ knowledge for agents

class FakeKB:
    def __init__(self):
        self.calls = []

    def handler(self, request: httpx.Request):
        self.calls.append(request)
        body = json.loads(request.content)
        assert body["params"]["name"] == "search"
        text = "<external source=\"mailbox\">\n### 1. `firma.mb_1:c0` · chunk\nFaktura od TKProfi\n</external>"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": {"content": [{"type": "text", "text": text}], "isError": False}})


def test_the_knowledge_tool_needs_its_grant_and_hides_the_key(conn, owner, company, monkeypatch):
    fake = FakeKB()
    monkeypatch.setenv("POS_KNOWLAGE_URL", "http://knowlage.test")
    monkeypatch.setenv("POS_KNOWLAGE_API_KEY", "kb-service-key")
    monkeypatch.setattr(kb_files, "_transport", httpx.MockTransport(fake.handler))
    assert "knowledge" in mcp_server.tool_names()
    growth = company["growth"].actor_id
    access.seed(conn)
    assert not mcp_server.may_use(conn, growth, "knowledge")
    access._insert_grant(conn, growth, "tool:knowledge", owner.actor_id, "owner", "test")
    access.refresh_cache(conn, growth)
    assert mcp_server.may_use(conn, growth, "knowledge")
    out = knowledge_tool.search("faktury tento týden", effort=9, k=100)
    assert out["effort"] == knowledge_tool.MAX_EFFORT and "TKProfi" in out["results"]
    assert "kb-service-key" not in json.dumps(out)
    sent = json.loads(fake.calls[0].content)["params"]["arguments"]
    assert sent["k"] == 20 and fake.calls[0].headers["authorization"] == "Bearer kb-service-key"
    monkeypatch.delenv("POS_KNOWLAGE_API_KEY")
    with pytest.raises(knowledge_tool.Unavailable):
        knowledge_tool.search("x")


def test_agent_files_grant_single_tools_once(conn, owner, tmp_path):
    cfo = _agent(conn, owner, tmp_path, "CFO", "cfo")
    access.seed(conn)
    row = actors.get(conn, cfo.actor_id)
    spec = {"name": "CFO", "grants": ["tool:knowledge", "access:manage", "cred:bank", "tasks:write"]}
    assert agents_code._grants_from_file(conn, Ctx(owner.actor_id), row, spec) == ["tool:knowledge"]
    assert mcp_server.may_use(conn, cfo.actor_id, "knowledge")
    gid = conn.execute("SELECT id FROM access_grants WHERE agent_id = ? AND capability = 'tool:knowledge'",
                       (cfo.actor_id,)).fetchone()["id"]
    conn.execute("UPDATE access_grants SET ended_at = ?, end_kind = 'revoked' WHERE id = ?", (now_iso(), gid))
    assert agents_code._grants_from_file(conn, Ctx(owner.actor_id), row, spec) == []  # a revoke stays
    listed = {s["name"]: s.get("grants") for s in agents_code.specs()}
    for name in ("CEO", "Chief of Staff", "CFO", "Head of Growth", "Head of Customer Success", "CTO",
                 "Knowlage Specialist", "Nexus Specialist", "Home Assistant Specialist", "SRE"):
        assert "tool:knowledge" in (listed[name] or []), name


# ------------------------------------------------------------------ business routing

def test_leads_go_to_growth_and_company_github_to_the_cto(conn, owner, company):
    routing.seed_defaults(conn)
    made = routing.ensure_business_rules(conn)
    assert routing.LEADS_RULE in made and routing.TRIAGE_RULE in made
    assert routing.ensure_business_rules(conn) == []
    ctx = Ctx(owner.actor_id, via="events:knowlage")
    lead = routing.ingest(conn, ctx, {"source": "gmail", "kind": "email", "title": "Poptávka: e-shop na míru",
                                      "body": "Dobrý den, máme zájem o spolupráci.", "ref": "m1"})
    support = routing.ingest(conn, ctx, {"source": "gmail", "kind": "email", "title": "Nefunguje přihlášení",
                                         "body": "Nejde se mi přihlásit.", "ref": "m2"})
    assert lead["assignee"] == "Head of Growth" and support["assignee"] == "Head of Customer Success"
    issue = routing.ingest(conn, ctx, {"source": "github", "kind": "issue", "title": "ObseumEU/tesco#4: bug",
                                       "ref": "ObseumEU/tesco#4", "meta": {"repo": "ObseumEU/tesco", "labels": []}})
    assert issue["assignee"] == "CTO"
    elsewhere = routing.ingest(conn, ctx, {"source": "github", "kind": "issue", "title": "someone/else#1: x",
                                           "ref": "someone/else#1", "meta": {"repo": "someone/else", "labels": []}})
    assert elsewhere["assignee"] is None
    # pull requests from the webhook: ours (agent/ branches) are not triaged
    pr = {"number": 7, "title": "Add feature", "body": "", "html_url": "u", "user": {"login": "petr"},
          "head": {"ref": "feature/x"}, "labels": []}
    assert routing.github_events("pull_request", {"action": "opened", "pull_request": pr,
                                                  "repository": {"full_name": "ObseumEU/tesco"}})[0]["kind"] == "pull_request"
    ours = {**pr, "head": {"ref": "agent/dev"}}
    assert routing.github_events("pull_request", {"action": "opened", "pull_request": ours,
                                                  "repository": {"full_name": "ObseumEU/tesco"}}) == []


def test_github_poll_turns_new_issues_and_prs_into_triage_tasks(conn, owner, company, monkeypatch):
    routing.seed_defaults(conn)
    routing.ensure_business_rules(conn)
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def fake(url, params):
        if url.endswith("/orgs/ObseumEU/repos"):
            return [{"full_name": "ObseumEU/tesco", "pushed_at": recent}, {"full_name": "ObseumEU/old",
                                                                          "pushed_at": "2020-01-01T00:00:00Z"}]
        if url.endswith("/repos/ObseumEU/tesco/issues"):
            return [{"number": 5, "title": "Crash on login", "created_at": recent, "user": {"login": "zak"},
                     "labels": [], "html_url": "i5"},
                    {"number": 6, "title": "Fix", "created_at": recent, "user": {"login": "bot"}, "labels": [],
                     "pull_request": {}, "html_url": "p6"},
                    {"number": 7, "title": "Deps", "created_at": recent, "user": {"login": "d", "type": "Bot"},
                     "labels": [], "pull_request": {}}]
        if url.endswith("/pulls/6"):
            return {"head": {"ref": "agent/dev"}, "user": {"login": "bot"}}
        raise AssertionError(url)

    monkeypatch.setattr(routing, "github_get", fake)
    out = routing.github_poll(conn, now=now)
    assert len(out["tasks"]) == 1
    t = conn.execute("SELECT * FROM tasks WHERE title LIKE 'ObseumEU/tesco#5%'").fetchone()
    assert t["assignee_name"] == "CTO" and t["topic"] == "triage"
    assert "tasks" not in routing.github_poll(conn, now=now)  # the same issue again: no second task


# ------------------------------------------------------------------ the worker's view

def test_worker_me_has_nudges_and_the_task_says_whether_the_owner_asked(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from pos.config import Settings
    from pos.main import create_app

    settings = Settings(data_dir=tmp_path, scheduler=False)
    with TestClient(create_app(settings)) as client:
        c = connect(settings.db_path)
        o = Ctx(actors.owner_id(c), via="api")
        a = _agent(c, o, tmp_path, "Head of Growth", "growth")
        key = agents.rotate_key(c, o, a.actor_id)
        chat.send_dm(c, a, o.actor_id, "Ahoj, mám nápad")
        t = tasks.create(c, o, {"title": "Najdi 3 leady", "assignee": "Head of Growth"})
        c.commit()
        h = {"Authorization": f"Bearer {key}"}
        me = client.get("/api/worker/me", headers=h).json()
        assert me["nudges"] and "CEO" in me["nudges"][0]
        work = client.get("/api/worker/next?wait=0", headers=h).json()
        assert work["task"]["id"] == t["id"] and work["task"]["owner_request"] is True
        c.close()


# ------------------------------------------------------------------ the one-off production change

def test_rollout_switches_models_scales_budgets_and_closes_gmail(conn, owner, tmp_path):
    from pos import biz_rollout

    cfo = _agent(conn, owner, tmp_path, "CFO", "cfo")
    conn.execute("UPDATE actors SET engine = 'claude', model = 'claude-sonnet-5' WHERE id = ?", (cfo.actor_id,))
    access.seed(conn)
    access._insert_budget(conn, cfo.actor_id, "usd_day", 1.0, owner.actor_id, "platform", "old")
    access._insert_budget(conn, cfo.actor_id, "usd_month", 99.0, owner.actor_id, "access_manager", "raised")
    while conn.execute("SELECT COALESCE(MAX(id), 0) FROM tasks").fetchone()[0] < 16:
        tasks.create(conn, owner, {"title": "Connect Gmail through the knowlage ingest (step 4)", "status": "waiting"})
    conn.commit()
    p = biz_rollout.plan(conn)
    assert "CFO" in [m["name"] for m in p["models"]] and p["tasks"][0]["ref"] == "T-016"
    # the spike floor's default (pos.access autonomy) is above the rollout's floor already, or it is raised
    assert bool(p["settings"]) == (access.DEFAULT_SETTINGS["spike_floor_usd"] < biz_rollout.SPIKE_FLOOR_USD)
    out = biz_rollout.apply(conn)
    a = actors.get(conn, cfo.actor_id)
    assert (a["engine"], a["model"]) == ("claude", "claude-opus-5-5")
    active = {r["metric"]: r["amount"] for r in conn.execute(
        "SELECT metric, amount FROM access_budgets WHERE agent_id = ? AND ended_at IS NULL", (cfo.actor_id,))}
    spec = agents_code.spec_of("CFO")["budget"]
    assert active["usd_day"] == spec["usd_day"] and active["usd_month"] == 99.0  # a raise is never lowered
    assert access.settings(conn)["spike_floor_usd"] >= biz_rollout.SPIKE_FLOOR_USD
    assert tasks.get(conn, owner, 16)["status"] == "done"
    again = biz_rollout.plan(conn)
    assert not again["models"] and not again["budgets"] and not again["settings"] and not again["tasks"]
    assert out["ledger"] == {"fixed": 0}


def test_rollout_script_applies_in_a_fresh_interpreter(conn, owner, tmp_path):
    """`python -m pos.biz_rollout --apply` imports what it needs itself (it crashed with KeyError: 'actor')."""
    import os
    import subprocess
    from pathlib import Path

    cfo = _agent(conn, owner, tmp_path, "CFO", "cfo")
    conn.execute("UPDATE actors SET engine = 'codex', model = 'gpt-5' WHERE id = ?", (cfo.actor_id,))
    conn.commit()
    db = conn.execute("PRAGMA database_list").fetchone()["file"]
    src = str(Path(__file__).resolve().parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    r = subprocess.run([sys.executable, "-m", "pos.biz_rollout", "--apply", "--db", db],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    a = conn.execute("SELECT engine, model FROM actors WHERE id = ?", (cfo.actor_id,)).fetchone()
    assert (a["engine"], a["model"]) == ("claude", "claude-opus-5-5")


def test_review_reminders_come_from_the_system_not_the_owner(conn, owner, company):
    """T-194/T-195: the SLA reminder was a DM from "Owner" with no priority, so the chat watch
    counted it as the owner's unanswered message."""
    from pos import workers

    cto, ceo = company["cto"], company["ceo"]
    t = tasks.create(conn, owner, {"title": "Porovnej hosting", "assignee": "CTO"})
    tasks.claim(conn, cto, t["id"])
    tasks.complete(conn, cto, t["id"], "hotovo")  # the CEO triages it
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert t["ref"] in business.review_sla(conn, now=later)["reminded"]
    m = conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT 1").fetchone()
    assert "waits for your review" in m["body"]
    assert m["author_id"] == actors.system_id(conn) and m["priority"] == "fyi"  # PersonalOS, pos.notices
    # whatever the platform sends under the owner's name is no message of his to answer
    sys_msg = chat.send_dm(conn, Ctx(owner.actor_id, via="system"), ceo.actor_id, "Připomínka", system=True)
    real = chat.send_dm(conn, owner, ceo.actor_id, "Jak to vypadá?")
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    until = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
    ids = [m["id"] for m, _ in workers._owner_messages(conn, since, until)]
    assert real["id"] in ids and sys_msg["id"] not in ids


def test_the_same_item_without_a_ref_is_one_escalation_passed_up(conn, owner, company):
    """T-147 -> T-148 -> T-149: one invoice, three tasks. The same document code (or mail link) is the
    same item even without a T-ref, and whoever holds the escalation passes that task up."""
    cfo, ceo, cos = company["cfo"], company["ceo"], company["cos"]
    up = tasks.create(conn, cfo, {"title": "Digest: zálohová faktura TKP-N-0088", "assignee": "CEO", "topic": "digest",
                                  "notes": "Neznámý dodavatel, potřeba rozhodnutí."})
    assert not up.get("deduplicated")
    again = tasks.create(conn, ceo, {"title": "Digest: potvrdit objednávku (nabídka TKP-N-0088)",
                                     "assignee": "Chief of Staff", "topic": "digest", "notes": "Pro Davida."})
    assert again["deduplicated"] and again["id"] == up["id"]
    t = tasks.get(conn, owner, up["id"])
    assert t["assignee_id"] == cos.actor_id and t["status"] == "next"  # passed up, not copied
    assert conn.execute("SELECT 1 FROM handoffs WHERE task_id = ? AND from_actor = ? AND to_actor = ?",
                        (up["id"], ceo.actor_id, cos.actor_id)).fetchone()
    other = tasks.create(conn, cfo, {"title": "Digest: faktura FAK-2026-0107", "assignee": "CEO", "topic": "digest"})
    assert not other.get("deduplicated")
    # older than 7 days: a new escalation
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (old, other["id"]))
    fresh = tasks.create(conn, cfo, {"title": "Digest: znovu FAK-2026-0107", "assignee": "CEO", "topic": "digest"})
    assert not fresh.get("deduplicated")
