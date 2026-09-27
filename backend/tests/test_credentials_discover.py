"""Přístupy, the smart part (pos.credentials.discover and the one-click flows):
the suggestion rules (SSH / token / HTTP basic / DB, known hosts, which agent),
the model only when the rules cannot decide and never with a value, register +
grant in one call, a request for an item that is only in the vault approved in
one click, Odebrat with undo, and the grouped audit."""

import json

import pytest
from fastapi.testclient import TestClient

from pos import actors, tasks
from pos.access import service as access
from pos.config import Settings
from pos.core import Ctx, Forbidden
from pos.credentials import discover, onepassword
from pos.credentials import service as creds
from pos.db import connect
from pos.main import create_app

VAULT = "PersonalOS Agents"
SECRET = "hunter2-VERY-secret-value"
HA = "Home Assistant Specialist"


def field(fid, title, type_="Concealed", section=None):
    return {"id": fid, "title": title, "type": type_, "section": section}


ITEMS = [
    {"id": "ssh1", "title": "SSH HomeAssistant", "category": "Login",
     "fields": [field("username", "username", "Text"), field("password", "password")], "urls": [], "hosts": []},
    {"id": "tok1", "title": "Knowlage API", "category": "ApiCredentials",
     "fields": [field("credential", "credential"), field("hostname", "hostname", "Text")],
     "urls": ["https://knowlage.obseum.cz"], "hosts": []},
    {"id": "bas1", "title": "Router admin", "category": "Login",
     "fields": [field("username", "username", "Text"), field("password", "password")],
     "urls": ["http://192.168.1.1:8080"], "hosts": []},
    {"id": "db1", "title": "Postgres analytics", "category": "Database",
     "fields": [field("username", "username", "Text"), field("password", "password"), field("database", "database", "Text")],
     "urls": [], "hosts": ["192.168.1.108"]},
    {"id": "gen1", "title": "Mystery", "category": "Password", "fields": [field("password", "password"),
                                                                           field("x", "notes", "Text")],
     "urls": [], "hosts": []},
]


class FakeOP:
    def __init__(self):
        self.items_ = json.loads(json.dumps(ITEMS))

    def resolve(self, ref):
        return SECRET

    def items(self, vault):
        return json.loads(json.dumps(self.items_))


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "1")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_test")
    monkeypatch.setenv("POS_OP_VAULT", VAULT)
    onepassword.set_provider(FakeOP())
    calls = []
    monkeypatch.setattr(discover, "_llm", lambda conn, facts, roster: calls.append(facts) or
                        {"kind": "token", "agents": ["SRE"], "reason": "vypadá jako token"})
    discover._llm_cache.clear()
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    yield {"client": client, "conn": conn, "owner": owner, "llm": calls,
           "ha": actors.find_by_name(conn, HA)["id"], "sre": actors.find_by_name(conn, "SRE")["id"]}
    onepassword.set_provider(None)
    conn.close()
    client.__exit__(None, None, None)


def item(i):
    return json.loads(json.dumps(next(x for x in ITEMS if x["id"] == i)))


# ------------------------------------------------------------------ the rules

def test_kind_rules_ssh_token_basic_db_and_undecided():
    assert discover.detect_kind(item("ssh1"))[:2] == ("ssh", True)
    assert discover.detect_kind(item("tok1"))[:2] == ("token", True)
    assert discover.detect_kind(item("bas1"))[:2] == ("basic", True)
    assert discover.detect_kind(item("db1"))[:2] == ("db", True)
    assert discover.detect_kind(item("gen1"))[:2] == ("generic", False)
    # ssh from the URL scheme or port 22 too
    assert discover.detect_kind({"title": "Box", "category": "Login", "fields": [field("password", "password")],
                                 "urls": ["ssh://10.0.0.5:22"]})[0] == "ssh"


def test_known_hosts_match_titles_and_urls():
    assert discover.known_for({"title": "SSH HomeAssistant"})["key"] == "home-assistant"
    assert discover.known_for({"title": "Home Assistant token"})["key"] == "home-assistant"
    assert discover.known_for({"title": "svr03 root"})["key"] == "svr03"
    assert discover.known_for({"title": "whatever", "urls": ["https://knowlage.obseum.cz/api"]})["key"] == "knowlage"
    assert discover.known_for({"title": "Some other thing"}) is None


def test_suggestion_for_ha_ssh_uses_the_ha_ssh_tool_and_the_specialist(app):
    s = discover.suggest(app["conn"], item("ssh1"), taken=set())
    assert s["kind"] == "ssh" and s["decided"] and s["source"] == "rules"
    names = {c["role"]: c for c in s["credentials"]}
    assert names["password"]["name"] == "ha-ssh" and names["user"]["name"] == "ha-ssh-user"
    assert names["password"]["allowed_commands"] == ["ha_ssh"] and "192.168.1.56" in names["password"]["allowed_hosts"]
    assert s["extra_grants"] == ["tool:ha_ssh"]
    assert [a["name"] for a in s["agents"]] == [HA]


def test_token_basic_and_db_suggestions(app):
    conn = app["conn"]
    tok = discover.suggest(conn, item("tok1"), taken=set())
    c = tok["credentials"][0]
    assert c["field"] == "credential" and c["header"] == "Authorization: Bearer {value}"
    assert c["allowed_hosts"] == ["knowlage.obseum.cz"] and c["env_var"].endswith("TOKEN")
    assert [a["name"] for a in tok["agents"]] == ["Knowlage Specialist"]
    basic = discover.suggest(conn, item("bas1"), taken=set())
    assert basic["kind"] == "basic" and basic["credentials"][0]["allowed_hosts"] == ["192.168.1.1:8080"]
    assert "{{cred:router-admin-user}}:{{cred:router-admin}}@" in basic["usage"]
    db = discover.suggest(conn, item("db1"), taken=set())
    assert db["credentials"][0]["env_var"] == "PGPASSWORD" and db["credentials"][0]["allowed_commands"] == ["psql", "pg_dump"]
    assert [a["name"] for a in db["agents"]] == ["SRE"]  # 192.168.1.108 is svr03
    # a name already taken gets a suffix
    again = discover.suggest(conn, item("tok1"), taken={"knowlage-api"})
    assert again["credentials"][0]["name"] == "knowlage-api-2"


def test_agent_matching_by_role_text():
    roster = [{"id": 1, "name": "Nexus Specialist", "purpose": "Správce služby Nexus", "role": None, "team": None},
              {"id": 2, "name": "CFO", "purpose": "Finance: faktury, předplatná", "role": None, "team": None},
              {"id": 3, "name": "Software Engineer", "purpose": "Vývojář PersonalOS", "role": None, "team": None}]
    assert [a["id"] for a in discover.match_agents("Faktury Fakturoid", [], roster)] == [2]
    assert discover.match_agents("Totally unrelated", [], roster) == []


def test_the_model_only_when_rules_cannot_decide_and_never_sees_a_value(app):
    out = app["client"].get("/api/credentials/discover").json()
    by = {i["item_id"]: i for i in out["items"]}
    assert set(by) == {"ssh1", "tok1", "bas1", "db1", "gen1"}
    assert len(app["llm"]) == 1 and app["llm"][0]["title"] == "Mystery"
    assert set(app["llm"][0]) == {"title", "category", "fields", "hosts"}
    assert all(set(f) == {"title", "type"} for f in app["llm"][0]["fields"])
    assert by["gen1"]["source"] == "llm" and by["gen1"]["kind"] == "token" and by["gen1"]["agents"][0]["name"] == "SRE"
    assert SECRET not in json.dumps(out)
    # cached: a second load asks nobody
    app["client"].get("/api/credentials/discover")
    assert len(app["llm"]) == 1


def test_register_and_grant_in_one_call(app):
    client, conn = app["client"], app["conn"]
    s = next(i for i in client.get("/api/credentials/discover").json()["items"] if i["item_id"] == "ssh1")
    r = client.post("/api/credentials/register", json={"item_id": "ssh1", "credentials": s["credentials"],
                                                       "agent_ids": [a["id"] for a in s["agents"]]})
    assert r.status_code == 201, r.text
    assert {c["name"] for c in r.json()["credentials"]} == {"ha-ssh", "ha-ssh-user"}
    held = {g["credential"] for g in creds.grants(conn, agent_id=app["ha"])}
    assert held == {"ha-ssh", "ha-ssh-user"}
    assert "tool:ha_ssh" in access.effective(conn, app["ha"])
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'cred_register_grant'").fetchone()
    # registered now: no longer a discovery card; the overview shows the card with its chips
    assert "ssh1" not in {i["item_id"] for i in client.get("/api/credentials/discover").json()["items"]}
    page = client.get("/api/credentials").json()
    c = next(c for c in page["credentials"] if c["name"] == "ha-ssh")
    assert c["kind"] == "ssh" and c["item"] == "SSH HomeAssistant" and c["companions"] == ["tool:ha_ssh"]
    assert [g["agent_name"] for g in c["grants"]] == [HA] and c["recommended"][0]["name"] == HA
    # the same name twice: refused before anything is written
    bad = client.post("/api/credentials/register", json={"credentials": s["credentials"], "agent_ids": []})
    assert bad.status_code in (400, 409, 422) and "exists already" in bad.text


def test_only_the_owner_registers(app):
    conn = app["conn"]
    spec = {"credentials": discover.suggest(conn, item("tok1"), taken=set())["credentials"], "agent_ids": []}
    with pytest.raises(Forbidden):
        creds.register_and_grant(conn, Ctx(app["sre"]), spec)
    assert creds.list_all(conn) == []


def test_request_for_a_vault_item_registers_and_grants_on_approval(app):
    conn, owner, agent = app["conn"], app["owner"], app["ha"]
    t = tasks.create(conn, owner, {"title": "Fix the HA host", "assignee": {"type": "agent", "id": agent}})
    conn.commit()
    out = access.request_access(conn, Ctx(agent), what="capability", capability="cred:ha-ssh",
                                why="I need to read the supervisor logs", task_id=t["id"], blocking=True)
    assert out["needs_owner"]
    req = creds.open_requests(conn)[0]
    assert not req["registered"] and req["suggestion"]["title"] == "SSH HomeAssistant"
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (t["id"],)).fetchone()["status"] == "waiting"
    res = app["client"].post(f"/api/credentials/requests/{req['id']}/decide", json={"decision": "grant", "note": ""})
    assert res.status_code == 200, res.text
    assert res.json()["registered"]["primary"] == "ha-ssh"
    held = {g["credential"] for g in creds.grants(conn, agent_id=agent)}
    assert held == {"ha-ssh", "ha-ssh-user"} and "tool:ha_ssh" in access.effective(conn, agent)
    assert conn.execute("SELECT status FROM tasks WHERE id = ?", (t["id"],)).fetchone()["status"] == "next"
    assert creds.open_requests(conn) == []
    # a name that is nowhere is still refused
    from pos.access.service import AccessError

    with pytest.raises(AccessError, match="no credential called"):
        access.request_access(conn, Ctx(agent), what="capability", capability="cred:nothing-like-this", why="x")


def test_bulk_grant_revoke_with_undo_and_grouped_audit(app):
    client, conn, owner = app["client"], app["conn"], app["owner"]
    vitem = next(i for i in creds._vault_items(conn) if i["id"] == "tok1")
    s = discover.suggest(conn, vitem, taken=set())
    assert s["credentials"][0]["op_ref"] == f"op://{VAULT}/Knowlage API/credential"
    creds.register_and_grant(conn, owner, {"credentials": s["credentials"], "agent_ids": []})
    name = s["credentials"][0]["name"]
    r = client.post("/api/credentials/grants/bulk", json={"agent_ids": [app["sre"]], "names": [name], "hours": 5})
    assert r.status_code == 201, r.text
    gids = r.json()["grants"]
    assert client.post("/api/credentials/grants/revoke", json={"grant_ids": gids}).json()["revoked"] == gids
    assert creds.grants(conn, agent_id=app["sre"]) == []
    assert client.post("/api/credentials/grants/restore", json={"grant_ids": gids}).status_code == 200
    g = creds.grants(conn, agent_id=app["sre"])
    assert len(g) == 1 and g[0]["expires_at"]
    for _ in range(4):
        creds.resolve_for(conn, Ctx(app["sre"]), [name], "http", host="knowlage.obseum.cz")
    with pytest.raises(creds.CredentialError):
        creds.resolve_for(conn, Ctx(app["sre"]), [name], "http", host="evil.example.com")
    lines = [a for a in client.get("/api/credentials").json()["audit"] if a["name"] == name]
    assert len(lines) == 1 and lines[0]["count"] == 5 and lines[0]["errors"] == 1
    assert lines[0]["agent_name"] == "SRE" and "evil.example.com" in lines[0]["last_error"]
    detail = client.get("/api/credentials/uses", params={"name": name, "agent_id": app["sre"],
                                                         "day": lines[0]["day"]}).json()["uses"]
    assert len(detail) == 5
