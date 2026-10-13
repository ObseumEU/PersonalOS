"""Agent-managed secrets (pos.credentials.secrets): agents create, share and rotate secrets without seeing a value.

Covers creation from every source (generate, capture_browser, capture_command, from_secret, a literal value) with the
value never in the tool result, the audit, the transcript or the run's record; the sharing rules (same team, across
teams with the digest, a high secret only after the owner's approval); revoke, rotate and delete; masking of the new
credentials by the existing redaction; the grounding check; limits and the name policy; the improve signal; and the
owner's page /api/secrets (owner only, never a value)."""

import json
import re

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.client import Client

from pos import accounts, actors, agents, approvals, mcp_server, needs_me, transcripts
from pos.access import service as access
from pos.config import Settings, get_settings
from pos.core import Ctx, Forbidden
from pos.credentials import secrets as sec
from pos.credentials import serverstore as ss
from pos.credentials import service as creds
from pos.db import connect
from pos.improve import signals
from pos.main import create_app
from test_credentials import FakeOP, _nowhere_in_db, op  # noqa: F401 - fixtures

LITERAL = "Lit3ral-V4lue-only-the-agent-knew"
PAGE_SECRET = "pk_live_C4ptur3d-from-the-page-XYZ"
CMD_SECRET = "cmd-0utput-T0ken-9f8e7d6c5b4a"


@pytest.fixture
def world(tmp_path, monkeypatch, op):  # noqa: F811
    monkeypatch.setenv("POS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POS_SECRETS_KEY", "k" * 40)
    get_settings.cache_clear()
    settings = Settings(data_dir=tmp_path, scheduler=False)
    client = TestClient(create_app(settings))
    client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))

    def agent(name, team, perms=("tasks:read", "tasks:claim", "approvals:request")):
        made = agents.create_agent(conn, owner, name=name, purpose=f"{name} work", lifetime="long_lived",
                                   permissions=list(perms), data_dir=tmp_path)
        aid = made["agent"]["id"]
        conn.execute("UPDATE actors SET team = ? WHERE id = ?", (team, aid))
        conn.commit()
        return aid, made["api_key"]

    a, a_key = agent("Kniha Dev", "kniha", perms=("tasks:read", "tasks:claim", "approvals:request", "browser:use"))
    b, _ = agent("Kniha QA", "kniha")
    c, _ = agent("Ops Bot", "ops-team")
    yield {"client": client, "conn": conn, "owner": owner, "a": a, "a_key": a_key, "b": b, "c": c,
           "db": settings.db_path, "tmp": tmp_path}
    conn.close()
    client.__exit__(None, None, None)
    get_settings.cache_clear()


def _mcp(db, actor, calls):
    """Run tool calls as `actor` over the pos MCP server; returns [(is_error, text)]."""
    server = mcp_server.build(db, default_actor=lambda c: actor)

    async def go():
        out = []
        async with Client(server) as cl:
            for name, args in calls:
                r = await cl.call_tool(name, args)
                out.append((r.is_error, "".join(getattr(x, "text", "") for x in r.content)))
        return out

    return anyio.run(go)


def _run(conn, actor):
    rid = conn.execute("INSERT INTO runs (actor_id, kind, status, started_at) VALUES (?, 'task', 'running', "
                       "'2026-10-13T08:00:00+00:00')", (actor,)).lastrowid
    conn.commit()
    return rid


def _value(name, field):
    return ss.resolve(ss.ref(name, field))


def _signals(db) -> dict:
    """The improve loop's event signals over all time (its own connection: a collector may write)."""
    c = connect(db)
    try:
        return signals.events(c, "2000-01-01", "2999-01-01")
    finally:
        c.rollback()
        c.close()


# ------------------------------------------------------------------ creating, from every source

def test_generate_creates_registers_and_grants_without_showing_the_value(world):
    conn, a, db = world["conn"], world["a"], world["db"]
    (err, text), = _mcp(db, a, [("secret_create", {
        "name": "kniha-smtp", "description": "SMTP login for the Kniha test mailer",
        "fields": {"username": {"source": "value", "value": "smtp-user-77x"}, "password": {"source": "generate",
                                                                                     "length": 40}},
        "hosts": ["smtp.example.com"], "purpose": "test e-mails"})])
    assert not err, text
    pw = _value("kniha-smtp", "password")
    assert len(pw) == 40 and re.fullmatch(r"[A-Za-z0-9]+", pw)
    assert pw not in text and "{{cred:kniha-smtp}}" in text and "{{cred:kniha-smtp.username}}" in text
    # registered as credentials (primary = password), granted to the creator, nowhere in the database
    assert creds.get(conn, "kniha-smtp")["op_ref"] == "pos://kniha-smtp/password"
    assert creds.get(conn, "kniha-smtp.username")["allowed_hosts"] == ["smtp.example.com"]
    assert {g["credential"] for g in creds.grants(conn, agent_id=a)} == {"kniha-smtp", "kniha-smtp.username"}
    s = sec.get(conn, "kniha-smtp")
    assert s["created_by"] == a and s["team"] == "kniha" and s["sensitivity"] == "normal"
    assert {f["name"]: f["source"] for f in s["fields"]} == {"username": "value", "password": "generate"}
    _nowhere_in_db(db, pw)
    _nowhere_in_db(db, "smtp-user-77x")  # the literal is in no audit row either
    # the agent sees it in secret_list, with metadata only
    (_, listed), = _mcp(db, a, [("secret_list", {})])
    assert "kniha-smtp" in listed and pw not in listed and "Kniha Dev" in listed


def test_a_literal_value_is_redacted_from_the_audit_the_transcript_and_the_run_record(world):
    conn, a, db, client = world["conn"], world["a"], world["db"], world["client"]
    rid = _run(conn, a)
    (err, text), = _mcp(db, a, [("secret_create", {
        "name": "partner-api", "description": "Partner API key the partner e-mailed",
        "fields": {"token": {"source": "value", "value": LITERAL}}, "hosts": ["api.partner.example"]})])
    assert not err and LITERAL not in text and "prefer generate" in text
    # the CLI's stream as the worker hands it in: the tool call carries the literal, the model even echoed it
    jsonl = "\n".join(json.dumps(x) for x in [
        {"type": "system", "subtype": "init", "session_id": "s1", "cwd": "/work"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "mcp__pos__secret_create",
                                                       "input": {"name": "partner-api", "fields": {"token": {
                                                           "source": "value", "value": LITERAL}}}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": f"stored {LITERAL}"}]}},
        {"type": "result", "num_turns": 2, "total_cost_usd": 0.01, "result": f"done {LITERAL}"}])
    r = client.post(f"/api/worker/runs/{rid}/finish", json={"status": "ok", "jsonl": jsonl,
                                                            "detail": f"done, key {LITERAL}"},
                    headers={"Authorization": f"Bearer {world['a_key']}"})
    assert r.status_code == 200, r.text
    _nowhere_in_db(db, LITERAL)
    summary = conn.execute("SELECT summary FROM run_transcripts WHERE run_id = ?", (rid,)).fetchone()[0]
    assert "secret_create" in summary and LITERAL not in summary
    assert "[REDACTED" in conn.execute("SELECT detail FROM runs WHERE id = ?", (rid,)).fetchone()[0]
    # a literal is expected in its own call: not a misuse of a value; but the improve loop counts the literal
    assert not conn.execute("SELECT 1 FROM audit_log WHERE action = 'secret_misuse'").fetchone()
    keys = _signals(db)
    assert "secret:misuse.literal_value" in keys


def test_a_generated_value_seen_in_a_run_is_a_misuse_and_is_redacted(world):
    conn, a, db, client = world["conn"], world["a"], world["db"], world["client"]
    rid = _run(conn, a)
    (err, _), = _mcp(db, a, [("secret_create", {"name": "gen-one", "description": "generated",
                                                "fields": ["token"], "source": "generate"})])
    assert not err
    leaked = _value("gen-one", "token")
    jsonl = json.dumps({"type": "result", "num_turns": 1, "result": f"oops {leaked}"})
    client.post(f"/api/worker/runs/{rid}/finish", json={"status": "ok", "jsonl": jsonl, "detail": leaked},
                headers={"Authorization": f"Bearer {world['a_key']}"})
    _nowhere_in_db(db, leaked)
    assert conn.execute("SELECT 1 FROM audit_log WHERE action = 'secret_misuse'").fetchone()
    assert "secret:misuse.value_in_run" in _signals(db)


def test_capture_browser_stages_the_page_value_for_the_capturing_agent_only(world):
    conn, a, b, client = world["conn"], world["a"], world["b"], world["client"]
    rid = _run(conn, a)
    h = {"Authorization": f"Bearer {world['a_key']}"}
    bad = client.post("/api/worker/browser/capture", json={"target": "secret", "value": PAGE_SECRET,
                                                           "url": "http://evil.example.com/keys", "run_id": rid},
                      headers=h)
    assert bad.status_code == 403  # plain HTTP off the LAN
    r = client.post("/api/worker/browser/capture", json={"target": "secret", "value": PAGE_SECRET,
                                                         "url": "https://dashboard.stripe.com/test/apikeys",
                                                         "run_id": rid}, headers=h)
    assert r.status_code == 200 and PAGE_SECRET not in r.text
    cid = r.json()["capture_id"]
    assert ss.STAGE_RE.match(cid)
    # another agent cannot use the capture
    (err, text), = _mcp(world["db"], b, [("secret_create", {
        "name": "stolen", "description": "x", "fields": {"key": {"source": "capture_browser", "capture_id": cid}}})])
    assert err and "not yours" in text
    (err, text), = _mcp(world["db"], a, [("secret_create", {
        "name": "kniha-stripe-test", "description": "Stripe test secret key", "sensitivity": "normal",
        "fields": {"key": {"source": "capture_browser", "capture_id": cid}}, "hosts": ["api.stripe.com"],
        "header": "Authorization: Bearer {value}"})])
    # "stripe" says payment: high, whatever the agent said
    assert not err and PAGE_SECRET not in text and '"sensitivity":"high"' in text.replace(" ", "")
    assert _value("kniha-stripe-test", "key") == PAGE_SECRET
    # a capture is taken once
    (err, text), = _mcp(world["db"], a, [("secret_create", {
        "name": "again", "description": "x", "fields": {"key": {"source": "capture_browser", "capture_id": cid}}})])
    assert err and "capture it again" in text
    _nowhere_in_db(world["db"], PAGE_SECRET)


def test_capture_command_takes_stdout_from_the_sandbox_and_never_returns_it(world, monkeypatch):
    from pos import sandbox

    files: dict[str, bytes] = {}
    ran: list[str] = []

    async def run(who, command, timeout=None, workdir=None):
        ran.append(command)
        m = re.search(r"> (/var/tmp/\.pos-secret-[0-9a-f]+)$", command)
        if m:
            files[m.group(1)] = (CMD_SECRET + "\n").encode()
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        m = re.match(r"rm -f (\S+)$", command)
        if m:
            files.pop(m.group(1), None)
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    async def read(who, path, max_bytes=0):
        if path not in files:
            raise sandbox.SandboxError("no such file")
        return files[path], path

    monkeypatch.setattr(sandbox, "run", run)
    monkeypatch.setattr(sandbox, "read", read)
    (err, text), = _mcp(world["db"], world["a"], [("secret_create", {
        "name": "deploy-token", "description": "token printed by the setup script",
        "fields": {"token": {"source": "capture_command", "command": "python3 make_token.py"}}})])
    assert not err, text
    assert CMD_SECRET not in text and _value("deploy-token", "token") == CMD_SECRET
    assert "python3 make_token.py" in ran[0] and files == {}  # the file is gone again
    _nowhere_in_db(world["db"], CMD_SECRET)

    async def failing(who, command, timeout=None, workdir=None):
        m = re.search(r"> (/var/tmp/\.pos-secret-[0-9a-f]+)$", command)
        if m:
            files[m.group(1)] = CMD_SECRET.encode()
            return {"exit_code": 3, "stdout": "", "stderr": f"warning {CMD_SECRET} failed"}
        return {"exit_code": 0}

    monkeypatch.setattr(sandbox, "run", failing)
    (err, text), = _mcp(world["db"], world["a"], [("secret_create", {
        "name": "deploy-token-2", "description": "x",
        "fields": {"token": {"source": "capture_command", "command": "false"}}})])
    assert err and "exit 3" in text and CMD_SECRET not in text  # stderr comes back redacted


def test_from_secret_derives_basic_auth_and_needs_an_unscoped_grant(world):
    conn, a, b, db = world["conn"], world["a"], world["b"], world["db"]
    (err, text), = _mcp(db, a, [("secret_create", {
        "name": "kniha-staging", "description": "staging basic auth",
        "fields": {"username": {"source": "value", "value": "tester"},
                   "password": {"source": "generate", "length": 24},
                   "authorization": {"source": "from_secret", "secret": "self:password", "transform": "basic_auth",
                                     "user_secret": "self:username"}},
        "hosts": ["staging.kniha.example"], "header": "Authorization: Basic {value}", "primary_field": "authorization"})])
    assert not err, text
    import base64

    pw = _value("kniha-staging", "password")
    assert _value("kniha-staging", "authorization") == base64.b64encode(f"tester:{pw}".encode()).decode()
    assert creds.get(conn, "kniha-staging")["op_ref"] == "pos://kniha-staging/authorization"
    # copying another credential: only with an unscoped grant; refused is audited for the improve loop
    (err, text), = _mcp(db, b, [("secret_create", {
        "name": "copy-of-staging", "description": "x",
        "fields": {"pw": {"source": "from_secret", "secret": "kniha-staging.password"}}})])
    assert err and "do not hold" in text
    assert "secret:denied.from_secret_no_grant" in _signals(db)
    (err, text), = _mcp(db, a, [("secret_create", {
        "name": "copy-of-staging", "description": "a copy",
        "fields": {"pw": {"source": "from_secret", "secret": "kniha-staging.password", "transform": "sha256"}}})])
    assert not err, text
    import hashlib

    assert _value("copy-of-staging", "pw") == hashlib.sha256(pw.encode()).hexdigest()
    assert conn.execute("SELECT COUNT(*) FROM credential_uses WHERE tool = 'derive' AND ok = 1").fetchone()[0] == 1


# ------------------------------------------------------------------ sharing, revoke, rotate, delete

def _create(db, actor, name, **extra):
    (err, text), = _mcp(db, actor, [("secret_create", {"name": name, "description": f"{name} for tests",
                                                       "fields": ["token"], "source": "generate", **extra})])
    assert not err, text


def test_share_in_the_team_and_across_teams_is_immediate_and_the_cross_team_one_is_in_the_digest(world):
    conn, a, b, c, db = world["conn"], world["a"], world["b"], world["c"], world["db"]
    _create(db, a, "kniha-cms", hosts=["cms.kniha.example"])
    # a non-holder cannot share it
    (err, text), = _mcp(db, b, [("secret_share", {"name": "kniha-cms", "to_agent": "Ops Bot", "reason": "x"})])
    assert err and "do not hold" in text
    (err, text), = _mcp(db, a, [("secret_share", {"name": "kniha-cms", "to_agent": "Kniha QA",
                                                  "reason": "QA tests the CMS"})])
    assert not err and "Kniha QA" in text
    assert sec.holds(conn, b, sec.get(conn, "kniha-cms"))
    (err, text), = _mcp(db, a, [("secret_share", {"name": "kniha-cms", "to_team": "ops-team",
                                                  "reason": "Ops monitors the CMS"})])
    assert not err and "Ops Bot" in text and "Across teams" in text
    assert sec.holds(conn, c, sec.get(conn, "kniha-cms"))
    row = conn.execute("SELECT detail FROM audit_log WHERE action = 'secret_share_cross_team'").fetchone()
    assert row and "Ops Bot" in row[0]
    # the owner's daily digest lists the cross-team share
    out = access.digest(conn)
    assert out["sent"]
    body = conn.execute("SELECT body FROM chat_messages ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "Tajemství sdílená mezi týmy" in body and "kniha-cms" in body and "Ops Bot" in body
    # the shared agent uses it like any credential: the value comes back redacted
    value = _value("kniha-cms", "token")

    def echo(request):
        return httpx.Response(200, text=f"you sent {request.headers.get('x-key')}")

    got = creds.http_call(conn, Ctx(c, via="mcp"), "GET", "https://cms.kniha.example/ping",
                          headers={"X-Key": "{{cred:kniha-cms}}"}, transport=httpx.MockTransport(echo))
    assert value not in json.dumps(got) and "[REDACTED:kniha-cms]" in got["body"]


def test_request_access_cannot_bypass_secret_share(world):
    """An agent's secret is not granted on request (that would skip the share rules and a high one's approval)."""
    from pos.access.service import AccessError

    conn, a, c, db = world["conn"], world["a"], world["c"], world["db"]
    _create(db, a, "kniha-payments", sensitivity="high")
    with pytest.raises(AccessError, match="secret_share"):
        access.request_access(conn, Ctx(c, via="mcp"), what="capability", capability="cred:kniha-payments",
                              why="I want it")
    assert not sec.holds(conn, c, sec.get(conn, "kniha-payments"))


def test_a_high_secret_is_shared_only_after_the_owners_approval(world):
    conn, a, b, db, owner = world["conn"], world["a"], world["b"], world["db"], world["owner"]
    _create(db, a, "kniha-bank-api", sensitivity="high")
    assert sec.get(conn, "kniha-bank-api")["sensitivity"] == "high"
    (err, text), = _mcp(db, a, [("secret_share", {"name": "kniha-bank-api", "to_agent": "Kniha QA",
                                                  "reason": "reconciliation tests"})])
    assert not err and "pending_approval" in text
    assert not sec.holds(conn, b, sec.get(conn, "kniha-bank-api"))
    ap = [x for x in approvals.pending(conn) if x["action"] == "secret.share"]
    assert len(ap) == 1 and ap[0]["details"]["priority"] == "business"
    # business priority: first in the owner's "Čeká na tebe" approvals, even before newer ones
    approvals.request(conn, Ctx(b, via="mcp"), "payment", {"kind": "money", "why": "a newer one"})
    conn.commit()
    items = [i for i in needs_me.collect(conn, owner)["items"] if i["kind"] == "approval"]
    assert items[0]["action"] == "secret.share" and "kniha-bank-api" in items[0]["title"]
    approvals.decide(conn, owner, ap[0]["id"], True)
    conn.commit()
    s = sec.get(conn, "kniha-bank-api")
    assert sec.holds(conn, b, s)
    g = creds.grants(conn, name="kniha-bank-api", agent_id=b)[0]
    assert g["granted_by"] == owner.actor_id and g["source"] == "owner_approval"


def test_revoke_rotate_and_delete(world):
    conn, a, b, c, db = world["conn"], world["a"], world["b"], world["c"], world["db"]
    (err, text), = _mcp(db, a, [("secret_create", {
        "name": "kniha-webhook", "description": "webhook signing secret",
        "fields": {"secret": {"source": "generate", "charset": "hex", "length": 32},
                   "secret_b64": {"source": "from_secret", "secret": "self:secret", "transform": "base64"}}})])
    assert not err, text
    _mcp(db, a, [("secret_share", {"name": "kniha-webhook", "to_agent": "Kniha QA", "reason": "tests"})])
    before = _value("kniha-webhook", "secret")
    # rotate: new value, derived field follows, grants stay
    (err, text), = _mcp(db, a, [("secret_rotate", {"name": "kniha-webhook"})])
    assert not err, text
    after = _value("kniha-webhook", "secret")
    import base64

    assert after != before and re.fullmatch(r"[0-9a-f]{32}", after) and after not in text
    assert _value("kniha-webhook", "secret_b64") == base64.b64encode(after.encode()).decode()
    s = sec.get(conn, "kniha-webhook")
    assert sec.holds(conn, a, s) and sec.holds(conn, b, s) and s["rotated_at"]
    # an agent outside the team that does not hold it cannot rotate; nor revoke someone else's grant
    (err, text), = _mcp(db, c, [("secret_rotate", {"name": "kniha-webhook"})])
    assert err
    (err, text), = _mcp(db, c, [("secret_revoke", {"name": "kniha-webhook", "from_agent": "Kniha QA",
                                                   "reason": "no"})])
    assert err and "takes it away" in text
    (err, text), = _mcp(db, a, [("secret_revoke", {"name": "kniha-webhook", "from_agent": "Kniha QA",
                                                   "reason": "done testing"})])
    assert not err and not sec.holds(conn, b, sec.get(conn, "kniha-webhook"))
    # delete: value, credentials and grants go; the name is free again
    (err, text), = _mcp(db, b, [("secret_delete", {"name": "kniha-webhook"})])
    assert err
    (err, text), = _mcp(db, a, [("secret_delete", {"name": "kniha-webhook", "reason": "not needed"})])
    assert not err
    assert "kniha-webhook" not in ss.names() and not creds.grants(conn, agent_id=a)
    assert creds.check_reference(conn, name="kniha-webhook")["state"] == "missing"


# ------------------------------------------------------------------ safety: grounding, limits, names, transcripts

def test_the_grounding_check_recognises_an_agents_secret(world):
    conn, a, db = world["conn"], world["a"], world["db"]
    _create(db, a, "kniha-analytics")
    assert creds.check_reference(conn, name="kniha-analytics")["state"] == "ok"
    assert creds.check_reference(conn, ref="pos://kniha-analytics/token")["state"] == "ok"
    from pos import grounding

    assert "kniha-analytics" in grounding._credential_names(conn)


def test_name_policy_and_limits(world, monkeypatch, op):  # noqa: F811
    conn, a, db = world["conn"], world["a"], world["db"]
    op.items = lambda vault: [{"id": "i9", "title": "Stripe live", "category": "Login", "fields": []}]
    from pos.credentials import onepassword

    onepassword.clear_cache()
    ss.put("kniha-test-admin", ss.basic_fields("admin", "adm-pw-123456"), source="test")
    from test_credentials import REF

    creds.add(conn, world["owner"], {"name": "github-deploy", "op_ref": REF, "allowed_hosts": ["github.com"]})
    bad = [("Bad_Name", "kebab"), ("ab", "kebab"), ("stripe-live", "1Password"), ("kniha-test-admin", "operator"),
           ("github-deploy", "registry")]
    for name, why in bad:
        (err, text), = _mcp(db, a, [("secret_create", {"name": name, "description": "x", "fields": ["token"],
                                                       "source": "generate"})])
        assert err, (name, text)
    (err, text), = _mcp(db, a, [("secret_create", {"name": "too-long-value", "description": "x",
                                                   "fields": {"t": {"source": "value", "value": "x" * 5000}}})])
    assert err and "4096" in text and "x" * 100 not in text
    (err, text), = _mcp(db, a, [("secret_create", {"name": "too-long-gen", "description": "x",
                                                   "fields": {"t": {"source": "generate", "length": 999}}})])
    assert err and "length" in text
    monkeypatch.setenv("POS_SECRETS_MAX_PER_AGENT", "1")
    _create(db, a, "first-one")
    (err, text), = _mcp(db, a, [("secret_create", {"name": "second-one", "description": "x", "fields": ["token"],
                                                   "source": "generate"})])
    assert err and "limit 1" in text
    assert "secret:denied.create_limit" in _signals(db)


def test_transcript_summary_scrubs_a_secret_tools_literal_value():
    line = json.dumps({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "t1", "name": "mcp__pos__secret_create",
        "input": {"name": "x-y", "fields": {"a": {"source": "value", "value": LITERAL}}}}]}})
    out = transcripts.summarize(line)
    assert LITERAL not in json.dumps(out) and "[REDACTED]" in out["calls"][0]["input"]
    codex = json.dumps({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "pos",
                                                           "tool": "secret_rotate", "status": "failed",
                                                           "arguments": json.dumps({"fields": {"a": {
                                                               "source": "value", "value": LITERAL}}})}})
    assert LITERAL not in json.dumps(transcripts.summarize(codex))


def test_humans_cannot_create_and_people_tools_refuse(world):
    with pytest.raises(Forbidden):
        sec.plan_create(world["conn"], world["owner"], name="mine", description="x", fields=["t"], source="generate")


# ------------------------------------------------------------------ the owner's page

@pytest.fixture
def page(tmp_path, monkeypatch, op):  # noqa: F811
    monkeypatch.setenv("POS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POS_SECRETS_KEY", "k" * 40)
    get_settings.cache_clear()
    settings = Settings(data_dir=tmp_path, password="owner-pw", session_secret="s" * 32, scheduler=False)
    app = create_app(settings)
    owner_client, other, anon = TestClient(app), TestClient(app), TestClient(app)
    owner_client.__enter__()
    conn = connect(settings.db_path)
    owner = Ctx(actors.owner_id(conn))
    inv = accounts.invite(conn, owner, email="petr@firma.cz", name="Petr")
    accounts.accept(conn, inv["token"], "petr-password-1")
    made = agents.create_agent(conn, owner, name="Kniha Dev", purpose="dev", lifetime="long_lived",
                               permissions=["tasks:read", "tasks:claim"], data_dir=tmp_path)
    conn.commit()
    assert owner_client.post("/api/auth/login", json={"password": "owner-pw"}).status_code == 200
    assert other.post("/api/auth/login", json={"email": "petr@firma.cz", "password": "petr-password-1"}).status_code == 200
    yield {"owner": owner_client, "other": other, "anon": anon, "conn": conn, "agent": made["agent"]["id"],
           "db": settings.db_path}
    conn.close()
    owner_client.__exit__(None, None, None)
    get_settings.cache_clear()


def test_the_secrets_page_is_owner_only_and_never_shows_a_value(page):
    conn, agent = page["conn"], page["agent"]
    ss.put("kniha-test-admin", ss.basic_fields("admin", "adm-Heslo-987654"), source="/opt/server/kniha-test/.env")
    plan = sec.plan_create(conn, Ctx(agent, via="mcp"), name="kniha-cdn", description="CDN token",
                           fields={"token": {"source": "value", "value": LITERAL}}, hosts=["cdn.example.com"])
    sec.finish(conn, Ctx(agent, via="mcp"), plan)
    assert page["anon"].get("/api/secrets").status_code == 401
    assert page["other"].get("/api/secrets").status_code == 403
    r = page["owner"].get("/api/secrets")
    assert r.status_code == 200
    body = r.text
    assert LITERAL not in body and "adm-Heslo" not in body and r.json()["values_shown"] is False
    by = {s["name"]: s for s in r.json()["secrets"]}
    assert by["kniha-test-admin"]["origin"] == "operator" and by["kniha-cdn"]["creator"] == "Kniha Dev"
    assert by["kniha-cdn"]["holders"][0]["name"] == "Kniha Dev"
    # the actions: owner only
    for path, body_ in (("/api/secrets/kniha-cdn/sensitivity", {"sensitivity": "high"}),
                        ("/api/secrets/kniha-cdn/revoke", {"agent_id": agent, "reason": "x"}),
                        ("/api/secrets/kniha-cdn/delete", {"reason": "x"})):
        assert page["other"].post(path, json=body_).status_code == 403, path
        assert page["anon"].post(path, json=body_).status_code == 401, path
    assert page["owner"].post("/api/secrets/kniha-cdn/sensitivity", json={"sensitivity": "high"}).json()[
        "sensitivity"] == "high"
    assert page["owner"].post("/api/secrets/kniha-cdn/revoke", json={"agent_id": agent, "reason": "x"}).status_code == 200
    assert not sec.holds(conn, agent, sec.get(conn, "kniha-cdn"))
    assert page["owner"].post("/api/secrets/kniha-cdn/delete", json={"reason": "x"}).status_code == 200
    assert "kniha-cdn" not in {s["name"] for s in page["owner"].get("/api/secrets").json()["secrets"]}
    # no endpoint returns a value: nothing like a reveal route exists
    assert page["owner"].get("/api/secrets/kniha-test-admin/value").status_code in (404, 405)
    _nowhere_in_db(page["db"], LITERAL)
