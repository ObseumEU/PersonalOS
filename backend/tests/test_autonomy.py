"""Agents are autonomous (the owner, 2026-09-27): every agent holds every platform capability by
default, request_access is approved at once in code, budgets and limits are 20x looser, a review
never blocks work for long; the owner-only items stay the owner's."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, autonomy, deploy_review, mcp_server, tasks
from pos.access import service as access
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.db import connect
from pos.main import create_app


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AUTONOMY", "1")
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    made = agents.create_agent(conn, owner, name="Writer", purpose="writes", lifetime="long_lived",
                               permissions=["tasks:read"], data_dir=tmp_path)
    yield {"conn": conn, "owner": owner, "agent": made["agent"]["id"], "tmp": tmp_path}
    conn.close()
    client.__exit__(None, None, None)


def test_a_new_agent_holds_every_platform_capability_but_the_owners(app):
    conn, agent = app["conn"], app["agent"]
    have = access.effective(conn, agent)
    assert set(access.autonomy_caps()) <= have
    assert {"tasks:write", "tasks:review", "agents:create", "messages:send", "browser:use", "ops:observe",
            "tool:ha_ssh", "tool:browser"} <= have
    from pos import browser

    assert browser.may_browse(conn, agent) and not browser.may_use_computer(conn, agent)
    assert access.request_access(conn, Ctx(agent), what="capability", capability="tool:computer",
                                 why="a GUI-only admin page")["status"] == "granted"
    assert browser.may_use_computer(conn, agent)
    assert "access:manage" not in have and not any(c.startswith("tool:access_") for c in have)
    assert "outbound:*" not in have                                    # constitution rule 1: unchanged
    for tool in ("create_task", "chat_send", "schedule_list", "note_create", "search", "hire_agent", "loki_query"):
        assert mcp_server.may_use(conn, agent, tool), tool
    assert not mcp_server.may_use(conn, agent, "access_grant")


def test_a_revoked_capability_stays_revoked_and_the_owner_items_need_the_owner(app):
    conn, agent = app["conn"], app["agent"]
    am = Ctx(access.manager_id(conn), via="mcp")
    access.revoke(conn, am, agent, "routes:write", "abused the routing rules")
    access.seed(conn)                                                  # every start
    assert "routes:write" not in access.effective(conn, agent)
    out = access.request_access(conn, Ctx(agent), what="capability", capability="guard:rules", why="edit the guard")
    assert out["status"] == "pending" and out["needs_owner"]
    with pytest.raises(Forbidden, match="owner only"):
        access.grant(conn, am, agent, "constitution:edit", "no")
    # everything else asked for again is granted at once
    assert access.request_access(conn, Ctx(agent), what="capability", capability="routes:write",
                                 why="I need it back for the mail rule")["status"] == "granted"


def test_the_one_off_switch_brings_existing_agents_along_once(app, monkeypatch):
    conn, agent, owner = app["conn"], app["agent"], app["owner"]
    monkeypatch.setenv("POS_AUTONOMY", "0")
    old = agents.create_agent(conn, owner, name="Old timer", purpose="old", lifetime="long_lived",
                              permissions=["tasks:read"], data_dir=app["tmp"])["agent"]["id"]
    access.set_budget(conn, owner, old, "usd_day", 3.0, "old budget")
    access.set_budget(conn, owner, old, "usd_run", 0.5, "old run cap")
    from pos import settings_store
    from pos.hr.policy import SETTING_MAX_ACTIVE

    settings_store.put(conn, owner, SETTING_MAX_ACTIVE, 26)
    settings_store.put(conn, owner, access.SETTINGS_KEY, {"spike_factor": 5.0, "spike_floor_usd": 1.0})
    conn.commit()
    assert "tasks:write" not in access.effective(conn, old)
    dry = autonomy.run(conn, apply=False)
    assert dry["grants"] and dry["budgets"] and dry["hr"] and dry["spikes"]
    assert "tasks:write" not in access.effective(conn, old)            # a dry run writes nothing
    autonomy.run(conn, apply=True)
    assert set(access.autonomy_caps()) <= access.effective(conn, old)
    assert access.limit(conn, old, "usd_day") == 60.0 and access.limit(conn, old, "usd_run") == 2.5
    assert access.settings(conn)["spike_factor"] == 20.0
    again = autonomy.run(conn, apply=True)
    assert not any(again.values())                                     # budgets are not scaled twice
    assert access.limit(conn, old, "usd_day") == 60.0


def test_a_pending_review_holds_the_deployer_only_until_the_sla(app, monkeypatch, tmp_path):
    conn, owner = app["conn"], app["owner"]
    qa = agents.create_agent(conn, owner, name="QA Reviewer", purpose="reviews", lifetime="long_lived",
                             permissions=["tasks:read", "tasks:claim", "tasks:review"], data_dir=tmp_path)
    se = agents.create_agent(conn, owner, name="Software Engineer", purpose="code", lifetime="long_lived",
                             permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)
    dep = Ctx(actors.find_by_name(conn, "Deployer")["id"], via="deployer")
    sha = "a" * 40
    assert deploy_review.ask(conn, dep, sha, author="Software Engineer")["status"] == "pending"
    past = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat(timespec="seconds")
    conn.execute("UPDATE deploy_reviews SET created_at = ? WHERE sha = ?", (past, sha))
    conn.commit()
    out = deploy_review.ask(conn, dep, sha, author="Software Engineer")
    assert out["status"] == "approved" and "SLA" in out["note"]
    # the QA Reviewer still reviews after the fact; a return is a fix task for the author
    res = deploy_review.decide(conn, Ctx(qa["agent"]["id"], via="mcp"), sha, "return", "1. rename the flag")
    fix = tasks.get(conn, owner, tasks.parse_id(res["fix_task"]))
    assert fix["assignee_id"] == se["agent"]["id"] and "rename the flag" in fix["notes"]
