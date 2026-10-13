"""sentinel.platform: PersonalOS's deep health every 15 s, the alert thresholds (deep health, 5xx share,
"database is locked"), one alert per incident with a recovery notice, the auto-heal (restart after 90 s of
failing deep health, at most once per 15 min, then escalation) and the incident log. prod 2026-10-13."""

import json
import urllib.error
from io import BytesIO

import pytest

from sentinel import detect, pos as posmod
from sentinel.incidents import Obs
from sentinel.store import Store

API = "personalos-api-1"


class Probe:
    """GET /api/health/deep, scripted: ok / degraded / numbers of the last minute."""

    def __init__(self):
        self.ok = True
        self.lm = {"requests": 100, "errors_5xx": 0, "rate_5xx": 0.0, "p95_ms": 40, "db_locked": 0, "long_tx": 0}
        self.calls = 0

    def __call__(self, url, timeout=8.0, want_json=False, **_):
        self.calls += 1
        assert url == "http://pos/api/health/deep" and want_json
        if self.ok:
            return {"ok": True, "status": 200, "ms": 12, "json": {"status": "ok", "last_minute": dict(self.lm)}}
        return {"ok": False, "status": 503, "ms": 2100, "detail": "HTTP 503",
                "json": {"status": "degraded", "reason": "database round-trip failed: OperationalError: database is "
                                                        "locked", "last_minute": dict(self.lm)}}


@pytest.fixture
def plat(sen, clock):
    probe = Probe()
    sen.platform.probe = probe
    sen.docker.add(API)
    return sen, probe, clock


def _run(sen, clock, seconds, step=15):
    """Probe every `step` s for `seconds`; the actions taken."""
    acts = []
    for _ in range(int(seconds // step)):
        clock.advance(step)
        acts += sen.platform_tick()["actions"]
    return acts


def _levels(sen):
    return [a["level"] for a in sen.pos.alerts]


# ------------------------------------------------------------------ thresholds

def test_healthy_says_nothing(plat):
    sen, probe, clock = plat
    _run(sen, clock, 300)
    assert sen.pos.alerts == [] and sen.docker.restarted == []
    assert sen.s.one("SELECT COUNT(*) AS n FROM incidents")["n"] == 0


def test_one_failed_probe_is_a_blip_two_in_a_row_alert_once(plat):
    sen, probe, clock = plat
    probe.ok = False
    _run(sen, clock, 15)
    assert sen.pos.alerts == []          # one blip
    probe.ok = True
    _run(sen, clock, 15)
    probe.ok = False
    _run(sen, clock, 30)                 # two in a row
    assert _levels(sen) == ["alert"]
    a = sen.pos.alerts[0]
    assert a["ref"].startswith("sentinel:") and "deep health selhal 2x" in a["title"] and a["severity"] == "critical"
    _run(sen, clock, 45)
    assert _levels(sen) == ["alert"]     # one alert per incident


def test_5xx_share_over_the_threshold_alerts_without_a_restart(plat):
    sen, probe, clock = plat
    probe.lm.update(requests=200, errors_5xx=4, rate_5xx=0.02)
    _run(sen, clock, 30)
    assert sen.pos.alerts == []          # under both thresholds
    probe.lm.update(requests=240, errors_5xx=20, rate_5xx=0.083)  # the handoff polls dilute the share
    _run(sen, clock, 15)
    assert _levels(sen) == ["alert"] and "20 z 240" in sen.pos.alerts[0]["title"]
    _run(sen, clock, 300)
    assert sen.docker.restarted == []    # the database answers: no restart for 5xx alone


def test_database_locked_per_minute_from_the_deep_health_or_the_logs(plat):
    sen, probe, clock = plat
    probe.lm["db_locked"] = 2
    _run(sen, clock, 15)
    assert sen.pos.alerts == []
    probe.lm["db_locked"] = 0
    now = clock()
    lines = [(now - 5 + i, "sqlite3.OperationalError: database is locked") for i in range(3)]
    detect.ingest_logs(sen.s, sen.t, "personalos", API, lines, now)  # the tick counts the API's log lines
    _run(sen, clock, 15)
    assert _levels(sen) == ["alert"] and "3x 'database is locked'" in sen.pos.alerts[0]["title"]


def test_the_platform_incident_never_takes_the_generic_events_path(plat):
    sen, probe, clock = plat
    probe.ok = False
    _run(sen, clock, 30)
    out = sen.process([], clock())
    assert out["notified"] == [] and sen.pos.events == []
    assert sen.inc.resolve_quiet(clock() + 7200) == []  # resolved only by its own watch


# ------------------------------------------------------------------ auto-heal

def test_restart_after_90s_of_failing_deep_health_then_recovery(plat):
    sen, probe, clock = plat
    probe.ok = False
    sen.docker.lines[API] = [(clock() + 5, "INFO: GET /api/auth/me 500"),
                             (clock() + 6, "sqlite3.OperationalError: database is locked"),
                             (clock() + 7, "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")]
    acts = _run(sen, clock, 75)          # failing since the first probe: 60 s so far
    assert sen.docker.restarted == [] and "restarted" not in acts
    acts = _run(sen, clock, 30)          # past 90 s
    assert sen.docker.restarted == [API] and "restarted" in acts
    assert _levels(sen) == ["alert", "healed"]
    healed = sen.pos.alerts[1]
    assert "database is locked" in healed["logs"] and "abcdefghijklmnopqrstuvwxyz0123456789" not in healed["logs"]
    rem = sen.s.one("SELECT * FROM remediations WHERE container = ?", API)
    assert rem["ok"] == 1 and rem["reason"].startswith("platform:")
    logs = list((sen.cfg and __import__("pathlib").Path(sen.cfg["state_dir"]) / "incident-log").glob("*.log"))
    assert len(logs) == 1 and "database is locked" in logs[0].read_text(encoding="utf-8")
    notes = json.loads(sen.inc.get(sen.platform.state()["incident"])["notes"])
    assert any("restarted personalos-api-1" in n["text"] for n in notes)
    # the restart worked: healthy for 2 minutes → resolved, recovery notice, once
    probe.ok = True
    _run(sen, clock, 105)
    assert _levels(sen) == ["alert", "healed"]
    _run(sen, clock, 30)
    assert _levels(sen) == ["alert", "healed", "recovered"]
    assert sen.s.one("SELECT status FROM incidents WHERE kind = 'platform'")["status"] == "resolved"
    _run(sen, clock, 120)
    assert _levels(sen) == ["alert", "healed", "recovered"]


def test_a_restart_that_did_not_help_escalates_once(plat):
    sen, probe, clock = plat
    probe.ok = False
    _run(sen, clock, 105)
    assert sen.docker.restarted == [API]
    _run(sen, clock, 105)
    assert "escalated" not in _levels(sen)
    _run(sen, clock, 30)                 # 120 s after the restart, still failing
    assert _levels(sen) == ["alert", "healed", "escalated"]
    _run(sen, clock, 600)
    assert sen.docker.restarted == [API] and _levels(sen).count("escalated") == 1


def test_at_most_one_restart_per_15_minutes_then_escalation(plat):
    sen, probe, clock = plat
    probe.ok = False
    _run(sen, clock, 105)
    assert sen.docker.restarted == [API]
    probe.ok = True                      # it helped ...
    _run(sen, clock, 150)
    assert _levels(sen)[-1] == "recovered"
    probe.ok = False                     # ... for 5 minutes: a new incident
    _run(sen, clock, 120)
    assert sen.docker.restarted == [API]  # within 15 min of the last restart: none
    assert _levels(sen)[-2:] == ["alert", "escalated"]
    assert "15 min" in sen.pos.alerts[-1]["title"]
    _run(sen, clock, 720)                # still failing, 15 min after the first restart has passed
    assert sen.docker.restarted == [API, API]


def test_a_runbook_restart_counts_for_the_rate_limit(plat):
    sen, probe, clock = plat
    sen.s.x("INSERT INTO remediations (container, at, reason, incident_id, ok) VALUES (?, ?, 'health', 1, 1)",
            API, clock() - 300)
    probe.ok = False
    _run(sen, clock, 120)
    assert sen.docker.restarted == [] and "escalated" in _levels(sen)


def test_a_refused_restart_is_recorded_and_escalated(plat):
    sen, probe, clock = plat
    from sentinel.docker import DockerError

    sen.docker.restart_error = DockerError("POST /containers/personalos-api-1/restart: 403 b'Forbidden'")
    probe.ok = False
    _run(sen, clock, 105)
    assert _levels(sen) == ["alert", "healed", "escalated"]
    assert "403" in sen.pos.alerts[1]["title"] and "403" in sen.pos.alerts[2]["title"]
    assert sen.s.one("SELECT ok FROM remediations WHERE container = ?", API)["ok"] == 0


def test_the_watch_survives_a_sentinel_restart(plat, cfg, clock):
    sen, probe, _ = plat
    probe.ok = False
    _run(sen, clock, 60)
    from sentinel.core import Sentinel

    again = Sentinel(cfg, store=sen.s, docker=sen.docker, pos=sen.pos, clock=clock)
    again.platform.probe = probe
    _run(again, clock, 45)               # 105 s of failing deep health in all
    assert sen.docker.restarted == [API] and _levels(sen) == ["alert", "healed"]


def test_the_probe_runs_every_15_seconds(plat):
    sen, probe, clock = plat
    assert sen.platform.due(clock())
    sen.platform_tick()
    assert not sen.platform.due(clock() + 10) and sen.platform.due(clock() + 15)
    st = sen.status()
    assert st["platform"]["fails"] == 0 and st["platform"]["last"]["deep_ok"] is True


# ------------------------------------------------------------------ delivery to PersonalOS

class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError("http://pos/api/sentinel/alert", code, "x", {}, BytesIO(json.dumps(body).encode()))


def test_an_alert_waits_in_the_outbox_and_the_retry_says_the_owner_has_it(monkeypatch):
    s = Store(":memory:")
    p = posmod.Pos("http://pos", "t" * 32, s)
    sent = []

    def fake_urlopen(req, timeout=10):
        sent.append(json.loads(req.data))
        if len(sent) == 1:  # the owner got the e-mail, the database was locked
            raise _http_error(503, {"owner": "email", "owner_done": True, "recorded": False})
        return _Resp(b'{"recorded": true}')

    monkeypatch.setattr(posmod, "urlopen", fake_urlopen)
    out = p.alert({"ref": "sentinel:x-1", "level": "alert", "title": "t"})
    assert out["queued"] and p.pending() == 1
    assert p.flush() == 1 and p.pending() == 0
    assert sent[1]["owner_done"] is True and "owner_done" not in sent[0]


def test_alerts_go_before_older_events(monkeypatch):
    s = Store(":memory:")
    p = posmod.Pos("http://pos", "t" * 32, s)
    monkeypatch.setattr(posmod, "urlopen", lambda req, timeout=10: (_ for _ in ()).throw(OSError("down")))
    p.send("/api/events", {"ref": "e1"})
    p.alert({"ref": "a1", "level": "alert"})
    order = []
    monkeypatch.setattr(posmod, "urlopen", lambda req, timeout=10: order.append(req.full_url) or _Resp(b"{}"))
    p.flush()
    assert order == ["http://pos/api/sentinel/alert", "http://pos/api/events"]


def test_an_authorization_header_loses_its_token_in_the_logs():
    from sentinel.redact import redact

    out = redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    assert "abcdefghijklmnopqrstuvwxyz" not in out and "<redacted>" in out


def test_lock_lines_are_counted_per_minute(sen, clock):
    now = clock()
    detect.ingest_logs(sen.s, sen.t, "personalos", API,
                       [(now - 1, "sqlite3.OperationalError: database is locked"), (now - 1, "fine")], now)
    m = int(now // 60)
    assert sen.s.stat_sum(API, "locked", m, m + 1) == 1


def test_obs_kind_platform_is_self_managed():
    from sentinel import incidents

    assert "platform" in incidents.SELF_MANAGED and Obs("a", "platform", "k", "high", "t").kind == "platform"
