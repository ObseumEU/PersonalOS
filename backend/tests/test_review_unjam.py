"""The one-off review unjam (pos.review_unjam): obsolete results closed, stale ones rerouted."""

from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, business, integrations, review_unjam, schedules, tasks
from pos.core import Ctx
from pos.db import connect, migrate


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "u.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    c.commit()
    yield c
    c.close()


def _agent(conn, owner, tmp_path, name, role, lead=None, team=None) -> Ctx:
    perms = ["tasks:read", "tasks:write", "tasks:claim", "tasks:review", "messages:send"]
    aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                              permissions=perms)["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ?, team = ? WHERE id = ?",
                 (role, lead.actor_id if lead else None, team, aid))
    conn.commit()
    return Ctx(aid, via="mcp")


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def test_unjam_closes_obsolete_reviews_and_sends_kniha_work_to_the_kniha_lead(conn, tmp_path):
    owner = Ctx(actors.owner_id(conn), via="api")
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo", team="leadership")
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo, team="engineering")
    se = _agent(conn, owner, tmp_path, "Software Engineer", "developer", cto, team="engineering")
    qa = _agent(conn, owner, tmp_path, "QA Reviewer", "qa", cto, team="engineering")
    klead = _agent(conn, owner, tmp_path, "Kniha Lead", "product_lead", ceo, team="kniha")
    kdev = _agent(conn, owner, tmp_path, "Kniha Developer", "developer", klead, team="kniha")

    def waiting(title, worker, reviewer, notes="", days=4, **kw):
        t = tasks.create(conn, cto, {"title": title, "assignee": {"type": "agent", "id": worker.actor_id},
                                     "notes": notes, **kw})
        conn.execute("UPDATE tasks SET status = 'review', reviewer_id = ?, updated_at = ?, created_at = ?, "
                     "progress_note = 'Hotovo.' WHERE id = ?", (reviewer.actor_id, _ago(days), _ago(days), t["id"]))
        return t["id"]

    incident = "Incident sentinelu: nová chyba v `personalos-sandbox-1` po commitu `c5e09bf`."
    fixes = [waiting(f"Fix: sandbox {i}", se, qa, incident) for i in range(3)]
    for n, f in enumerate(fixes):  # created within minutes of each other
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (_ago(4 - n / 1000), f))
    s = schedules.create(conn, owner, {"name": "Týdenní report", "schedule": "daily 07:00", "visibility": "team",
                                       "assignee": {"type": "agent", "id": cto.actor_id}})
    old_report = waiting("Týdenní report", cto, ceo, source=f"schedule:{s['id']}")
    newer = tasks.create(conn, owner, {"title": "Týdenní report", "assignee": "CTO", "source": f"schedule:{s['id']}"})
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (newer["id"],))
    kniha = waiting("App E2E: platba", kdev, qa, topic="kniha-dev")
    fresh = waiting("Kniha: čerstvý výsledek", kdev, qa, topic="kniha-dev", days=1)
    lead_own = waiting("Kniha: plán pilotu", klead, qa, topic="kniha")
    conn.commit()

    dry = review_unjam.unjam(conn)
    assert dry["applied"] is False and len(dry["closed"]) == 3 and len(dry["rerouted"]) == 1
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (fixes[1],)).fetchone()[0] == "review"

    out = review_unjam.unjam(conn, apply=True)
    status = {i: conn.execute("SELECT status, reviewer_id FROM tasks WHERE id = ?", (i,)).fetchone()
              for i in (*fixes, old_report, kniha, fresh, lead_own)}
    assert status[fixes[0]]["status"] == "review"  # the first of the incident stays
    assert status[fixes[1]]["status"] == status[fixes[2]]["status"] == "done"
    assert status[old_report]["status"] == "done"
    assert status[kniha]["reviewer_id"] == klead.actor_id
    assert status[fresh]["reviewer_id"] == qa.actor_id  # not stale yet (under 3 days)
    assert status[lead_own]["reviewer_id"] == qa.actor_id  # the Kniha Lead never reviews its own work
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'review_auto_accept'").fetchone()
    assert "duplicitní oprava" in conn.execute("SELECT progress_note FROM tasks WHERE id = ?",
                                               (fixes[1],)).fetchone()[0]
    assert out["waiting_per_reviewer"]["Kniha Lead"] == 1
    assert review_unjam.unjam(conn, apply=True)["closed"] == []  # idempotent
