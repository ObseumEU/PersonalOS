"""Fixes from the 2026-10 audit of what agents tried and failed: tool arguments agents reach for
(follow_up with a reason, notes_append, task_comment text, list_tasks view="working", reviewers by role),
hand-in reports for routine work, and more."""

import json

import anyio
import pytest
from mcp.client import Client

from pos import actors, agents, mcp_server, owner_report, tasks
from pos.core import Ctx
from pos.db import connect, migrate


def _call(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


def _error(result) -> str:
    assert result.is_error
    return " ".join(getattr(c, "text", "") for c in result.content)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "a.db"
    conn = connect(path)
    migrate(conn)
    ids = actors.ensure_builtin(conn)
    agents.seed_builtin_permissions(conn)
    owner = Ctx(actors.owner_id(conn))
    made = {}
    for name, role in (("Worker A", "developer"), ("Kniha Marketing Lead", "marketing_lead"),
                       ("QA Reviewer", "qa"), ("Marketing Lead Assistant", "assistant_x")):
        a = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived",
                                permissions=["tasks:read", "tasks:claim", "tasks:write", "tasks:review",
                                             "messages:send"], data_dir=tmp_path)["agent"]
        conn.execute("UPDATE actors SET role = ? WHERE id = ?", (role, a["id"]))
        made[name] = a["id"]
    conn.commit()
    conn.close()
    return path, {**ids, **made}


def _task(path, ids, **extra):
    conn = connect(path)
    t = tasks.create(conn, Ctx(actors.owner_id(conn)),
                     {"title": "Napsat shrnutí", "assignee": {"type": "agent", "id": ids["Worker A"]}, **extra})
    conn.commit()
    conn.close()
    return t


def test_update_task_follow_up_with_a_reason_keeps_the_date_and_notes_append_adds(db):
    path, ids = db
    t = _task(path, ids, notes="Původní popis.")
    server = mcp_server.build(path, default_actor=lambda c: ids["Worker A"])

    async def scenario():
        async with Client(server) as c:
            _call(await c.call_tool("update_task", {"task_id": t["ref"], "fields": {
                "status": "waiting", "follow_up": "2026-10-09 (čekám na odpověď zákazníka)"}}))
            _call(await c.call_tool("update_task", {"task_id": t["ref"], "fields": {"notes_append": "Zjištění: X."}}))
            bad = await c.call_tool("update_task", {"task_id": t["ref"], "fields": {"follow_up": "až odpoví"}})
            assert "progress_note" in _error(bad)
            unknown = await c.call_tool("update_task", {"task_id": t["ref"], "fields": {"colour": "red"}})
            assert "notes_append" in _error(unknown)  # the error lists what can be changed

    anyio.run(scenario)
    conn = connect(path)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (t["id"],)).fetchone()
    assert row["follow_up"] == "2026-10-09" and row["status"] == "waiting"
    assert row["progress_note"] == "čekám na odpověď zákazníka"
    assert row["notes"].startswith("Původní popis.") and row["notes"].endswith("Zjištění: X.")
    conn.close()


def test_task_comment_takes_text_and_list_tasks_takes_working(db):
    path, ids = db
    t = _task(path, ids, status="working")
    server = mcp_server.build(path, default_actor=lambda c: ids["Worker A"])

    async def scenario():
        async with Client(server) as c:
            _call(await c.call_tool("task_comment", {"task_id": t["ref"], "text": "Poznámka k práci"}))
            working = _call(await c.call_tool("list_tasks", {"view": "working"}))
            assert [x["ref"] for x in working] == [t["ref"]]
            assert _call(await c.call_tool("list_tasks", {"view": "in_progress"})) == working
            bad = await c.call_tool("list_tasks", {"view": "whatever"})
            assert "to_review" in _error(bad) and "working" in _error(bad)

    anyio.run(scenario)
    conn = connect(path)
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE body = 'Poznámka k práci'").fetchone()[0] == 1
    conn.close()


def test_request_review_finds_the_reviewer_by_role_or_part_of_the_name(db):
    path, ids = db
    conn = connect(path)
    owner = Ctx(actors.owner_id(conn))
    assert tasks.resolve_reviewer(conn, owner, "qa") == ids["QA Reviewer"]
    assert tasks.resolve_reviewer(conn, owner, "marketing_lead") == ids["Kniha Marketing Lead"]
    assert tasks.resolve_reviewer(conn, owner, "Marketing Lead") == ids["Kniha Marketing Lead"]  # its role
    with pytest.raises(tasks.Invalid, match="ambiguous"):  # two names contain "Lead"
        tasks.resolve_reviewer(conn, owner, "lead")
    with pytest.raises(tasks.Invalid, match="member of PersonalOS"):
        tasks.resolve_reviewer(conn, owner, "Jan Novák")
    conn.close()
    t = _task(path, ids)
    server = mcp_server.build(path, default_actor=lambda c: ids["Worker A"])

    async def scenario():
        async with Client(server) as c:
            got = _call(await c.call_tool("request_review", {"task_id": t["ref"], "reviewer": "QA reviewer",
                                                             "note": "Hotovo, prosím o kontrolu."}))
            assert got["status"] == "review" and got["reviewer_name"] == "QA Reviewer"

    anyio.run(scenario)


def test_a_project_member_wins_an_ambiguous_reviewer_name(db):
    path, ids = db
    conn = connect(path)
    from pos import projects

    pid = projects.create(conn, Ctx(actors.owner_id(conn)), name="Kniha", channel=False,
                          member_refs=[ids["Kniha Marketing Lead"]])["id"]
    assert tasks.resolve_reviewer(conn, Ctx(actors.owner_id(conn)), "Lead", pid) == \
        ids["Kniha Marketing Lead"]
    conn.close()


def test_kniha_reservations_are_counted_without_personal_data(tmp_path, monkeypatch):
    from pos import kniha_reservations as kr

    rows = [
        {"kind": "gift", "email": "jana.novakova@seznam.cz", "recipient": "Babička Marie", "referral": "Petr Novák",
         "source": "hero", "createdAt": "2026-10-03T10:00:00Z", "src": "other:p-01", "utm_source": "facebook"},
        {"kind": "self", "email": "JANA.novakova@seznam.cz", "recipient": "", "referral": "", "source": "cenik",
         "createdAt": "2026-10-04T09:00:00Z", "src": "p01", "utm_source": "jan.svoboda@firma.cz"},
        {"kind": "lead_magnet", "email": "karel@example.cz", "source": "volný text se jménem Karel Dvořák",
         "createdAt": "2026-10-05T08:00:00Z", "src": "wo-01"},
        {"kind": "gift", "email": "test@obseum.cz", "source": "test-form", "createdAt": "2026-10-05T08:00:00Z"},
    ]
    f = tmp_path / "reservations.jsonl"
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{broken\n", encoding="utf-8")
    monkeypatch.setenv("POS_KNIHA_RESERVATIONS", str(f))
    out = kr.summary()
    assert out["total"] == 3 and out["tests_left_out"] == 1 and out["unreadable_lines"] == 1
    assert out["unique_contacts"] == 2 and out["gift_recipient_filled"] == 1 and out["referral_filled"] == 1
    assert {s["src"]: s["count"] for s in out["by_src"]} == {"p01": 2, "wo01": 1}
    assert out["by_kind"] == {"gift": 1, "self": 1, "lead_magnet": 1}
    assert out["by_day"] == {"2026-10-03": 1, "2026-10-04": 1, "2026-10-05": 1}
    text = json.dumps(out, ensure_ascii=False).lower()
    for personal in ("@", "marie", "novák", "novakova", "karel", "svoboda"):
        assert personal not in text, personal
    assert kr.summary(since="2026-10-04")["total"] == 2
    with pytest.raises(ValueError):
        kr.summary(since="včera")
    monkeypatch.setenv("POS_KNIHA_RESERVATIONS", str(tmp_path / "none.jsonl"))
    assert kr.summary()["available"] is False


def test_only_the_kniha_team_and_the_ceo_get_the_reservations_tool():
    from pathlib import Path

    from pos.access import service as access

    assert "tool:kniha_reservations_summary" not in access.autonomy_caps()  # not for everyone by default
    assert "kniha:reservations" not in access.autonomy_caps()
    assert access.restricted("tool:kniha_reservations_summary")
    root = Path(__file__).resolve().parents[2] / "agents"
    holders = {d.name for d in root.iterdir() if (d / "agent.json").is_file()
               and "tool:kniha_reservations_summary" in json.loads((d / "agent.json").read_text("utf-8")).get("grants", [])}
    assert holders == {"ceo", "kniha-lead", "kniha-developer", "kniha-marketing-lead", "kniha-growth-sales",
                       "kniha-content-creator"}


def test_a_routine_or_notify_only_report_needs_no_inline_content(db):
    path, ids = db
    plain = {"takeaway": "Kontrola proběhla, vše v pořádku.", "next": "Nic dalšího."}
    with pytest.raises(owner_report.Invalid, match="content is missing"):
        owner_report.validate(plain)
    assert owner_report.validate(plain, content_required=False)["takeaway"]
    t = _task(path, ids)
    plan = _task(path, ids, title="Plán kampaně", topic="plan")
    conn = connect(path)
    assert owner_report.content_required(conn, t["id"], plain) is False  # notify-only: no decisions
    decide = {**plain, "decisions": [{"question": "Pokračovat?", "options": ["Ano", "Ne"]}]}
    assert owner_report.content_required(conn, t["id"], decide) is True
    assert owner_report.content_required(conn, plan["id"], plain) is True  # a plan carries its text
    conn.close()
    assert "600 znaků" in mcp_server.REPORT_DESC
