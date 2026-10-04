"""The work-flow engine fixes (prod 2026-09-25..10-04): reviews are work, routines keep firing, the
review SLA is one daily digest, the picker respects do_date and backs off, the access loop, flood and
loop guards, the escalation dedup, the owner's comments wake work, no tasks for the owner from agents,
the watch routines in code, value fields on tasks, and the one-off production fix."""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from pos import (actors, agents, api_worker, business, chat, comments, head_alerts, integrations, prodfix_workflow,
                 review_work, schedules, tasks)
from pos.access import service as access
from pos.core import Ctx, now_iso
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "w.db")
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


def _agent(conn, owner, tmp_path, name, role, lead=None, team=None, perms=None) -> Ctx:
    perms = perms or ["tasks:read", "tasks:write", "tasks:claim", "tasks:review", "messages:send",
                      "approvals:request"]
    found = actors.find_by_name(conn, name)
    if found is not None:  # a built-in member (the COO, the Access manager)
        aid = found["id"]
        agents.set_permissions(conn, owner, aid, perms)
    else:
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                  permissions=perms)["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ?, team = ? WHERE id = ?",
                 (role, lead.actor_id if lead else None, team, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


@pytest.fixture
def co(conn, owner, tmp_path):
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo", team="leadership")
    coo = _agent(conn, owner, tmp_path, "COO", "project_manager", ceo, team="leadership")
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo, team="engineering")
    se = _agent(conn, owner, tmp_path, "Software Engineer", "developer", cto, team="engineering")
    qa = _agent(conn, owner, tmp_path, "QA Reviewer", "qa", cto, team="engineering")
    am = _agent(conn, owner, tmp_path, "Access manager", "access_manager", ceo, team="leadership")
    growth = _agent(conn, owner, tmp_path, "Head of Growth", "growth", ceo, team="growth")
    klead = _agent(conn, owner, tmp_path, "Kniha Lead", "product_lead", ceo, team="kniha")
    kdev = _agent(conn, owner, tmp_path, "Kniha Developer", "developer", klead, team="kniha")
    ksales = _agent(conn, owner, tmp_path, "Kniha Growth & Sales", "growth_sales", klead, team="kniha")
    return {"ceo": ceo, "coo": coo, "cto": cto, "se": se, "qa": qa, "am": am, "growth": growth, "klead": klead,
            "kdev": kdev, "ksales": ksales}


def _item(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE source = ? AND status != 'done'", (f"review:{task_id}",)).fetchone()


def _handed_in(conn, creator: Ctx, worker: Ctx, title: str, note: str = "Hotovo, viz poznámka.", **kw) -> dict:
    t = tasks.create(conn, creator, {"title": title, "assignee": {"type": "agent", "id": worker.actor_id}, **kw})
    tasks.claim(conn, worker, t["id"])
    return tasks.complete(conn, worker, t["id"], note)


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


# ------------------------------------------------------------------ 1. reviews are real work

def test_a_review_request_is_a_task_the_reviewers_worker_picks_up(conn, owner, co):
    cto, se, qa = co["cto"], co["se"], co["qa"]
    t = _handed_in(conn, cto, se, "Oprav export", "commit 1234abcd, pytest 3 passed")  # code: to the QA Reviewer
    assert t["status"] == "review" and t["reviewer_id"] == qa.actor_id
    item = _item(conn, t["id"])
    assert item["assignee_id"] == qa.actor_id and item["status"] == "next" and item["title"].startswith("Review: ")
    # the QA Reviewer's worker gets it from the queue (a DM alone started no run)
    work = api_worker._next_work(conn, Ctx(qa.actor_id, via="worker"))
    assert work["task"]["id"] == item["id"]
    # reviewing the result closes the item; completing the item itself needs no review of a review
    tasks.claim(conn, qa, item["id"])
    tasks.review(conn, qa, t["id"], True, "ok")
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (item["id"],)).fetchone()[0] == "done"
    assert tasks.complete(conn, qa, item["id"], "accepted")["status"] == "done"


def test_a_review_item_follows_its_reviewer_and_closes_on_return_or_auto_accept(conn, owner, co):
    cto, se, qa, ceo = co["cto"], co["se"], co["qa"], co["ceo"]
    t = tasks.create(conn, cto, {"title": "Plán migrace", "assignee": {"type": "agent", "id": se.actor_id}})
    tasks.claim(conn, se, t["id"])
    tasks.request_review(conn, se, t["id"], "QA Reviewer", "prosím zkontroluj")
    assert _item(conn, t["id"])["assignee_id"] == qa.actor_id
    tasks.hand_review(conn, business.system_ctx(conn), t["id"], ceo.actor_id, "moved")
    item = _item(conn, t["id"])
    assert item["assignee_id"] == ceo.actor_id  # the same item, moved: never two
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source = ?", (f"review:{t['id']}",)).fetchone()[0] == 1
    tasks.review(conn, ceo, t["id"], False, "doplň rollback")
    assert _item(conn, t["id"]) is None
    # a stale item (its result left review behind the platform's back) is closed, not served
    tasks.claim(conn, se, t["id"])
    tasks.request_review(conn, se, t["id"], "QA Reviewer")
    stale = _item(conn, t["id"])
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (t["id"],))
    assert api_worker._next_work(conn, Ctx(qa.actor_id, via="worker")).get("task") is None
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (stale["id"],)).fetchone()[0] == "done"
    # the hourly sync creates what is missing (a result moved by a raw UPDATE)
    other = _handed_in(conn, cto, se, "Návrh API", "návrh je v poznámce")
    conn.execute("UPDATE tasks SET source = 'x' WHERE source = ?", (f"review:{other['id']}",))
    assert review_work.sync(conn)["created"]
    assert _item(conn, other["id"]) is not None


# ------------------------------------------------------------------ 2. routines

def test_a_routine_fires_while_its_last_result_waits_for_review_and_tracked_findings_close(conn, owner, co):
    ceo, coo = co["ceo"], co["coo"]
    s = schedules.create(conn, owner, {"name": "Týdenní plán", "schedule": "daily 07:00", "visibility": "team",
                                       "assignee": {"type": "agent", "id": coo.actor_id}})
    first = schedules.fire(conn, s["id"])
    tid = tasks.parse_id(first["task"])
    conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ? WHERE id = ?", (ceo.actor_id, tid))
    again = schedules.fire(conn, s["id"])
    assert "task" in again and again["task"] != first["task"]  # review counts as closed for the routine
    # a routine check whose finding already has its task: accepted, not left for the CEO
    tracker = tasks.create(conn, ceo, {"title": "Swap nad prahem", "assignee": "CTO"})
    t = tasks.create(conn, owner, {"title": "Denní kontrola kapacity", "assignee": {"type": "agent",
                                                                                     "id": coo.actor_id},
                                   "source": f"schedule:{s['id']}"})
    tasks.claim(conn, coo, t["id"])
    out = tasks.complete(conn, coo, t["id"], f"Nad prahem: swap 81 % -> {tracker['ref']} (CTO). Disk v normě.")
    assert out["status"] == "done"


# ------------------------------------------------------------------ 3. the review SLA

def test_the_sla_sends_one_digest_a_day_escalates_after_24h_and_never_as_the_owner(conn, owner, co):
    cto, se, ceo, qa = co["cto"], co["se"], co["ceo"], co["qa"]
    mine = [tasks.create(conn, owner, {"title": f"Výsledek {i}", "assignee": "Head of Growth"}) for i in range(35)]
    for t in mine:  # 35 results for the CEO (more than the old LIMIT 30)
        conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ?, updated_at = ? WHERE id = ?",
                     (ceo.actor_id, _ago(40), t["id"]))
    code = _handed_in(conn, cto, se, "Oprav worker", "commit 1234abcd, pytest passed")
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_ago(40), code["id"]))
    conn.execute("UPDATE tasks SET created_at = ? WHERE source = ?", (_ago(40), f"review:{code['id']}"))
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
    out = business.review_sla(conn)
    # the QA row escalates although the CEO has 35 older ones
    assert f"{code['ref']}→CTO" in out["moved"]
    assert _item(conn, code["id"])["assignee_id"] == cto.actor_id
    # one digest for the CEO (not 35 reminders), one message for the CTO
    to_ceo = conn.execute("""SELECT m.* FROM chat_messages m JOIN chat_inbox i ON i.message_id = m.id
                             WHERE i.actor_id = ? AND m.id > ?""", (ceo.actor_id, before)).fetchall()
    assert len(to_ceo) == 1 and "Review digest: 35" in to_ceo[0]["body"]
    assert all(m["author_id"] != owner.actor_id for m in conn.execute(
        "SELECT author_id FROM chat_messages WHERE id > ?", (before,)))
    # an hour later: nothing new for the CEO (one digest a day)
    business.review_sla(conn)
    assert len(conn.execute("""SELECT 1 FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id
                               WHERE i.actor_id = ? AND m.id > ?""", (ceo.actor_id, before)).fetchall()) == 1
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'review_reminder'").fetchone()


# ------------------------------------------------------------------ 4. the picker

def test_the_picker_waits_for_do_date_and_an_ok_run_backs_off(conn, owner, co):
    ks = co["ksales"]
    later = (date.today() + timedelta(days=5)).isoformat()
    planned = tasks.create(conn, co["klead"], {"title": "Follow-up DigiTry", "do_date": later,
                                               "assignee": {"type": "agent", "id": ks.actor_id}})
    w = Ctx(ks.actor_id, via="worker")
    assert api_worker._next_work(conn, w).get("task") is None  # T-516 was picked 241 times before its day
    now = tasks.create(conn, co["klead"], {"title": "Odpověz na lead", "assignee": {"type": "agent",
                                                                                    "id": ks.actor_id}})
    assert api_worker._next_work(conn, w)["task"]["id"] == now["id"]
    # an ok run that left the task open: held 6 h, not re-queued at once
    tasks.claim(conn, ks, now["id"])
    rid = conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, engine) VALUES "
                       "(?, ?, 'task', 'running', ?, 'claude')", (ks.actor_id, now["id"], now_iso())).lastrowid
    conn.commit()
    out = api_worker.finish_run(rid, api_worker.FinishIn(status="ok"), conn=conn, ctx=w)
    assert out["held_until"] > now_iso()
    assert api_worker._next_work(conn, w).get("task") is None
    # a held task with a later do_date waits for that day
    until = api_worker.idle_back_off(conn, planned["id"], ks.actor_id)
    assert until >= f"{(date.today() + timedelta(days=4)).isoformat()}T"


# ------------------------------------------------------------------ 5. the access loop

def test_a_looping_task_is_held_and_the_queue_is_reopened_once_a_day(conn, owner, co):
    ks, am, coo = co["ksales"], co["am"], co["coo"]
    t = tasks.create(conn, co["klead"], {"title": "T-516 follow-up", "assignee": {"type": "agent", "id": ks.actor_id}})
    for _ in range(6):
        conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, 'task', 'ok', ?)",
                     (ks.actor_id, t["id"], now_iso()))
    access.limit_hit(conn, ks.actor_id, "runs_day", 40, 40, task_id=t["id"])
    assert conn.execute("SELECT retry_after FROM tasks WHERE id = ?", (t["id"],)).fetchone()[0] > now_iso()
    # the queue task: decided and closed, then the same kind again: reopened once, then only counted
    q = conn.execute("SELECT id FROM tasks WHERE source = 'access' AND assignee_id = ?", (am.actor_id,)).fetchone()
    for _ in range(4):
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (q["id"],))
        access._wake_manager(conn, "again", subject="Kniha Growth & Sales · limit runs_day")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'access_queue_reopen'").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (q["id"],)).fetchone()[0] == "done"
    # the COO and the Access manager may pause a looping agent (not resume it)
    assert agents.pause(conn, am, ks.actor_id, True)["paused"]
    with pytest.raises(Exception):
        agents.pause(conn, am, ks.actor_id, False)
    assert not agents.pause(conn, owner, ks.actor_id, False)["paused"]
    assert agents.pause(conn, coo, ks.actor_id, True)["paused"]
    with pytest.raises(Exception):
        agents.pause(conn, co["se"], ks.actor_id, True)


def test_the_access_manager_tells_a_lead_about_an_agent_once_a_day(conn, owner, co):
    am, klead = co["am"], co["klead"]
    first = chat.send_dm(conn, am, klead.actor_id, "Kniha Growth & Sales narazil na limit runs_day.")
    again = chat.send_dm(conn, am, klead.actor_id, "Kniha Growth & Sales opět narazil na limit.")
    assert again.get("coalesced") and again["id"] == first["id"]
    other = chat.send_dm(conn, am, klead.actor_id, "Kniha Developer potřebuje přístup.")
    assert not other.get("coalesced")


# ------------------------------------------------------------------ 6. flood and loop guards

def test_a_one_way_flood_coalesces_into_one_digest_and_a_loop_never_goes_to_the_owner(conn, owner, co):
    ceo, growth = co["ceo"], co["growth"]
    outs = [chat.send_dm(conn, growth, ceo.actor_id, f"Update {i}: pipeline T-00{i}") for i in range(10)]
    assert sum(1 for o in outs if not o.get("coalesced")) == 6
    last = conn.execute("SELECT body FROM chat_messages WHERE id = ?", (outs[-1]["id"],)).fetchone()[0]
    assert "Update 9" in last and "Update 6" in last
    # an answer resets it
    chat.send_dm(conn, ceo, growth.actor_id, "Díky, stačí jednou denně.")
    assert not chat.send_dm(conn, growth, ceo.actor_id, "Rozumím").get("coalesced")
    # a CEO <-> Head of Growth loop: the COO sorts it out, never the owner (T-414)
    for i in range(8):
        chat.send_dm(conn, ceo, growth.actor_id, f"Otázka {i}?")
        chat.send_dm(conn, growth, ceo.actor_id, f"Odpověď {i}.")
    loop = conn.execute("SELECT assignee_id FROM tasks WHERE topic = 'chat-loop'").fetchone()
    assert loop is not None and loop[0] == co["coo"].actor_id


# ------------------------------------------------------------------ 7. the escalation dedup

def test_the_dedup_needs_a_shared_source_and_skips_scheduled_and_own_tasks(conn, owner, co):
    ceo, cto = co["ceo"], co["cto"]
    # T-232 case: a quota alert linked to older tasks, and the CEO's scheduled report citing other old tasks
    base = tasks.create(conn, owner, {"title": "Review fronta", "assignee": "CEO"})
    mid = tasks.create(conn, cto, {"title": "Review politika", "assignee": "CEO", "notes": f"Z {base['ref']}"})
    alert = tasks.create(conn, cto, {"title": "Kvóta: Claude usage limit", "assignee": "CEO",
                                     "notes": f"Souvisí s {base['ref']}"})
    report = tasks.create(conn, ceo, {"title": "CEO: denní přehled", "assignee": {"type": "agent", "id": ceo.actor_id},
                                      "notes": f"Odkud: {mid['ref']}", "source": "schedule:45"})
    assert not report.get("deduplicated") and report["id"] != alert["id"]
    # a self-assigned task is never merged either
    own = tasks.create(conn, ceo, {"title": "Moje poznámka", "assignee": {"type": "agent", "id": ceo.actor_id},
                                   "notes": f"K {alert['ref']}"})
    assert not own.get("deduplicated")
    # the same source named directly is still one escalation
    again = tasks.create(conn, cto, {"title": "Kvóta znovu", "assignee": "CEO", "notes": f"Viz {alert['ref']}"})
    assert again["deduplicated"] and again["id"] == alert["id"]


# ------------------------------------------------------------------ 8. the owner's comments wake work

def test_the_owners_comment_wakes_the_assignee_or_the_ceo(conn, owner, co):
    se, ceo = co["se"], co["ceo"]
    t = tasks.create(conn, co["cto"], {"title": "Nabu průzkum", "assignee": {"type": "agent", "id": se.actor_id}})
    conn.execute("UPDATE tasks SET status = 'waiting', retry_after = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) + timedelta(hours=20)).isoformat(timespec="seconds"), t["id"]))
    comments.add(conn, owner, t["id"], "Tohle udělej ještě dnes, prosím.")
    row = conn.execute("SELECT status, retry_after FROM tasks WHERE id = ?", (t["id"],)).fetchone()
    assert row["status"] == "next" and row["retry_after"] is None
    assert chat.inbox_unread(conn, se.actor_id) >= 1
    # a task with the owner himself: the CEO hears of it
    mine = tasks.create(conn, owner, {"title": "Moje věc", "status": "next"})
    before = chat.inbox_unread(conn, ceo.actor_id)
    comments.add(conn, owner, mine["id"], "CEO, převezmi to.")
    assert chat.inbox_unread(conn, ceo.actor_id) == before + 1


# ------------------------------------------------------------------ 9. no tasks for the owner from agents

def test_agents_cannot_give_the_owner_a_task(conn, owner, co):
    cto = co["cto"]
    with pytest.raises(tasks.Invalid, match="ask_owner"):
        tasks.create(conn, cto, {"title": "Zastav towerdog", "assignee": "David"})
    t = tasks.create(conn, cto, {"title": "Zastav towerdog", "assignee": "SRE"})
    with pytest.raises(tasks.Invalid):
        tasks.assign(conn, cto, t["id"], "David")
    # a person still can, and platform flows keep their rules
    assert tasks.assign(conn, owner, t["id"], "David")["assignee_id"] == owner.actor_id


# ------------------------------------------------------------------ 10. the watch routines in code

def test_the_heads_get_their_stuck_work_from_code_once(conn, owner, co):
    se, cto, coo = co["se"], co["cto"], co["coo"]
    t = tasks.create(conn, cto, {"title": "Zaseknutý úkol", "assignee": {"type": "agent", "id": se.actor_id},
                                 "status": "next"})
    conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_ago(10), t["id"]))
    conn.commit()
    out = head_alerts.sweep(conn)
    assert out["sent"].get("CTO") == 1
    assert head_alerts.sweep(conn) == {}  # the same list is not sent again within 12 h
    second = head_alerts.sweep(conn, second_line=True)
    assert second["sent"] == {"COO": 1}
    assert not conn.execute("SELECT 1 FROM runs").fetchone()  # no model run


# ------------------------------------------------------------------ 11. value fields

def test_agents_tasks_carry_a_project_and_a_value_kind(conn, owner, co):
    from pos import projects

    p = projects.create(conn, owner, name="Kniha", goal="Vydat knihu", lead="Kniha Lead",
                        member_refs=["Kniha Developer"])
    t = tasks.create(conn, co["kdev"], {"title": "Oprav build", "topic": "dev",
                                        "assignee": {"type": "agent", "id": co["kdev"].actor_id}})
    row = conn.execute("SELECT project_id, value_kind FROM tasks WHERE id = ?", (t["id"],)).fetchone()
    assert row["project_id"] == p["id"] and row["value_kind"] == "business"  # Kniha roles are business
    ops = tasks.create(conn, co["cto"], {"title": "Upgrade serveru", "assignee": "Software Engineer"})
    assert conn.execute("SELECT value_kind FROM tasks WHERE id = ?", (ops["id"],)).fetchone()[0] == "platform"
    # the classifier counts the team for older rows without the field
    conn.execute("UPDATE tasks SET value_kind = NULL, project_id = NULL WHERE id = ?", (t["id"],))
    assert business.classify(conn, conn.execute("SELECT * FROM tasks WHERE id = ?", (t["id"],)).fetchone()) \
        == "business"


# ------------------------------------------------------------------ the one-off production fix

def test_the_prod_fix_dry_runs_on_a_copy_then_applies_once(conn, owner, co, tmp_path):
    ceo, se, cto, coo = co["ceo"], co["se"], co["cto"], co["coo"]
    watch = schedules.create(conn, coo, {"name": "CTO: hlídání výpadků týmu (09:00)", "schedule": "daily 09:00",
                                         "visibility": "team", "assignee": {"type": "agent", "id": cto.actor_id}})
    conn.execute("UPDATE schedules SET id = 27 WHERE id = ?", (watch["id"],))
    stray = tasks.create(conn, Ctx(ceo.actor_id, via="system"), {"title": "HR přehled", "assignee": "David",
                                                                  "status": "inbox"})
    waiting = tasks.create(conn, cto, {"title": "Návrh kampaně", "assignee": "Head of Growth"})
    conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ?, updated_at = ? WHERE id = ?",
                 (cto.actor_id, _ago(100), waiting["id"]))
    conn.commit()
    dry = prodfix_workflow.run(conn, apply=False)
    assert dry["dry_run"] and dry["routines"] and dry["owner"] and any("review items created: CTO 1" in x
                                                                       for x in dry["reviews"])
    assert conn.execute("SELECT status FROM schedules WHERE id = 27").fetchone()[0] == "active"  # untouched
    assert _item(conn, waiting["id"]) is None
    prodfix_workflow.run(conn, apply=True)
    assert conn.execute("SELECT status FROM schedules WHERE id = 27").fetchone()[0] == "paused"
    assert tasks.get(conn, owner, stray["id"])["assignee_id"] == ceo.actor_id
    assert _item(conn, waiting["id"])["assignee_id"] == cto.actor_id
    # the backlog is not moved up the chain the hour its items appear
    assert conn.execute("SELECT reviewer_id FROM tasks WHERE id = ?", (waiting["id"],)).fetchone()[0] == cto.actor_id
    again = prodfix_workflow.run(conn, apply=True)
    assert not again["routines"] and not again["owner"] and not again["t232"]
    assert json.dumps(again)  # serialisable report


# ------------------------------------------------------------------ the review backlog drains in batches

def test_the_review_backlog_drains_in_daily_batches_business_first_and_waits_for_its_item(conn, owner, co):
    ceo, klead = co["ceo"], co["klead"]
    old = [tasks.create(conn, owner, {"title": f"Platforma {i}", "assignee": "QA Reviewer", "topic": "ops"})
           for i in range(4)]
    biz = [tasks.create(conn, klead, {"title": f"Kniha {i}", "assignee": {"type": "agent", "id": klead.actor_id}})
           for i in range(3)]
    for n, t in enumerate(old + biz):  # the platform rows are the oldest
        conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ?, updated_at = ? WHERE id = ?",
                     (ceo.actor_id, _ago(100 - n), t["id"]))
    conn.commit()
    dry = review_work.sync(conn, apply=False, daily_new=4)
    assert len(dry["created"]) == 4 and len(dry["deferred"]) == 3 and _item(conn, old[0]["id"]) is None
    out = review_work.sync(conn, daily_new=4)
    made = {t["id"] for t in old + biz if _item(conn, t["id"]) is not None}
    assert {t["id"] for t in biz} <= made and old[0]["id"] in made and len(made) == 4  # business, then oldest
    assert len(out["deferred"]) == 3
    assert not review_work.sync(conn, daily_new=4).get("created")  # the day's batch is used up
    # the SLA does not move a deferred result up the chain (its clock starts at its item)
    moved = business.review_sla(conn, dry_run=True).get("moved", [])
    assert not any(tasks.display_id(t["id"]) in x for t in old[1:] for x in moved)


def test_near_the_company_cap_the_picker_offers_only_business_work(conn, owner, co):
    se = co["se"]
    access.set_budget(conn, owner, None, "usd_day", 50.0, "company cap")
    plat = tasks.create(conn, co["cto"], {"title": "Úklid logů", "topic": "ops", "priority": 1,
                                          "assignee": {"type": "agent", "id": se.actor_id}})
    kniha = tasks.create(conn, co["cto"], {"title": "Platební brána pro zákazníka", "topic": "sales",
                                           "priority": 3, "assignee": {"type": "agent", "id": se.actor_id}})
    w = Ctx(se.actor_id, via="worker")
    assert api_worker._next_work(conn, w)["task"]["id"] == plat["id"]
    conn.execute("INSERT INTO engine_usage (at, engine, actor_id, input_tokens, output_tokens, cost_usd) "
                 "VALUES (?, 'claude', ?, 1000, 0, 41.0)", (now_iso(), co["qa"].actor_id))
    conn.commit()
    assert access.business_only(conn)
    out = api_worker._next_work(conn, w)
    assert out["task"]["id"] == kniha["id"] and out["state"]["business_only"]
