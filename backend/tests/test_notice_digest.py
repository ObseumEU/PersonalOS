"""Informational notices to agents (pos.notice_digest): one digest a day, one line per subject, no run of their own."""

from datetime import datetime, timezone

import pytest

from pos import actors, agents, api_worker, chat, flood, notice_digest, tasks
from pos.access import service as access
from pos.core import Ctx
from pos.db import connect, migrate
from pos.notices import system_ctx


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "personalos.db")
    migrate(conn)
    actors.ensure_builtin(conn)
    owner = Ctx(actors.owner_id(conn))
    mk = lambda name: agents.create_agent(conn, owner, name=name, purpose="x", lifetime="long_lived",  # noqa: E731
                                          permissions=["tasks:read", "messages:send"], data_dir=tmp_path)["agent"]["id"]
    ids = {"ceo": mk("CEO"), "growth": mk("Kniha Growth & Sales"), "lead": mk("Kniha Lead")}
    conn.commit()
    yield conn, owner, ids
    conn.close()


def _rows(conn, actor_id):
    return conn.execute("""SELECT i.*, m.body FROM chat_inbox i JOIN chat_messages m ON m.id = i.message_id
                           WHERE i.actor_id = ? ORDER BY i.message_id""", (actor_id,)).fetchall()


def test_review_reminders_fold_into_one_digest_that_starts_no_run(env):
    """prod 30. 9.–6. 10.: 219 "T-x waits for your review" DMs to the CEO, each one able to start a run."""
    conn, owner, ids = env
    sys = system_ctx(conn)
    t1 = tasks.create(conn, owner, {"title": "Report A"})["id"]
    t2 = tasks.create(conn, owner, {"title": "Report B"})["id"]
    for _ in range(5):
        chat.send_dm(conn, sys, ids["ceo"], f"T-{t1} 'Report A' waits for your review over 12 h.", priority="fyi",
                     attachments=[{"type": "task", "id": t1}], system=True, notice="review_waits")
    rows = _rows(conn, ids["ceo"])
    assert len(rows) == 1 and rows[0]["info"] == 1 and rows[0]["read_at"] is None
    assert "×5" in rows[0]["body"] and notice_digest.TITLE in rows[0]["body"]
    # informational only: no inbox task, no rank, no run
    assert chat.inbox_unread(conn, ids["ceo"]) == 1 and chat.inbox_unread(conn, ids["ceo"], include_info=False) == 0
    assert chat.ensure_inbox_task(conn, ids["ceo"]) is None
    work = api_worker._next_work(conn, Ctx(ids["ceo"]))
    assert work.get("task") is None and work["unread_messages"] == 0
    # delivered with the next real run, then the same subject today does not come back
    rid = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', 'x')",
                       (ids["ceo"],)).lastrowid
    got = chat.check_inbox(conn, ids["ceo"], run_id=rid)
    assert [m["id"] for m in got] == [rows[0]["message_id"]]
    chat.send_dm(conn, sys, ids["ceo"], f"T-{t1} 'Report A' waits for your review over 24 h.", priority="fyi",
                 attachments=[{"type": "task", "id": t1}], system=True, notice="review_waits")
    assert chat.inbox_unread(conn, ids["ceo"]) == 0
    # a new subject: one more line in the same digest, unread again (still informational)
    chat.send_dm(conn, sys, ids["ceo"], f"T-{t2} 'Report B' waits for your review.", priority="fyi",
                 attachments=[{"type": "task", "id": t2}], system=True, notice="review_waits")
    rows = _rows(conn, ids["ceo"])
    assert len(rows) == 1 and rows[0]["read_at"] is None and rows[0]["info"] == 1
    assert "Report A" in rows[0]["body"] and "Report B" in rows[0]["body"] and "×6" in rows[0]["body"]
    assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE body LIKE ?",
                        (f"%{notice_digest.TITLE}%",)).fetchone()[0] == 1


def test_access_denials_and_the_cap_notice_are_digested(env):
    """prod 2.–3. 10.: 176 "Access manager zamítl tvou žádost" DMs to Kniha Growth & Sales."""
    conn, owner, ids = env
    access.store.ensure_schema(conn)
    access.ensure_access_manager(conn)
    for n in range(4):
        rid = access._insert_request(conn, agent_id=ids["growth"], requested_by=None, trigger="limit_hit",
                                     what="budget", metric="runs_day", amount=None, hours=None, task_id=None,
                                     why=f"limit {n}", detail={})
        access._settle(conn, ids["growth"], rid, "runs_day", "Smyčka: nezvyšuji.")
    rows = _rows(conn, ids["growth"])
    assert len(rows) == 1 and rows[0]["info"] == 1 and "×4" in rows[0]["body"]
    assert chat.inbox_unread(conn, ids["growth"], include_info=False) == 0
    access._dm_ceo(conn, "Strop firmy USD / 24 h: $41.92 z $50.00 (83 %).")
    access._dm_ceo(conn, "Strop firmy USD / 24 h: $44.78 z $50.00 (89 %).")
    rows = _rows(conn, ids["ceo"])
    assert len(rows) == 1 and "$44.78" in rows[0]["body"] and "$41.92" not in rows[0]["body"]


def test_a_person_gets_the_notice_as_it_is_and_urgent_notices_are_never_digested(env):
    conn, owner, ids = env
    sys = system_ctx(conn)
    t = tasks.create(conn, owner, {"title": "X"})["id"]
    out = chat.send_dm(conn, sys, owner.actor_id, "T-1 waits for your review.", priority="fyi", system=True,
                       notice="review_waits")
    assert not out.get("digest") and notice_digest.TITLE not in out["body"]
    out = chat.send_dm(conn, sys, ids["lead"], f"Owner commented on T-{t}: hotovo?", priority="change_plan",
                       attachments=[{"type": "task", "id": t}], system=True, notice="owner_comment")
    assert not out.get("digest")
    assert chat.inbox_unread(conn, ids["lead"], include_info=False) == 1  # this one is worth a run


def test_a_flood_digest_marked_unread_again_starts_no_run(env, monkeypatch):
    """flood.merge marks the digest unread again: informational, so no inbox run (it used to make one)."""
    conn, owner, ids = env
    monkeypatch.setenv("POS_CHAT_ONEWAY", "1/3600")
    a = Ctx(ids["lead"])
    first = chat.send_dm(conn, a, ids["growth"], "Prosím mrkni na T-1.")
    rid = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', 'x')",
                       (ids["growth"],)).lastrowid
    chat.check_inbox(conn, ids["growth"], run_id=rid)
    conn.execute("UPDATE runs SET status = 'ok' WHERE id = ?", (rid,))
    merged = chat.send_dm(conn, a, ids["growth"], "A ještě T-2.")
    assert merged.get("coalesced") and merged["id"] == first["id"]
    assert chat.inbox_unread(conn, ids["growth"]) == 1
    assert chat.inbox_unread(conn, ids["growth"], include_info=False) == 0
    assert chat.ensure_inbox_task(conn, ids["growth"]) is None
    assert flood.one_way_limit() == (1, 3600)


def test_the_digest_is_per_day(env):
    conn, owner, ids = env
    sys = system_ctx(conn)
    day1 = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    to = actors.get(conn, ids["ceo"])
    a = notice_digest.add(conn, sys, to, "Strop firmy 80 %", "company_cap", None, now=day1)
    b = notice_digest.add(conn, sys, to, "Strop firmy 85 %", "company_cap", None, now=day2)
    assert a["id"] != b["id"]
    assert notice_digest.subject_of("access_denied:runs_day", None) == ("access_denied", "runs_day")
    assert notice_digest.subject_of("handed_in", [{"type": "task", "id": 7}]) == ("handed_in", "T-7")
