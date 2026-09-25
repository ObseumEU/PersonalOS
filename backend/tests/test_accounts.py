"""Accounts for people (REVIZE-FUNKCI 4.1)."""

from fastapi.testclient import TestClient

from pos import accounts, actors, tasks
from pos.config import Settings
from pos.core import Ctx
from pos.db import connect
from pos.main import create_app


def test_invite_accept_login_and_acting_as_yourself(tmp_path):
    settings = Settings(data_dir=tmp_path, password="owner-pw", session_secret="s" * 32)
    with TestClient(create_app(settings)) as owner:
        owner.post("/api/auth/login", json={"password": "owner-pw"})
        inv = owner.post("/api/invites", json={"email": "Jana@Firma.cz", "name": "Jana"}).json()
        assert inv["token"] and inv["email"] == "jana@firma.cz"
        assert owner.get("/api/invites").json()[0]["email"] == "jana@firma.cz"
        with TestClient(create_app(settings)) as jana:
            assert jana.get(f"/api/auth/invite/{inv['token']}").json()["name"] == "Jana"
            assert jana.post("/api/auth/accept", json={"token": inv["token"], "password": "short"}).status_code == 422
            me = jana.post("/api/auth/accept", json={"token": inv["token"], "password": "a-long-secret"}).json()
            assert me["name"] == "Jana" and me["is_owner"] is False
            # what Jana writes down is hers, "me" is Jana
            t = jana.post("/api/tasks", json={"title": "Call the supplier", "status": "next"}).json()
            assert t["assignee_name"] == "Jana" and t["owner_id"] == me["actor_id"]
            jana.post("/api/auth/logout")
            assert jana.post("/api/auth/login", json={"email": "jana@firma.cz", "password": "wrong-one!"}).status_code == 401
            assert jana.post("/api/auth/login", json={"email": "JANA@firma.cz", "password": "a-long-secret"}).json()["name"] == "Jana"
            # the invitation is used up
            assert jana.post("/api/auth/accept", json={"token": inv["token"], "password": "a-long-secret"}).status_code == 422
        # the owner's emergency login still acts as the owner
        assert owner.get("/api/auth/me").json()["is_owner"] is True


def test_an_agents_task_belongs_to_whoever_gave_the_work(tmp_path, monkeypatch):
    from pos import agents
    from pos.db import migrate

    c = connect(tmp_path / "o.db")
    migrate(c)
    actors.ensure_builtin(c)
    me = Ctx(actors.owner_id(c))
    inv = accounts.invite(c, me, email="petr@firma.cz", name="Petr")
    petr = accounts.accept(c, inv["token"], "petr-password-1")
    helper = agents.create_agent(c, me, name="Helper", purpose="helps Petr", lifetime="long_lived",
                                 permissions=["tasks:read", "tasks:write"], data_dir=tmp_path)["agent"]["id"]
    c.execute("UPDATE actors SET reports_to = ? WHERE id = ?", (petr, helper))
    t = tasks.create(c, Ctx(helper), {"title": "Draft the offer"})
    assert t["owner_id"] == petr
    parent = tasks.create(c, Ctx(petr), {"title": "Offer for Acme", "status": "next"})
    step = tasks.create(c, Ctx(helper), {"title": "Prices", "parent_id": parent["ref"]})
    assert step["owner_id"] == petr
    c.close()
