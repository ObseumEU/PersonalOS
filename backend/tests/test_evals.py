"""The agent eval suite (pos.evals) in mock mode: every scenario's good transcript passes every check,
every bad transcript fails the check it targets, and the live wiring (the agent's tools and prompt, the
mock MCP server, the command guard) works without a model."""

import json
import subprocess
import sys

import pytest

from pos.evals import checks, fixtures, runner, scenarios, schemas
from pos.evals.__main__ import main

IDS = [s["id"] for s in scenarios.SCENARIOS]
BAD = [(s["id"], i, b["breaks"]) for s in scenarios.SCENARIOS for i, b in enumerate(s["bad"])]


def by_id(sid: str) -> dict:
    return next(s for s in scenarios.SCENARIOS if s["id"] == sid)


def test_every_role_has_two_scenarios_and_unique_ids():
    assert len(IDS) == len(set(IDS))
    counts = {r: sum(1 for s in scenarios.SCENARIOS if s["role"] == r) for r in scenarios.roles()}
    assert set(counts) == {"ceo", "head-of-customer-success", "kniha-lead", "kniha-growth-sales",
                           "software-engineer", "sre", "access-manager"}
    assert all(2 <= n <= 3 for n in counts.values())


@pytest.mark.parametrize("sid", IDS)
def test_every_check_has_a_bad_transcript(sid):
    sc = by_id(sid)
    names = [c["name"] for c in sc["checks"]]
    assert len(names) == len(set(names))
    assert {b["breaks"] for b in sc["bad"]} == set(names)
    assert all(c["type"] in checks.CHECKS for c in sc["checks"])


@pytest.mark.parametrize("sid", IDS)
def test_good_transcript_passes(sid):
    rep = runner.mock_scenario(by_id(sid))
    assert rep.failures == [], rep.failures
    assert rep.passed == rep.total > 0


@pytest.mark.parametrize("sid,index,breaks", BAD, ids=[f"{s}-{b}" for s, _, b in BAD])
def test_bad_transcript_fails_its_check(sid, index, breaks):
    sc = by_id(sid)
    rep = runner.mock_scenario(sc, runner.mutate(sc["good"], sc["bad"][index]["ops"]))
    hit = {r["name"]: r for r in rep.results}[breaks]
    assert not hit["ok"] and hit["why"]
    assert rep.passed < rep.total


def test_scenarios_only_use_real_tool_names():
    """A renamed or removed pos tool breaks the scenarios here, not silently in a live run."""
    _, tools, real = schemas.tools()
    assert real, "the real schemas could not be introspected from pos.mcp_server"
    names = {t["name"] for t in tools}
    assert all(t["inputSchema"].get("type") == "object" for t in tools)
    used = set()
    for sc in scenarios.SCENARIOS:
        variants = [sc["good"], *(runner.mutate(sc["good"], b["ops"]) for b in sc["bad"])]
        used |= {c["name"] for v in variants for c in v["calls"] if c["name"] not in runner.BUILTIN}
        used |= {t for c in sc["checks"] for t in (c.get("tools") or [])} - runner.BUILTIN
        used |= set(sc.get("canned") or {})
    used -= {"raise_memory_limit"}
    assert used - names == set()
    assert set(schemas.FALLBACK) <= names


@pytest.mark.parametrize("sid", IDS)
def test_live_wiring_shows_the_tools_the_good_run_needs(sid):
    """The agent's pos tools as the worker would show them (its permissions, grants, profile) include every
    tool the good transcript calls, and its prompt builds."""
    from pos.evals import live

    sc = by_id(sid)
    _, tools, _ = schemas.tools()
    me = live.agent_me(sc["role"], [t["name"] for t in tools], sc.get("memory", ""))
    _, _, prompt, wtools = live._worker_imports()
    shown, _ = wtools.pos_tools(me)
    needed = {c["name"] for c in sc["good"]["calls"] if c["name"] not in runner.BUILTIN}
    assert needed - set(shown) == set()
    me["stable_prompt"] = prompt.stable_prompt(me)
    assert me["name"] in me["stable_prompt"] and "Guardrails" in me["guardrails"]
    task = prompt.build_task_prompt(me, sc["task"], sc.get("context") or [], include_guardrails=False,
                                    include_stable=False)
    assert sc["task"]["ref"] in task
    assert me["model"] == "claude-opus-5-5" and me["profile"].get("effort") == "medium"


def test_mock_server_answers_and_records(tmp_path):
    canned = tmp_path / "canned.json"
    record = tmp_path / "calls.jsonl"
    canned.write_text(json.dumps({
        "instructions": "hi", "tools": [{"name": "metrics_snapshot", "description": "d", "inputSchema": {
            "type": "object", "properties": {"host": {"type": "string"}}}}],
        "responses": {"metrics_snapshot": {"__by__": "host", "cases": {"agent": {"disk": 41}},
                                           "default": {"__seq__": [{"disk": 98}, {"disk": 46}]}},
                      "ops_runbook": {"__error__": "not allowlisted"}},
        "defaults": {"task": {"ref": "T-1", "title": "x"}}}), encoding="utf-8")
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            *({"jsonrpc": "2.0", "id": 3 + i, "method": "tools/call", "params": {"name": n, "arguments": a}}
              for i, (n, a) in enumerate([("metrics_snapshot", {"host": "svr03"}), ("metrics_snapshot", {"host": "agent"}),
                                          ("metrics_snapshot", {"host": "svr03"}), ("ops_runbook", {"action": "x"}),
                                          ("create_task", {"title": "T", "assignee": "CTO"}), ("get_task", {"task_id": "T-1"})]))]
    out = subprocess.run([sys.executable, "-m", "pos.evals.mock_server", str(canned), str(record)],
                         input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True,
                         encoding="utf-8", timeout=60, env={**__import__("os").environ,
                                                            "PYTHONPATH": str(runner.REPO / "backend" / "src")})
    replies = {r["id"]: r for r in map(json.loads, out.stdout.splitlines())}
    assert replies[1]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[1]["result"]["instructions"] == "hi"
    assert [t["name"] for t in replies[2]["result"]["tools"]] == ["metrics_snapshot"]
    text = [json.loads(replies[i]["result"]["content"][0]["text"]) for i in (3, 4, 5)]
    assert text == [{"disk": 98}, {"disk": 41}, {"disk": 46}]
    assert replies[6]["result"]["isError"] and "not allowlisted" in replies[6]["result"]["content"][0]["text"]
    assert json.loads(replies[7]["result"]["content"][0]["text"])["assignee_name"] == "CTO"
    assert json.loads(replies[8]["result"]["content"][0]["text"])["ref"] == "T-1"
    calls = [json.loads(x) for x in record.read_text(encoding="utf-8").splitlines()]
    assert [c["name"] for c in calls][:2] == ["metrics_snapshot", "metrics_snapshot"] and len(calls) == 6
    assert calls[0]["args"] == {"host": "svr03"}


def test_command_guard_stand_in(tmp_path):
    work = str(tmp_path / "work")
    assert fixtures.check_command({"command": "python -m pytest -q", "cwd": work, "workdir": work})["auto"] is True
    assert fixtures.check_command({"command": "git push origin main", "cwd": work, "workdir": work})["outcome"] != "allow"
    assert fixtures.check_command({"command": "curl -X POST https://x.example", "cwd": work,
                                   "workdir": work}).get("auto") is not True


@pytest.mark.parametrize("text,czech", [
    ("Zařídím to, platbu kartou má Kniha Lead do konce října.", True),
    ("Dobrý den, paní Malá, moc nás mrzí, že kniha dorazila poškozená.", True),
    ("On it. The Kniha Lead owns card payments now and will report back to you.", False),
    ("OK.", True),
    ("Done.", False),
    ("Root cause: the disk was full (98.7%).\nVerified: disk at 46% after the prune.", False),
])
def test_czech_heuristic(text, czech):
    assert checks.is_czech(text) is czech


def test_raw_ids_and_money_promises():
    sc = {"owner_channels": [12]}
    t = checks.Transcript(calls=[{"name": "chat_send", "args": {"channel": 12, "body": "Hotovo, viz "
                                                                 "[úkol](https://pos.obseum.cz/tasks/123)."}}])
    assert checks.no_raw_ids(t, sc)[0]
    for body in ("Úkol T-12 je hotový.", "Zdroj: mail-acme:c2.", "Poznámka 231 je hotová.", "Viz #4512."):
        t.calls[0]["args"]["body"] = body
        assert not checks.no_raw_ids(t, sc)[0], body
    t = checks.Transcript(calls=[{"name": "request_outbound", "args": {"action": "email.send", "payload": {
        "body": "Dobrý den, vrátíme se vám s odpovědí zítra."}}}])
    assert checks.no_money_promise(t, sc)[0]
    t.calls[0]["args"]["payload"]["body"] = "Dobrý den, vrátíme vám peníze."
    assert not checks.no_money_promise(t, sc)[0]
    t.calls[0]["args"]["kind"] = "commitment"
    assert checks.no_money_promise(t, sc)[0]


def test_cli_mock_run(capsys):
    assert main(["--scenario", "ceo-card-payments"]) == 0
    out = capsys.readouterr().out
    assert "ceo-card-payments" in out and "8/8" in out and "selftest: 8/8" in out
    assert main(["--role", "sre", "--json", "--no-selftest"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {r["scenario"] for r in data["reports"]} == {"sre-disk-full", "sre-oom-restarts"}
    assert all(r["score"] == 1.0 for r in data["reports"])
