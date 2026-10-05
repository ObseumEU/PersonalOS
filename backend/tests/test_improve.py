"""The closed self-improvement loop (pos.improve): the signal digest on a fixture DB, the target block on
fix tasks, the verification 7 days after deploy, the weekly triage / coach tasks and their scheduling,
the owner's section of the weekly report and the web smoke."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from pos import actors, agents, business, integrations, scheduler, tasks, weekly, weekly_packet
from pos.core import Ctx
from pos.db import connect, migrate
from pos.improve import loop, signals, smoke


@pytest.fixture(autouse=True)
def no_outside(monkeypatch):
    monkeypatch.delenv("POS_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(weekly_packet, "http_get", None)
    monkeypatch.setenv("POS_KNIHA_DIR", "/nonexistent-kniha")
    monkeypatch.setenv("POS_UX_SMOKE", "0")


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "i.db")
    migrate(c)
    actors.ensure_builtin(c)
    integrations.register_builtin_agents(c)
    business.ensure_schema(c)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    return Ctx(actors.owner_id(conn), via="api")


def _agent(conn, owner, tmp_path, name, role, lead=None, team=None) -> int:
    row = actors.find_by_name(conn, name)
    if row is not None:
        aid = row["id"]
    else:
        aid = agents.create_agent(conn, owner, name=name, purpose=name, lifetime="long_lived", data_dir=tmp_path,
                                  permissions=["tasks:read", "tasks:write", "tasks:claim", "tasks:review",
                                               "messages:send", "approvals:request"])["agent"]["id"]
    conn.execute("UPDATE actors SET role = ?, reports_to = ?, team = ?, runtime = 'codex_worker' WHERE id = ?",
                 (role, lead, team, aid))
    conn.commit()
    return aid


@pytest.fixture
def company(conn, owner, tmp_path):
    ceo = _agent(conn, owner, tmp_path, "CEO", "ceo")
    cto = _agent(conn, owner, tmp_path, "CTO", "cto", ceo, "engineering")
    out = {"ceo": ceo, "cto": cto}
    for name, role in (("Software Engineer", "developer"), ("QA Reviewer", "qa"), ("SRE", "sre"),
                       ("Security Engineer", "security")):
        out[name] = _agent(conn, owner, tmp_path, name, role, cto, "engineering")
    out["coach"] = _agent(conn, owner, tmp_path, "Performance Coach", "coach", ceo, "people")
    return out


NOW = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)  # Monday 06:00 Prague


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def _run(conn, actor, status, detail="", at=None, kind="task", task_id=None) -> int:
    return conn.execute("INSERT INTO runs (actor_id, task_id, kind, status, started_at, detail) VALUES (?, ?, ?, ?, ?, ?)",
                        (actor, task_id, kind, status, iso(at or NOW - timedelta(hours=3)), detail)).lastrowid


def _audit(conn, actor, action, at, detail=None, entity=None, entity_id=None, run_id=None):
    conn.execute("INSERT INTO audit_log (at, actor_id, run_id, via, action, entity, entity_id, detail) "
                 "VALUES (?, ?, ?, 'mcp', ?, ?, ?, ?)",
                 (iso(at), actor, run_id, action, entity, entity_id, json.dumps(detail or {})))


def _deploy(conn, status, at, stage="", task_id=None):
    conn.execute("INSERT INTO deploys (old_sha, new_sha, status, stage, created_at, task_id) VALUES ('a', 'b', ?, ?, ?, ?)",
                 (status, stage, iso(at), task_id))


# ------------------------------------------------------------------ normalizing

def test_causes_are_normalized_to_stable_keys():
    assert signals.slug("follow_up must be YYYY-MM-DD") == "follow_up"
    assert signals.slug("unknown fields: ['notes_append']") == "unknown_fields"
    assert signals.slug("no credential called 'litellm-read': credentials_list shows") == "no_credential_called"
    assert signals.slug("report is not ready for the owner: content is missing") == "report_not_ready_owner"
    # Prod wording (2026-10): refs and channel numbers do not split one cause into many keys.
    assert signals.slug("T-556 is working, not claimable") == signals.slug("T-12 is working, not claimable") \
        == "working_not_claimable"
    assert signals.slug("you are not in #32: agents read the channels they belong to") == "not_agents_read_channels"
    assert signals.slug("the project's members, its lead or the owner record decisions") == "project_members_lead_owner"
    assert signals.run_cause("worker went silent for 5 min") == signals.run_cause("worker went silent for 20 min") \
        == "worker_silent"
    assert signals.run_cause("timeout after 60s") == "timeout"
    assert signals.run_cause("You've hit your session limit · resets 7:10pm (UTC)") == "usage_limit"
    assert signals.run_cause("unexpected status 401 Unauthorized: Missing bearer, url: https://x/y") == "auth"
    assert signals.cap_cause("company cap USD / 24 h reached ($50.99 of $50.00)") == "company"
    assert signals.cap_cause("budget runs / 24 h reached (30 běhů of 30 běhů)") == "agent_runs_day"
    assert smoke.stable_path("http://web/assets/index-Bx81kq2a.js") == "/assets/index.js"


# ------------------------------------------------------------------ the digest

def test_signal_digest_aggregates_windows_trends_and_examples(conn, owner, company):
    se, cto = company["Software Engineer"], company["cto"]
    # Run errors: 3 timeouts this day, 1 three days ago, 2 last week; a silent worker; caps.
    ids = [_run(conn, se, "error", "timeout after 60s", NOW - timedelta(hours=h)) for h in (1, 2, 3)]
    _run(conn, se, "error", "timeout after 90s", NOW - timedelta(days=3))
    for d in (8, 9):
        _run(conn, se, "error", "timeout after 60s", NOW - timedelta(days=d))
    _run(conn, cto, "error", "worker went silent for 5 min", NOW - timedelta(days=3))
    _run(conn, cto, "blocked", "company cap USD / 24 h reached ($50.99 of $50.00); only the owner raises it")
    _run(conn, se, "error", "error_max_budget_usd")
    for _ in range(4):
        _run(conn, se, "ok")
    # Tool refusals: 6 this week (the same cause with different wording of the value), 1 the week before.
    for h in range(6):
        _audit(conn, se, "mcp:update_task:refused", NOW - timedelta(hours=5 + h * 20),
               {"reason": "follow_up must be YYYY-MM-DD"}, run_id=ids[0])
    _audit(conn, se, "mcp:update_task:refused", NOW - timedelta(days=10), {"reason": "follow_up must be YYYY-MM-DD"})
    # A loop: one task claimed 6 times; a deploy rejected at merge twice.
    t = tasks.create(conn, owner, {"title": "Smyčka", "assignee": "Software Engineer"})
    for h in range(6):
        _audit(conn, se, "claim", NOW - timedelta(hours=h + 1), entity="task", entity_id=t["id"])
    _deploy(conn, "rejected", NOW - timedelta(days=1), "merge")
    _deploy(conn, "rejected", NOW - timedelta(days=2), "merge")
    _deploy(conn, "ok", NOW - timedelta(days=1))
    # A review waiting past the SLA (a state, not an event).
    r = tasks.create(conn, owner, {"title": "Čeká na revizi", "assignee": "Software Engineer"})
    conn.execute("UPDATE tasks SET status = 'review', updated_at = ? WHERE id = ?", (iso(NOW - timedelta(days=3)), r["id"]))
    conn.commit()

    rows = {x["key"]: x for x in signals.compute(conn, NOW)}
    to = rows["run_error:task.timeout"]
    assert (to["count_24h"], to["prev_24h"], to["count_7d"], to["prev_7d"]) == (3, 0, 4, 2)
    assert to["examples"][:3] == [f"run:{i}" for i in reversed(ids)] and to["sample"] == "timeout after 60s"
    assert rows["run_error:task.worker_silent"]["count_7d"] == 1
    assert rows["cost_cap:company"]["count_24h"] == 1 and rows["cost_cap:run_usd"]["count_24h"] == 1
    assert "run_error:task.max_budget" not in rows and not any(k.startswith("run_error:task.error") for k in rows)
    te = rows["tool_error:update_task.follow_up"]
    assert te["count_7d"] == 6 and te["prev_7d"] == 1 and te["category"] == "tool_error"
    assert rows["loop:claim_repeat"]["count_7d"] == 1 and rows["loop:claim_repeat"]["examples"] == [tasks.display_id(t["id"])]
    assert rows["deploy_rejected:merge"]["count_7d"] == 2 and rows["deploy_rejected:merge"]["count_24h"] == 1
    assert rows["stuck:review"]["count_7d"] == 1 and rows["stuck:review"]["examples"] == [tasks.display_id(r["id"])]
    assert rows["agent_fail:software_engineer"]["count_7d"] == 5  # 4 timeouts + the per-run cap

    # Stored once a day, idempotent; the newest digest and a key's value are readable.
    out = signals.daily(conn, NOW)
    assert out["signals"] == len(rows)
    signals.daily(conn, NOW)
    day = signals.day_of(NOW)
    assert len(signals.stored(conn, day)) == len(rows)
    assert signals.latest(conn)[0] == day
    assert signals.value(conn, "tool_error:update_task.follow_up", day) == 6
    assert signals.value(conn, "tool_error:nothing.here", day) == 0  # absent from an existing digest
    assert signals.value(conn, "tool_error:update_task.follow_up", "2020-01-01") is None  # no digest

    # Ranking: impact × trend; spend and per-agent rows are information only.
    top = signals.ranked(list(rows.values()))
    assert top[0]["score"] >= top[-1]["score"] > 0
    assert not any(x["category"] in ("spend", "agent_fail") for x in top)
    assert "`tool_error:update_task.follow_up` 6/7 d (předtím 1 ↑" in signals.render_line(te)


def test_snapshot_signal_takes_its_previous_value_from_the_stored_digest(conn, owner, company):
    r = tasks.create(conn, owner, {"title": "Stará revize", "assignee": "Software Engineer"})
    conn.execute("UPDATE tasks SET status = 'review', updated_at = ? WHERE id = ?", (iso(NOW - timedelta(days=9)), r["id"]))
    conn.commit()
    signals.daily(conn, NOW - timedelta(days=7))
    r2 = tasks.create(conn, owner, {"title": "Další revize", "assignee": "Software Engineer"})
    conn.execute("UPDATE tasks SET status = 'review', updated_at = ? WHERE id = ?", (iso(NOW - timedelta(days=2)), r2["id"]))
    conn.commit()
    row = {x["key"]: x for x in signals.compute(conn, NOW)}["stuck:review"]
    assert (row["count_7d"], row["prev_7d"], row["prev_24h"]) == (2, 1, None)  # no digest yesterday


# ------------------------------------------------------------------ targets and verification

def _fix_task(conn, owner, title="Opravit follow_up", assignee="Software Engineer", key="tool_error:update_task.follow_up",
              baseline="6", target="0"):
    notes = f"### Důkaz\nrun:1\n\n### Cíl\n- signal: `{key}`\n" + (f"- baseline: {baseline}\n" if baseline else "") \
        + (f"- target: {target}\n" if target else "")
    return tasks.create(conn, owner, {"title": title, "assignee": assignee, "notes": notes})["id"]


def _done(conn, tid, at):
    conn.execute("UPDATE tasks SET status = 'done', completed_at = ?, updated_at = ? WHERE id = ?", (iso(at), iso(at), tid))


def _digest(conn, at, counts: dict):
    rows = [{"key": k, "category": k.split(":")[0], "label": k, "count_24h": v, "prev_24h": None, "count_7d": v,
             "prev_7d": None, "examples": [], "sample": ""} for k, v in counts.items()]
    signals.store(conn, signals.day_of(at), rows)


def test_target_block_parsing_and_registration():
    p = loop.parse_target("text\n### Cíl\n- signal: `tool_error:update_task.follow_up`\n- baseline: 6\n- target: 0\n")
    assert p == {"signal": "tool_error:update_task.follow_up", "baseline": 6.0, "target": 0.0}
    assert loop.parse_target("Signál: deploy_rejected:merge\nVýchozí: 3,5\nCíl: 1") == \
        {"signal": "deploy_rejected:merge", "baseline": 3.5, "target": 1.0}
    assert loop.parse_target("### Cíl\nfronta pod 10") is None
    assert loop.target_block("loop:chat", 4, 0) == "### Cíl\n- signal: `loop:chat`\n- baseline: 4\n- target: 0"
    assert loop.outcome(6, 0, 0) == "improved" and loop.outcome(8, 2, 6) == "improved"  # ≥ 25 % under
    assert loop.outcome(6, 0, 5) == "not_improved" and loop.outcome(6, 0, 9) == "worse"
    assert loop.outcome(1, 0, 2) == "not_improved"  # +1 is noise, not worse
    assert loop.outcome(0, 0, 3) == "worse"


def test_registration_defaults_baseline_from_the_digest(conn, owner, company):
    _digest(conn, NOW, {"loop:chat": 4})
    tid = _fix_task(conn, owner, key="loop:chat", baseline=None, target=None)
    assert loop.register(conn, NOW) == [tid]
    assert loop.register(conn, NOW) == []  # once
    g = loop.targets(conn)[0]
    assert (g["signal_key"], g["baseline"], g["target"], g["status"]) == ("loop:chat", 4.0, 2.0, "open")


def test_verification_improved_after_seven_days_from_deploy(conn, owner, company):
    tid = _fix_task(conn, owner)
    loop.register(conn, NOW)
    done = NOW - timedelta(days=9)
    _done(conn, tid, done)
    _deploy(conn, "ok", done + timedelta(hours=2))
    conn.commit()
    # 5 days after deploy: measuring, no verdict.
    early = done + timedelta(days=5)
    _digest(conn, early, {"tool_error:update_task.follow_up": 3})
    assert loop.verify(conn, early) == {"measuring": 1}
    assert loop.targets(conn)[0]["status"] == "measuring"
    # 8 days after: the digest has no such key any more → 0 → verified.
    _digest(conn, NOW, {"run_error:task.timeout": 2})
    out = loop.verify(conn, NOW)
    assert out == {"verified": [tasks.display_id(tid)]}
    g = loop.targets(conn)[0]
    assert g["status"] == "verified" and g["measured_value"] == 0
    assert "Ověřeno" in conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC",
                                     (tid,)).fetchone()[0]
    assert loop.verify(conn, NOW) == {}  # done once

    # The owner's weekly section: before → after, at most 3 lines; appended to the narrative once.
    sec = loop.packet_section(conn, iso(NOW - timedelta(days=7)), iso(NOW + timedelta(hours=1)))
    assert sec["lines"] == [f"Opravit follow_up ({tasks.display_id(tid)}): chyby nástroje 6 → 0 za týden"]
    text = loop.with_section("## Co se stalo\n- x", sec)
    assert text.endswith("## Samo se zlepšilo tento týden\n- " + sec["lines"][0])
    assert loop.with_section(text, sec) == text
    assert loop.with_section("x", {"lines": []}) == "x"


def test_verification_waits_for_a_deploy_and_reopens_to_the_cto(conn, owner, company):
    tid = _fix_task(conn, owner)
    loop.register(conn, NOW)
    done = NOW - timedelta(days=3)
    _done(conn, tid, done)
    conn.commit()
    _digest(conn, NOW, {"tool_error:update_task.follow_up": 5})
    assert loop.verify(conn, NOW) == {}  # done, no deploy yet: waits
    assert loop.targets(conn)[0]["status"] == "open"
    _deploy(conn, "ok", done + timedelta(days=1))
    conn.commit()
    later = done + timedelta(days=9)
    _digest(conn, later, {"tool_error:update_task.follow_up": 5})
    out = loop.verify(conn, later)
    assert out == {"reopened": [tasks.display_id(tid)]}
    t = conn.execute("SELECT status, assignee_id FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert t["status"] == "next" and t["assignee_id"] == company["cto"]
    body = conn.execute("SELECT body FROM task_comments WHERE task_id = ? ORDER BY id DESC", (tid,)).fetchone()[0]
    assert "6/7 d → teď 5/7 d" in body
    # While it is with the CTO nothing happens; done again → measured again from the new deploy.
    assert loop.verify(conn, later) == {}
    _done(conn, tid, later + timedelta(hours=1))
    _deploy(conn, "ok", later + timedelta(hours=2))
    conn.commit()
    final = later + timedelta(days=8)
    _digest(conn, final, {"tool_error:update_task.follow_up": 1})
    assert loop.verify(conn, final) == {"verified": [tasks.display_id(tid)]}


def test_verification_worse_creates_an_urgent_task_for_the_engineer(conn, owner, company):
    tid = _fix_task(conn, owner, key="deploy_rejected:merge", baseline="3", target="0")
    loop.register(conn, NOW)
    done = NOW - timedelta(days=10)
    _done(conn, tid, done)
    _deploy(conn, "ok", done + timedelta(hours=1), task_id=tid)
    conn.commit()
    _digest(conn, NOW, {"deploy_rejected:merge": 9})
    out = loop.verify(conn, NOW)
    assert out == {"worse": [tasks.display_id(tid)]}
    g = loop.targets(conn)[0]
    f = conn.execute("SELECT * FROM tasks WHERE id = ?", (g["followup_task_id"],)).fetchone()
    assert f["priority"] == 1 and f["assignee_id"] == company["Software Engineer"] and f["source"] == "improve_verify"
    assert "revert" in f["title"] and "signal: `deploy_rejected:merge`" in f["notes"]
    assert f["project_id"] is not None
    # The follow-up carries the block: it is tracked too.
    assert g["followup_task_id"] in loop.register(conn, NOW)


def test_instruction_work_counts_from_completion(conn, owner, company):
    tid = _fix_task(conn, owner, assignee="Performance Coach", key="agent_fail:cto", baseline="4", target="1")
    loop.register(conn, NOW)
    _done(conn, tid, NOW - timedelta(days=8))
    conn.commit()
    _digest(conn, NOW, {"agent_fail:cto": 1})
    assert loop.verify(conn, NOW) == {"verified": [tasks.display_id(tid)]}


# ------------------------------------------------------------------ the weekly tasks

def test_weekly_triage_one_task_for_the_cto_with_dedupe(conn, owner, company):
    se = company["Software Engineer"]
    for h in range(6):
        _audit(conn, se, "mcp:update_task:refused", NOW - timedelta(hours=2 + h), {"reason": "follow_up must be YYYY-MM-DD"})
    _deploy(conn, "rejected", NOW - timedelta(days=1), "tests")
    open_fix = _fix_task(conn, owner, title="Už otevřená oprava", key="deploy_rejected:tests", baseline="1", target="0")
    from pos import platform_loop

    pid = platform_loop.ensure(conn)["project_id"]
    conn.execute("UPDATE tasks SET project_id = ? WHERE id = ?", (pid, open_fix))
    conn.commit()
    loop.daily(conn, NOW)

    out = loop.weekly_triage(conn, NOW)
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (tasks.parse_id(out["task"]),)).fetchone()
    assert t["assignee_id"] == company["cto"] and t["reviewer_id"] == company["cto"] and t["project_id"] == pid
    assert t["title"] == "Samozlepšení: týdenní triáž · 2026-W41" and t["source"] == "improve_triage"
    assert "`tool_error:update_task.follow_up` 6/7 d" in t["notes"]
    assert "Už otevřená oprava" in t["notes"] and "`deploy_rejected:tests`" in t["notes"]
    assert "- signal: `<klíč signálu odsud>`" in t["notes"]
    # Once a week, and not while last week's is open.
    assert "skipped" in loop.weekly_triage(conn, NOW)
    assert "skipped" in loop.weekly_triage(conn, NOW + timedelta(days=7))
    _done(conn, t["id"], NOW + timedelta(days=1))
    _audit(conn, se, "mcp:update_task:refused", NOW + timedelta(days=6), {"reason": "follow_up must be YYYY-MM-DD"})
    conn.commit()
    assert "task" in loop.weekly_triage(conn, NOW + timedelta(days=7))  # the stale digest is recomputed first


def test_weekly_triage_skips_without_signals_or_cto(conn, owner, company):
    assert loop.weekly_triage(conn, NOW) == {"skipped": "no signals worth a run"}
    conn.execute("UPDATE actors SET archived_at = ? WHERE id = ?", (iso(NOW), company["cto"]))
    conn.commit()
    assert loop.weekly_triage(conn, NOW) == {"skipped": "no CTO"}


def test_weekly_coach_task_for_the_three_worst_agents(conn, owner, company):
    se, cto, sre, qa = company["Software Engineer"], company["cto"], company["SRE"], company["QA Reviewer"]
    task = tasks.create(conn, owner, {"title": "Nasadit opravu", "assignee": "Software Engineer"})
    for _ in range(3):
        _run(conn, se, "error", "timeout after 60s", task_id=task["id"])
    _run(conn, se, "ok")
    _run(conn, cto, "error", "worker went silent for 5 min")
    for _ in range(9):
        _run(conn, cto, "ok")
    for _ in range(4):
        _audit(conn, sre, "mcp:incident_close:refused", NOW - timedelta(hours=2), {"reason": "incident is not open"})
    _run(conn, qa, "ok")  # healthy: not picked
    conn.commit()
    worst = loop.worst_agents(conn, NOW)
    assert [a["name"] for a in worst] == ["Software Engineer", "CTO", "SRE"]
    out = loop.weekly_coach(conn, NOW)
    t = conn.execute("SELECT * FROM tasks WHERE id = ?", (tasks.parse_id(out["task"]),)).fetchone()
    assert t["assignee_id"] == company["coach"] and t["reviewer_id"] == company["coach"]
    assert "### Software Engineer: úspěšnost 25 %" in t["notes"] and "timeout after 60s" in t["notes"]
    assert "Nasadit opravu" in t["notes"] and "incident_close: incident is not open" in t["notes"]
    assert "propose_instructions" in t["notes"]
    assert "skipped" in loop.weekly_coach(conn, NOW)


def test_jobs_are_scheduled(conn):
    scheduler.seed(conn)
    jobs = {r["action"]: r for r in conn.execute("SELECT * FROM jobs")}
    assert jobs["improve_daily"]["schedule"] == "daily 05:40"
    assert jobs["improve_triage"]["schedule"] == "weekly mon 08:30"
    assert jobs["improve_coach"]["schedule"] == "weekly mon 08:40"
    # Monday 08:30 Prague is 06:30 UTC in October (CEST); the coach 10 minutes later.
    sunday = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    assert scheduler.next_run("weekly mon 08:30", sunday) == datetime(2026, 10, 5, 6, 30, tzinfo=timezone.utc)
    assert scheduler.next_run("daily 05:40", sunday) == datetime(2026, 10, 5, 3, 40, tzinfo=timezone.utc)
    assert {"improve_daily", "improve_triage", "improve_coach"} <= set(scheduler.ACTIONS)


def test_daily_job_through_the_scheduler(conn, owner, company, monkeypatch):
    _run(conn, company["cto"], "error", "timeout after 60s", datetime.now(timezone.utc) - timedelta(hours=1))
    conn.commit()
    scheduler.seed(conn)
    job = conn.execute("SELECT * FROM jobs WHERE action = 'improve_daily'").fetchone()
    out = scheduler.run_job(conn, job)
    assert out["signals"] >= 1 and "run_error:task.timeout=1" in out["top"]
    assert out["smoke"] == "switched off (POS_UX_SMOKE=0)"


def test_weekly_report_carries_the_section(conn, owner, company):
    tid = _fix_task(conn, owner)
    loop.register(conn)
    now = datetime.now(timezone.utc)
    _done(conn, tid, now - timedelta(days=9))
    conn.commit()
    _digest(conn, now, {"loop:chat": 1})  # today's digest exists, without the fixed signal
    assert loop.verify(conn, now)["verified"]
    packet = weekly_packet.build(conn, outside=False)
    assert packet["self_improved"]["lines"][0].endswith("6 → 0 za týden")
    _, narrative = weekly.auto_narrative(packet)
    assert "## Samo se zlepšilo tento týden" in narrative


# ------------------------------------------------------------------ the web smoke

def test_smoke_reports_failed_assets_and_slow_pages(monkeypatch):
    html = (b'<html><head><script type="module" src="/assets/index-AbCdEf12.js"></script>'
            b'<link rel="stylesheet" href="/assets/index-ZZZZzzzz.css"><link rel="icon" href="/favicon.ico">'
            b'<script src="https://cdn.example.com/x.js"></script></head></html>')
    calls = []

    def fake(url):
        calls.append(url)
        if url.endswith(".css"):
            return 404, b"", 0.01
        if url.endswith("/m"):
            return 200, html, 2.0
        return 200, html if not url.endswith(".js") else b"js", 0.01

    monkeypatch.setattr(smoke, "fetch", fake)
    monkeypatch.setattr(smoke, "reachable", lambda base: True)
    out = smoke.run("http://web")
    assert out["ran"]
    assert "http://web/favicon.ico" not in calls and not any("cdn.example.com" in c for c in calls)
    assert calls.count("http://web/assets/index-AbCdEf12.js") == 1  # shared assets fetched once
    items = out["items"]
    assert items["ux:failed_request:/assets/index.css"]["count"] == 1
    assert items["ux:slow:/m"]["category"] == "ux"
    monkeypatch.setattr(smoke, "reachable", lambda base: False)
    assert smoke.run("http://web")["ran"] is False


def test_smoke_findings_enter_the_digest(conn):
    items = {"ux:http_error:/m": {"key": "ux:http_error:/m", "category": "ux", "label": "x", "count": 1.0,
                                  "examples": ["/m"], "sample": "HTTP 502 for /m"}}
    signals.daily(conn, NOW, extra=items)
    assert signals.value(conn, "ux:http_error:/m", signals.day_of(NOW)) == 1


# ------------------------------------------------------------------ the real sources: transcripts, command denials

def _transcript(conn, run_id, calls, at):
    from pos import transcripts

    transcripts.ensure_schema(conn)
    conn.execute("INSERT INTO run_transcripts (run_id, engine, session_id, tool_calls, errors, summary, created_at) "
                 "VALUES (?, 'claude', 's', ?, ?, ?, ?)",
                 (run_id, len(calls), sum(1 for c in calls if c.get("error")),
                  json.dumps({"calls": calls, "counts": {}}), iso(at)))


def test_transcript_tool_errors_count_once_per_failed_call_with_a_stable_cause(conn, owner, company):
    se = company["Software Engineer"]
    at = NOW - timedelta(hours=2)
    runs = [_run(conn, se, "ok", at=at) for _ in range(3)]
    err = lambda tool, text: {"tool": tool, "input": "{}", "error": True, "result": text}  # noqa: E731
    for i, rid in enumerate(runs):
        _transcript(conn, rid, [
            {"tool": "mcp__pos__get_task", "input": "{}", "error": False},
            # the same cause in other words (a channel number, a quoted value) is one key
            err("mcp__pos__chat_send", f"Error executing tool chat_send: you are not in #{30 + i}: agents read "
                                       "the channels they belong to"),
            err("Bash", f"Exit code {i + 1}\nModuleNotFoundError: No module named 'x{i}'"),
            # a command the hook denied: a guard signal from the API's audit, not a tool error
            err("Bash", "Tento příkaz není na tvém allow-listu … zavolej request_command_approval(…)"),
            *([err("mcp__pos__update_task", "Error executing tool update_task: follow_up must be YYYY-MM-DD "
                                            "(T-556)")] if i == 0 else []),
        ], at)
    # The refusal the API logged with its reason: the transcript's error of that call is not counted again,
    # and neither are the tool_usage rows of calls the transcript or the audit already counted.
    _audit(conn, se, "mcp:update_task:refused", at, {"reason": "follow_up must be YYYY-MM-DD"}, run_id=runs[0])
    for rid, tool in ((runs[0], "update_task"), (runs[1], "chat_send"), (runs[2], "create_task")):
        conn.execute("INSERT INTO tool_usage (actor_id, run_id, tool, ok, at) VALUES (?, ?, ?, 0, ?)",
                     (se, rid, tool, iso(at)))
    conn.commit()
    rows = {x["key"]: x for x in signals.compute(conn, NOW)}
    assert rows["tool_error:update_task.follow_up"]["count_7d"] == 1
    assert rows["tool_error:chat_send.not_agents_read_channels"]["count_7d"] == 3
    assert rows["tool_error:chat_send.not_agents_read_channels"]["examples"] == [f"run:{r}" for r in reversed(runs)]
    assert rows["tool_error:bash.exit.modulenotfounderror_no_module"]["count_7d"] == 3
    assert "tool_error:chat_send.failed" not in rows and "tool_error:update_task.failed" not in rows
    assert rows["tool_error:create_task.failed"]["count_7d"] == 1  # no better source for that one
    assert not any(k.startswith("tool_error:bash.") and "allow" in k for k in rows)
    assert not any("t_556" in k or "556" in k for k in rows)
    assert signals.tool_name("pos:update_task") == "update_task"
    assert signals.tool_name("mcp__browser__click") == "browser_click"
    assert signals.error_cause("Exit code 1") == "exit"


def test_command_denials_from_the_api_audit_are_guard_signals(conn, owner, company):
    se = company["Software Engineer"]
    at = NOW - timedelta(hours=1)
    for outcome, rule, program in (("cli_denied", None, "docker"), ("cli_denied", None, "docker"),
                                   ("needs_cto", "cto", "git"), ("deny", "U3", "rm")):
        _audit(conn, se, "command_denied", at, {"outcome": outcome, "rule": rule, "program": program,
                                                "command": f"{program} x"}, entity="actor", entity_id=se)
    conn.commit()
    rows = {x["key"]: x for x in signals.compute(conn, NOW)}
    assert rows["guard:command_cli_denied.docker"]["count_24h"] == 2
    assert rows["guard:command_needs_cto.cto"]["count_7d"] == 1
    assert rows["guard:command_deny.u3"]["category"] == "guard"
