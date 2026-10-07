from sentinel import api, detect, runbook
from sentinel.incidents import Incidents, Obs
from sentinel.store import Store


def _err(sen, container, text, n, at):
    sen.docker.lines.setdefault(container, []).extend((at + i * 0.001, text) for i in range(n))


# ------------------------------------------------------------------ dedupe and escalation

def test_one_open_incident_absorbs_and_escalates_only_on_severity_or_tenfold(cfg, clock):
    s = Store(":memory:")
    inc = Incidents(s, cfg["thresholds"], clock)
    o = Obs("nexus", "new_error", "fp1", "medium", "boom", 5)
    iid, what = inc.observe(o)
    assert what == "opened"
    assert [k for k, _ in inc.due()] == ["incident"]
    inc.mark_notified(iid)
    assert inc.due() == []
    for _ in range(3):
        assert inc.observe(Obs("nexus", "new_error", "fp1", "medium", "boom", 5)) == (iid, "absorbed")
    assert inc.get(iid)["count"] == 20 and inc.due() == []           # 4× the notified count: quiet
    inc.observe(Obs("nexus", "new_error", "fp1", "medium", "boom", 40))
    assert [k for k, _ in inc.due()] == ["incident_escalated"]      # 60 ≥ 10 × 5
    inc.mark_notified(iid)
    inc.observe(Obs("nexus", "new_error", "fp1", "high", "boom worse", 1))
    assert [k for k, _ in inc.due()] == ["incident_escalated"]      # severity up
    inc.mark_notified(iid)
    assert inc.get(iid)["notified_level"] == 2
    # another key is another incident
    assert inc.observe(Obs("nexus", "new_error", "fp2", "medium", "other", 5))[1] == "opened"


def test_auto_resolve_after_quiet_period(cfg, clock):
    s = Store(":memory:")
    inc = Incidents(s, cfg["thresholds"], clock)
    iid, _ = inc.observe(Obs("host", "swap", "swap", "high", "swap 99%"))
    hid, _ = inc.observe(Obs("personalos", "health", "personalos-api", "high", "down"))
    clock.advance(11 * 60)
    assert [r["id"] for r in inc.resolve_quiet()] == [hid]           # health: 10 min
    clock.advance(20 * 60)
    assert [r["id"] for r in inc.resolve_quiet()] == [iid]           # others: 30 min
    # the same problem again is a new incident that knows it recurred
    nid, what = inc.observe(Obs("host", "swap", "swap", "high", "swap 99%"))
    assert what == "opened" and nid != iid and '"recurrences_24h": 1' in inc.get(nid)["detail"]


# ------------------------------------------------------------------ thresholds

def test_health_needs_three_failures_in_a_row(cfg, clock):
    s, t = Store(":memory:"), cfg["thresholds"]
    check = {"name": "nexus-api", "service": "nexus", "url": "http://nexus-api:3001/readyz",
             "restart": "nexus-process-pilot-api-1"}
    bad, good = {"ok": False, "detail": "ConnectionRefusedError"}, {"ok": True, "status": 200}
    assert detect.health(s, t, check, bad, clock()) == []
    assert detect.health(s, t, check, bad, clock()) == []
    assert detect.health(s, t, check, good, clock()) == []          # the streak resets
    detect.health(s, t, check, bad, clock())
    detect.health(s, t, check, bad, clock())
    obs = detect.health(s, t, check, bad, clock())
    assert len(obs) == 1 and obs[0].kind == "health" and obs[0].container == "nexus-process-pilot-api-1"


def test_tls_and_host_thresholds(cfg, clock):
    s, t = Store(":memory:"), cfg["thresholds"]
    check = {"name": "personalos-web", "service": "personalos", "url": "https://personalos.obseum.cz/"}
    assert detect.health(s, t, check, {"ok": True, "tls_days": 40}, clock()) == []
    assert [o.severity for o in detect.health(s, t, check, {"ok": True, "tls_days": 10}, clock())] == ["medium"]
    assert [o.severity for o in detect.health(s, t, check, {"ok": True, "tls_days": 3}, clock())] == ["high"]
    obs = detect.host(t, {"swap_pct": 99.7, "mem_avail_pct": 45, "load5": 3.3, "cpus": 4, "disk_pct": {"/": 63}})
    assert [o.kind for o in obs] == ["swap"] and obs[0].severity == "high"
    obs = detect.host(t, {"swap_pct": 89.9, "mem_avail_pct": 3, "load5": 13, "cpus": 4, "disk_pct": {"/": 97}})
    assert sorted(o.kind for o in obs) == ["disk", "load", "memory"]


def test_run_failure_ratio_needs_enough_runs_and_more_than_half(cfg):
    t = cfg["thresholds"]
    assert detect.runs(t, "nexus", {"total": 4, "failed": 4}) == []          # too few runs
    assert detect.runs(t, "nexus", {"total": 10, "failed": 5}) == []         # exactly half: not above
    obs = detect.runs(t, "nexus", {"total": 15, "failed": 12, "detail": {"last_reasons": ["usage limit"]}})
    assert obs[0].kind == "run_failures" and obs[0].severity == "high" and obs[0].detail["ratio"] == 0.8
    assert detect.runs(t, "nexus", {"total": 59, "failed": 59})[0].severity == "critical"


def test_push_failures_open_an_incident_only_when_nothing_gets_through(cfg):
    t = cfg["thresholds"]
    assert detect.push(t, None) == [] and detect.push(t, {"push": None}) == []
    ok = {"failed_24h": 5, "client_errors_24h": 0, "ok_devices_24h": 1, "owner_devices": 2}
    assert detect.push(t, {"push": ok}) == []                                   # one device still gets them
    obs = detect.push(t, {"push": {**ok, "ok_devices_24h": 0}})
    assert obs[0].kind == "push_failures" and obs[0].severity == "high" and obs[0].detail["failed_24h"] == 5
    assert detect.push(t, {"push": {**ok, "failed_24h": 2, "ok_devices_24h": 0}}) == []   # below the minimum
    sub = detect.push(t, {"push": {"failed_24h": 0, "client_errors_24h": 1, "ok_devices_24h": 0, "owner_devices": 0}})
    assert sub[0].key == "subscribe" and sub[0].severity == "medium"


def test_new_fingerprint_needs_a_meaningful_rate_and_spike_needs_baseline(cfg, clock):
    s, t = Store(":memory:"), cfg["thresholds"]
    now = clock()
    line = "ERROR worker crashed: KeyError 'x'"
    assert detect.ingest_logs(s, t, "nexus", "c", [(now, line)] * 3, now) == []      # 3 < 5
    obs = detect.ingest_logs(s, t, "nexus", "c", [(now, line)] * 2, now)
    assert [o.kind for o in obs] == ["new_error"] and obs[0].count == 5
    # a known fingerprint with a steady baseline of ~1/min
    other = "ERROR slow query took 1200 ms"
    for m in range(180, 0, -1):
        detect.ingest_logs(s, t, "nexus", "c", [(now - 60 * m, other)], now - 60 * m, learning=True)
    assert detect.ingest_logs(s, t, "nexus", "c", [(now, other)] * 4, now) == []      # 4/5 min: normal
    obs = detect.ingest_logs(s, t, "nexus", "c", [(now, other)] * 60, now)
    assert [o.kind for o in obs] == ["error_spike"] and obs[0].detail["baseline_per_min"] <= 1.1


def test_status_code_rules(cfg, clock):
    s, t = Store(":memory:"), cfg["thresholds"]
    now = clock()
    ok = 'INFO: 1.2.3.4:1 - "POST /chat/completions HTTP/1.1" 200 OK'
    e429 = '{"message": "1.2.3.4:1 - \\"POST /chat/completions HTTP/1.1\\" 429", "level": "INFO"}'
    quota = "litellm.RateLimitError: You've hit your usage limit"
    e401 = 'INFO: 1.2.3.4:1 - "GET /mcp HTTP/1.1" 401 Unauthorized'
    lines = [(now, ok)] * 50 + [(now, e429)] * 25 + [(now, quota)] * 3 + [(now, e401)] * 120
    kinds = sorted(o.kind for o in detect.ingest_logs(s, t, "litellm", "litellm", lines, now))
    assert kinds == ["auth_flood", "quota", "rate_limited"]


def test_learning_counts_but_opens_nothing(sen, clock):
    sen.s.set_meta("created_at", clock())
    sen.docker.add("nexus-process-pilot-api-1")
    _err(sen, "nexus-process-pilot-api-1", "ERROR boom 1", 50, clock() - 10)
    sen.tick()
    assert sen.s.one("SELECT COUNT(*) AS n FROM incidents")["n"] == 0
    assert sen.s.one("SELECT total FROM fp")["total"] == 50


# ------------------------------------------------------------------ runbook

def test_runbook_restarts_allowlisted_once_per_cooldown(sen, cfg, clock):
    s, inc = sen.s, sen.inc
    iid, _ = inc.observe(Obs("nexus", "health", "nexus-api", "high", "down", container="nexus-process-pilot-api-1"))
    assert runbook.maybe_restart(s, inc, cfg, iid, sen.docker) == "restarted"
    assert sen.docker.restarted == ["nexus-process-pilot-api-1"]
    assert inc.due() == []                                     # held for the grace period
    inc.resolve(iid, "test")
    jid, _ = inc.observe(Obs("nexus", "health", "nexus-api", "high", "down", container="nexus-process-pilot-api-1"))
    assert runbook.maybe_restart(s, inc, cfg, jid, sen.docker) == "cooldown"
    assert [k for k, _ in inc.due()] == ["incident"]           # no second restart: escalate now
    clock.advance(31 * 60)
    inc.resolve(jid, "test")
    kid, _ = inc.observe(Obs("nexus", "health", "nexus-api", "high", "down", container="nexus-process-pilot-api-1"))
    assert runbook.maybe_restart(s, inc, cfg, kid, sen.docker) == "restarted"
    # databases are never on the list, other kinds are not remediable
    pid, _ = inc.observe(Obs("nexus", "health", "pg", "high", "down", container="nexus-process-pilot-postgres-1"))
    assert runbook.maybe_restart(s, inc, cfg, pid, sen.docker) == "not_allowed"
    qid, _ = inc.observe(Obs("host", "swap", "swap", "high", "swap"))
    assert runbook.maybe_restart(s, inc, cfg, qid, sen.docker) == "not_remediable"
    assert len(sen.docker.restarted) == 2


def test_runbook_starts_deployer_stopped_by_its_deploy(sen, cfg):
    s, inc = sen.s, sen.inc
    down = dict(service="personalos", kind="container_down", key="personalos-deployer-1", severity="high",
                title="personalos-deployer-1 is exited (exit 143)", container="personalos-deployer-1")
    iid, _ = inc.observe(Obs(**down, detail={"status": "exited", "exit_code": 143}))
    assert runbook.maybe_restart(s, inc, cfg, iid, sen.docker) == "restarted"
    assert sen.docker.restarted == ["personalos-deployer-1"]
    inc.resolve(iid, "test")
    # a crash (exit 1) is not started blindly; nor is a container off the start list
    jid, _ = inc.observe(Obs(**down, detail={"status": "exited", "exit_code": 1}))
    assert runbook.maybe_restart(s, inc, cfg, jid, sen.docker) == "not_remediable"
    kid, _ = inc.observe(Obs(**{**down, "key": "pg", "container": "personalos-postgres-1"},
                             detail={"status": "exited", "exit_code": 143}))
    assert runbook.maybe_restart(s, inc, cfg, kid, sen.docker) == "not_remediable"
    assert sen.docker.restarted == ["personalos-deployer-1"]


def test_restart_that_fixes_it_never_reaches_personalos(sen, clock):
    check = {"name": "nexus-api", "service": "nexus", "url": "http://x", "restart": "nexus-process-pilot-api-1"}
    for _ in range(3):
        sen.process(detect.health(sen.s, sen.t, check, {"ok": False, "detail": "refused"}, clock()), clock())
    assert sen.docker.restarted == ["nexus-process-pilot-api-1"] and sen.pos.events == []
    clock.advance(60)
    sen.process(detect.health(sen.s, sen.t, check, {"ok": True, "status": 200}, clock()), clock())
    clock.advance(11 * 60)
    out = sen.process([], clock())
    assert out["resolved"] and sen.pos.events == []
    row = sen.s.one("SELECT * FROM incidents")
    assert row["status"] == "resolved" and "restarted" in row["notes"]


def test_restart_that_does_not_fix_it_escalates_after_the_grace(sen, clock):
    check = {"name": "nexus-api", "service": "nexus", "url": "http://x", "restart": "nexus-process-pilot-api-1"}
    for _ in range(3):
        sen.process(detect.health(sen.s, sen.t, check, {"ok": False, "detail": "refused"}, clock()), clock())
        clock.advance(60)
    assert sen.pos.events == []
    for _ in range(3):  # the grace (150 s) ends; it still fails
        sen.process(detect.health(sen.s, sen.t, check, {"ok": False, "detail": "refused"}, clock()), clock())
        clock.advance(60)
    assert [e["kind"] for e in sen.pos.events] == ["incident"]
    assert "runbook: restarted nexus-process-pilot-api-1" in sen.pos.events[0]["body"]


# ------------------------------------------------------------------ the whole tick

def test_tick_turns_an_error_flood_into_one_event_with_a_compact_packet(sen, clock):
    sen.docker.add("nexus-process-pilot-runtime-1")
    secret_line = ("ERROR run 991 failed for jana@firma.cz: litellm.RateLimitError key sk-live-ABCDEFGH12345678 "
                   "(request 3f9c1e2a-1b2c-4d5e-8f90-123456789abc)")
    for i in range(10_000):
        sen.docker.lines.setdefault("nexus-process-pilot-runtime-1", []).append(
            (clock() - 30 + i * 0.001, secret_line.replace("991", str(i))))
    out = sen.tick()
    assert len(out["opened"]) == 1 and len(sen.pos.events) == 1
    ev = sen.pos.events[0]
    assert ev["source"] == "sentinel" and ev["kind"] == "incident"
    assert ev["ref"] == f"sentinel:{sen.instance}-{out['opened'][0]}#0"
    inc = ev["data"]["incident"]
    assert inc["count"] == 10_000 and inc["service"] == "nexus" and inc["kind"] == "new_error"
    body = ev["body"]
    assert "jana@firma.cz" not in body and "sk-live-ABCDEFGH" not in body and "<email>" in body
    assert body.count("RateLimitError") <= 20 and len(body) < 6500           # compact, never raw logs
    # the same flood next minute is absorbed, not a second event
    for i in range(500):
        sen.docker.lines["nexus-process-pilot-runtime-1"].append((clock() + 30 + i * 0.001, secret_line))
    clock.advance(60)
    sen.tick()
    assert len(sen.pos.events) == 1
    assert sen.s.one("SELECT count FROM incidents")["count"] == 10_500
    # quiet for 30 min: resolved, and PersonalOS hears it
    clock.advance(31 * 60)
    sen.tick()
    assert [e["kind"] for e in sen.pos.events] == ["incident", "incident_resolved"]
    assert sen.pos.events[1]["ref"].endswith("#resolved")


def test_personalos_down_keeps_events_and_raises_the_fallback(sen, clock, tmp_path):
    sen.pos.up = False
    sen.docker.add("personalos-api-1")
    _err(sen, "personalos-api-1", "ERROR sqlite3.OperationalError: database is locked", 20, clock() - 5)
    for _ in range(3):
        sen.tick()
        clock.advance(60)
    assert sen.pos.pending() == 1 and sen.pos_down_since
    alert = (tmp_path / "ALERT-personalos-down.md").read_text(encoding="utf-8")
    assert "PersonalOS is down" in alert and "database is locked" in alert
    sen.pos.up = True
    sen.tick()
    assert len(sen.pos.events) == 1 and sen.pos_down_since is None
    assert not (tmp_path / "ALERT-personalos-down.md").exists()


def test_container_rules(sen, clock):
    sen.docker.add("kb-kb-1", restarts=0)
    sen.docker.add("litellm-postgres", status="exited", exit_code=137)
    sen.docker.add("nexus-process-pilot-clamav-1", status="exited", exit_code=0)  # a clean exit is not an outage
    sen.docker.add("nexus-process-pilot-migrate-1", status="exited", exit_code=0, health="unhealthy")  # a finished one-off
    for i in range(4):
        sen.docker.items["kb-kb-1"]["restarts"] = i
        sen.docker.items["kb-kb-1"]["started"] = f"2026-09-26T08:0{i}:00Z"
        sen.tick()
        clock.advance(60)
    kinds = {(r["kind"], r["key"]) for r in sen.s.q("SELECT kind, key FROM incidents")}
    assert kinds == {("restart_loop", "kb-kb-1"), ("container_down", "litellm-postgres")}


def test_inject_hook_and_log_reads(sen, clock):
    out = api.inject(sen, {"line": "ERROR SyntheticTestError: e2e check 42", "count": 30})
    assert out["incident"] and len(sen.pos.events) == 1
    assert sen.pos.events[0]["data"]["incident"]["service"] == "sentinel-test"
    got = api.read_logs(sen, "sentinel-test-app", clock(), 10, fingerprint=out["fingerprint"])
    assert got["source"] == "samples" and got["lines"]
    # a real container: filtered, deduplicated, redacted and capped
    sen.docker.add("kb-kb-1")
    sen.tick()
    now = clock()
    sen.docker.lines["kb-kb-1"] = ([(now - 100 + i, "ERROR ingest failed for petr@acme.cz: Timeout") for i in range(50)]
                                   + [(now - 40, 'INFO: 1.2.3.4:1 - "GET /api/health HTTP/1.1" 200 OK')]
                                   + [(now - 30 + i, f"ERROR other {i} token=abcd1234") for i in range(200)])
    r = api.read_logs(sen, "kb-kb-1", now, 10, limit=500)
    assert r["lines"][0].endswith("(×50)") and "petr@acme.cz" not in r["lines"][0]
    assert "abcd1234" not in " ".join(r["lines"]) and len(r["lines"]) <= api.MAX_LIMIT
    assert all("/api/health" not in x for x in r["lines"])
    g = api.read_logs(sen, "kb-kb-1", now, 60, grep="health", errors_only=True)
    assert len(g["lines"]) == 1


# ------------------------------------------------------------------ quota with a reset time

SHIM = "nexus-process-pilot-codex-shim-1"
SHIM_LINE = ('{"level":50,"time":%d,"msg":"codex shim call failed","err":"Codex CLI usage limit: ERROR: You\'ve hit '
             'your usage limit. Visit https://chatgpt.com/codex/settings/usage to purchase more credits or try again at '
             'Sep 29th, 2026 6:47 AM. (skipping Codex until 2026-09-29T06:48:00.000Z)"}')


def test_quota_reset_is_read_from_the_lines():
    from datetime import datetime, timezone

    from sentinel import fingerprint as fp

    now = datetime(2026, 9, 26, 10, 11, tzinfo=timezone.utc).timestamp()
    at = fp.quota_reset(SHIM_LINE % 0, now)
    assert datetime.fromtimestamp(at, timezone.utc) == datetime(2026, 9, 29, 6, 47, tzinfo=timezone.utc)  # earliest
    worker = ("run for T-046 blocked: no runtime available: codex usage limit until 2026-09-29T06:47:00+00:00; "
              "claude usage limit until 2026-09-26T11:20:00+00:00")
    assert datetime.fromtimestamp(fp.quota_reset(worker, now), timezone.utc).hour == 11
    assert datetime.fromtimestamp(fp.quota_reset("You've hit your session limit · resets 11:20am (UTC)", now),
                                  timezone.utc) == datetime(2026, 9, 26, 11, 20, tzinfo=timezone.utc)
    assert fp.quota_reset("You've hit your usage limit", now) is None
    assert fp.quota_reset("usage limit until 2020-01-01T00:00:00Z", now) is None           # past: no reset


def test_hourly_quota_retries_are_one_ongoing_incident_until_the_reset(sen, clock):
    """2026-09-26: LiteLLM's hourly health check hit the exhausted Codex subscription through the
    shim, 3 lines an hour; the quota incident resolved after 30 quiet minutes and reopened every
    hour (T-051 … T-062). With the reset in the lines it stays one incident until the reset."""
    from datetime import datetime, timezone

    sen.docker.add(SHIM)
    reset = clock() + 5 * 3600  # the line names 2026-09-29; keep the reset close for the test
    iso = datetime.fromtimestamp(reset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    line = SHIM_LINE.replace("2026-09-29T06:48:00.000Z", iso).replace("try again at Sep 29th, 2026 6:47 AM", "later")

    def health_check():
        for i in range(3):
            sen.docker.lines.setdefault(SHIM, []).append((clock() - 20 + i, line % 0))
        sen.tick()

    health_check()
    assert len(sen.pos.events) == 1
    inc = sen.pos.events[0]["data"]["incident"]
    assert inc["kind"] == "quota" and inc["detail"]["quota_until"].startswith(iso[:16])
    assert "quota exhausted until" in sen.pos.events[0]["title"]
    for _ in range(4):  # four more hourly checks, 12 more lines, no new event, no count escalation
        clock.advance(3600)
        health_check()
    assert [e["kind"] for e in sen.pos.events] == ["incident"]
    assert sen.s.one("SELECT COUNT(*) AS n FROM incidents")["n"] == 1
    # past the reset with no new error: resolved by itself, PersonalOS hears it
    clock.advance(reset - clock() + 400)
    sen.tick()
    assert [e["kind"] for e in sen.pos.events] == ["incident", "incident_resolved"]


def test_quota_without_a_reset_keeps_the_quiet_rule(sen, clock):
    sen.docker.add("litellm")
    for i in range(3):
        sen.docker.lines.setdefault("litellm", []).append((clock() - 5 + i, "litellm.RateLimitError: You've hit your usage limit"))
    sen.tick()
    clock.advance(31 * 60)
    sen.tick()
    assert [e["kind"] for e in sen.pos.events] == ["incident", "incident_resolved"]


def test_a_steady_5xx_rate_counts_each_event_once_and_never_escalates(cfg, clock):
    s = Store(":memory:")
    t = cfg["thresholds"]
    inc = Incidents(s, t, clock)
    real, told = 0, []
    for tick in range(20):  # 5 answers 500 a minute: 25 in the 5-minute window (threshold 20)
        now = clock()
        lines = [(now - 60 + i * 12, '1.2.3.4 - - "GET /api/x HTTP/1.1" 500 12') for i in range(5)]
        real += 5
        for o in detect.ingest_logs(s, t, "api", "api", lines, now):
            inc.observe(o, now)
        for kind, row in inc.due(now):
            if row["kind"] == "http_5xx":  # (the 500 lines are also an error fingerprint of their own)
                told.append(kind)
            inc.mark_notified(row["id"], now)
        clock.advance(60)
    row = s.one("SELECT * FROM incidents WHERE kind = 'http_5xx'")
    assert row["count"] == real
    assert told == ["incident"]
    # the rate rises three times: now it escalates
    now = clock()
    lines = [(now - 60 + i * 0.5, '1.2.3.4 - - "GET /api/x HTTP/1.1" 500 12') for i in range(100)]
    for o in detect.ingest_logs(s, t, "api", "api", lines, now):
        inc.observe(o, now)
    assert [k for k, r in inc.due(now) if r["kind"] == "http_5xx"] == ["incident_escalated"]


# ------------------------------------------------------------------ backups

def test_backup_age_thresholds_check_rows_and_metrics(sen, clock, tmp_path, capsys):
    import os

    fresh, stale, old = tmp_path / "fresh", tmp_path / "stale", tmp_path / "old"
    for d, hours in ((fresh, 2), (stale, 30), (old, 50)):
        (d / "2026-09-26").mkdir(parents=True)
        f = d / "2026-09-26" / "db.sqlite.gz"
        f.write_text("x")
        t = clock() - hours * 3600
        for p in (f, f.parent):
            os.utime(p, (t, t))
    sen.cfg["backups"] = {"fresh": str(fresh), "stale": str(stale), "old": str(old),
                          "gone": str(tmp_path / "missing")}
    sen.tick()
    b = sen.backups
    assert b["fresh"]["status"] == "ok" and b["fresh"]["age_h"] == 2.0
    assert b["stale"]["status"] == "warn" and b["old"]["status"] == "fail"
    assert b["gone"] == {"age_h": None, "status": "fail", "detail": "missing"}
    sev = {r["key"]: r["severity"] for r in sen.s.q("SELECT key, severity FROM incidents WHERE kind = 'backup_age'")}
    assert sev == {"stale": "medium", "old": "high", "gone": "high"}
    rows = {r["name"]: r["ok"] for r in sen.s.q("SELECT name, ok FROM checks WHERE service = 'backup'")}
    assert rows == {"backup-fresh": 1, "backup-stale": 0, "backup-old": 0, "backup-gone": 0}
    m = sen.metrics()
    assert 'backup_age_hours{backup="stale"} 30.0' in m and 'backup_age_hours{backup="gone"}' not in m
    assert 'probe_success{app="backup-stale"} 1' in m and 'probe_success{app="backup-old"} 0' in m
    out = capsys.readouterr().out
    assert '"check": "backup"' in out and '"level": "error"' in out


# ------------------------------------------------------------------ sync level error (T-265)

def test_a_steady_sync_error_is_one_incident_even_after_the_monitor_closes_it(sen, clock, monkeypatch):
    from sentinel import checks

    sen.cfg["http"] = [{"name": "knowlage-api", "service": "knowlage", "url": "http://kb/api/health",
                        "json_level": "sync"}]
    monkeypatch.setattr(checks, "http_check", lambda *a, **k: {"ok": True, "status": 200, "level": "error"})
    opened = []
    for i in range(10):
        opened += sen.tick()["opened"]
        if i == 5:  # the Monitor classifies it "transient" and resolves it while it goes on
            sen.inc.resolve(opened[0], "closed by the Monitor agent", classification="transient")
        clock.advance(60)
    assert len(opened) == 1                                             # nothing before 5 checks in a row
    row = sen.inc.get(opened[0])
    assert row["status"] == "open" and row["count"] == 10 and row["severity"] == "low"
    assert sen.s.one("SELECT COUNT(*) AS n FROM incidents WHERE kind = 'sync_error'")["n"] == 1
    # once it is quiet for its quiet period it resolves
    monkeypatch.setattr(checks, "http_check", lambda *a, **k: {"ok": True, "status": 200, "level": "ok"})
    clock.advance(31 * 60)
    assert sen.tick()["resolved"] == [opened[0]]
