"""HR and leads create agents end to end, without the owner, within the limits in
code (pos.hiring.hire): the worker is provisioned in the agent pool at once, the
grants and a budget are seeded, probation starts, #team hears about it; a lead
hires only into its own part of the chart and never hands out more than it has.
The existing Home Assistant Specialist is the example agent (no test agent of
its own)."""

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, chat, hiring, org, tasks, workers
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect
from pos.main import create_app

HA = "Home Assistant Specialist"


@pytest.fixture
def co(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "1")
    monkeypatch.setenv("POS_WORKER_KEYS_DIR", str(tmp_path / "keys"))
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="t" * 32, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    by = {n: actors.find_by_name(conn, n) for n in ("HR agent", "Project manager", HA, "Monitor", "Access manager")}
    yield {"conn": conn, "keys": tmp_path / "keys", "data": tmp_path, **by}
    conn.close()
    client.__exit__(None, None, None)


def _ctx(row):
    return Ctx(row["id"], via="mcp")


def test_hr_hires_an_agent_that_works_at_once(co):
    conn, hr, pm = co["conn"], co["HR agent"], co["Project manager"]
    out = hiring.hire(conn, _ctx(hr), name="Hlídač faktur", purpose="hlídá splatnost faktur",
                      job_description="Každé ráno projde faktury a upozorní na splatné.", role="finance",
                      team="finance", lead="Project manager", permissions=["tasks:read", "tasks:claim"],
                      data_dir=co["data"])
    assert out["created"] and out["lead"] == "Project manager" and out["worker"] == "pool/hlidac-faktur"
    aid = out["agent"]["id"]
    a = actors.get(conn, aid)
    assert a["reports_to"] == pm["id"] and a["role"] == "finance" and a["team"] == "finance" and a["probation_until"]
    key = (co["keys"] / "pool" / "hlidac-faktur" / "key").read_text().strip()
    assert actors.actor_for_key(conn, key) == aid                     # its worker can start now
    assert agents.permissions_of(conn, aid) >= {"tasks:read", "tasks:claim"}  # grants seeded
    assert conn.execute("SELECT COUNT(*) FROM access_budgets WHERE agent_id = ?", (aid,)).fetchone()[0] == 4
    assert "Náplň práce" in agents.instructions_of(a)
    assert "Hlídač faktur" in conn.execute("SELECT body FROM chat_messages ORDER BY id DESC LIMIT 1").fetchone()[0]
    files = conn.execute("SELECT notes FROM tasks WHERE title LIKE 'Soubory nového agenta%'").fetchone()
    assert files and '"worker": "pool"' in files["notes"]                 # its files go to git
    # its lead gives it work, and it answers the owner through its worker
    t = tasks.create(conn, _ctx(pm), {"title": "Zkontroluj faktury", "assignee": {"type": "agent", "id": aid},
                                      "notes": "Test.", "definition_of_done": "Seznam splatných."})
    assert t["assignee_id"] == aid
    owner = Ctx(actors.owner_id(conn))
    conn.execute("UPDATE actors SET last_seen_at = ? WHERE id = ?", ("2099-01-01T00:00:00+00:00", aid))
    msg = chat.send_dm(conn, owner, aid, "Jak jsi na tom?")
    chat_task = conn.execute("SELECT * FROM tasks WHERE assignee_id = ? AND title LIKE 'Chat: answer%'", (aid,)).fetchone()
    assert chat_task is not None
    reply = chat.send(conn, Ctx(aid), msg["channel_id"], "Začínám.", reply_to=msg["id"])
    assert reply["body"] == "Začínám."


def test_a_lead_hires_only_below_itself_and_never_hands_out_more(co):
    conn, pm, ha, monitor = co["conn"], co["Project manager"], co[HA], co["Monitor"]
    # the PM hires a helper for the Home Assistant Specialist (in its part of the chart)
    out = hiring.hire(conn, _ctx(pm), name="Pomocník HA", purpose="pomáhá s dashboardy", lead=HA,
                      permissions=["tasks:read", "tasks:claim"], data_dir=co["data"])
    assert out["created"] and actors.get(conn, out["agent"]["id"])["reports_to"] == ha["id"]
    # the specialist is a lead now: it hires under itself, not under someone else
    with pytest.raises(Forbidden, match="own part of the chart"):
        hiring.hire(conn, _ctx(ha), name="Cizí", purpose="x", lead="Monitor", permissions=["tasks:read"],
                    data_dir=co["data"])
    with pytest.raises(Forbidden, match="escalation"):
        hiring.hire(conn, _ctx(ha), name="Mocnější", purpose="x", permissions=["tasks:read", "ops:monitor"],
                    data_dir=co["data"])
    with pytest.raises(Forbidden, match="only the owner grants"):
        hiring.hire(conn, _ctx(pm), name="Tvůrce", purpose="x", permissions=["agents:create"], data_dir=co["data"])
    # a member without reports does not hire at all
    with pytest.raises(Forbidden, match="leads"):
        hiring.hire(conn, _ctx(monitor), name="Nikdo", purpose="x", data_dir=co["data"])
    assert monitor["reports_to"] != monitor["id"]


def test_over_hrs_limits_it_becomes_a_request_for_the_owner(co, monkeypatch):
    from pos.hr import service as hr

    conn, hr_agent = co["conn"], co["HR agent"]
    monkeypatch.setattr(hr, "admit_agent", lambda *a, **k: {"allowed": False, "decision": "ask_owner",
                                                             "reason": "nad limitem týmu"})
    out = hiring.hire(conn, _ctx(hr_agent), name="Navíc", purpose="x", permissions=["tasks:read"], data_dir=co["data"])
    assert out["created"] is False and out["decider"] == "Owner" and actors.find_by_name(conn, "Navíc") is None


def test_hr_and_leads_edit_instructions_of_the_agents_below_them(co):
    conn, hr, pm, ha = co["conn"], co["HR agent"], co["Project manager"], co[HA]
    text = "# Home Assistant Specialist\n\nNová verze instrukcí: nejdřív záloha, pak změna, vždy rollback."
    out = agents.propose_instructions(conn, _ctx(hr), ha["id"], text, "test")
    assert out["path"] == "agents/home-assistant-specialist/INSTRUCTIONS.md"
    with pytest.raises(Forbidden):                                    # HR: not the Access manager
        agents.propose_instructions(conn, _ctx(hr), co["Access manager"]["id"], text, "test")
    made = hiring.hire(conn, _ctx(pm), name="Nový", purpose="x", permissions=["tasks:read"], data_dir=co["data"])
    out = agents.propose_instructions(conn, _ctx(pm), made["agent"]["id"], text, "lead")
    assert out["applied_now"] and "rollback" in agents.instructions_of(actors.get(conn, made["agent"]["id"]))
    with pytest.raises(Forbidden):                                    # not a lead of the HA Specialist's peer
        agents.propose_instructions(conn, _ctx(ha), co["Monitor"]["id"], text, "x")
    assert workers.reply_path(conn, actors.get(conn, made["agent"]["id"]))["kind"] == "pool"
    assert org.manages(conn, pm["id"], made["agent"]["id"])
