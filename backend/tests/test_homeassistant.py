"""The Home Assistant Specialist (pos.homeassistant, agents/home-assistant-specialist):
the token never leaves the API, plain HTTP only to a local-network host:port the
credential lists, the safety rules for both APIs, the WebSocket session, and its
routine every 4 days."""

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


def test_safety_rules_refuse_locks_alarms_gates_and_safety_devices(ha):
    conn, a = ha["conn"], ha["agent"]
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    for path, body in (("/api/services/lock/unlock", '{"entity_id": "lock.front_door"}'),
                       ("/api/services/alarm_control_panel/alarm_disarm", "{}"),
                       ("/api/services/cover/open_cover", '{"entity_id": "cover.garage_door"}'),
                       ("/api/services/switch/turn_off", '{"entity_id": "switch.smoke_detector_power"}')):
        with pytest.raises(creds.CredentialError, match="owner"):
            creds.http_call(conn, Ctx(a["id"]), "POST", f"http://192.168.1.56:8123{path}", ["home-assistant"],
                            body=body, transport=ok)
    # an ordinary change and every read pass
    creds.http_call(conn, Ctx(a["id"]), "POST", "http://192.168.1.56:8123/api/services/light/turn_on",
                    ["home-assistant"], body='{"entity_id": "light.kitchen"}', transport=ok)
    creds.http_call(conn, Ctx(a["id"]), "GET", "http://192.168.1.56:8123/api/states/lock.front_door",
                    ["home-assistant"], transport=ok)
    with pytest.raises(homeassistant.Refused):
        homeassistant.check_ws({"type": "call_service", "domain": "lock", "service": "unlock"})
    with pytest.raises(homeassistant.Refused):
        homeassistant.check_ws({"type": "config/automation/config/update",
                                "config": {"action": [{"service": "alarm_control_panel.alarm_disarm"}]}})
    homeassistant.check_ws({"type": "get_states"})
    homeassistant.check_ws({"type": "config/entity_registry/list"})
    homeassistant.check_ws({"type": "call_service", "domain": "light", "service": "turn_on"})


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
    with pytest.raises(homeassistant.Refused):
        homeassistant.ws_call(conn, Ctx(a["id"]), [{"type": "call_service", "domain": "lock", "service": "unlock"}],
                              connect=connect_)
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
