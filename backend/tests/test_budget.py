import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from pos.budget import policy, service, store
from pos.budget.codex_usage import parse_exec_jsonl, parse_session_log, thread_id_from_path
from pos.budget.forecast import Sample, billing_period, forecast_window, tokens_per_percent
from pos.config import Settings
from pos.db import connect
from pos.main import create_app

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
THREAD = "0199a213-81c0-7800-8aa1-bbab2a035a53"
WEEK = 7 * 24 * 60


def exec_output(thread=THREAD, turns=((1000, 400, 200),)):
    lines = [json.dumps({"type": "thread.started", "thread_id": thread}), "not json", ""]
    for inp, cached, out in turns:
        lines.append(json.dumps({"type": "turn.started"}))
        lines.append(json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
                      "reasoning_output_tokens": 50},
        }))
    return lines


def token_count(at, total, primary, secondary, resets_primary, resets_secondary):
    return json.dumps({
        "timestamp": at.isoformat().replace("+00:00", "Z"),
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": {"input_tokens": total, "cached_input_tokens": 0,
                                           "output_tokens": 0, "reasoning_output_tokens": 0}},
            "rate_limits": {
                "primary": {"used_percent": primary, "window_minutes": 300, "resets_at": resets_primary},
                "secondary": {"used_percent": secondary, "window_minutes": WEEK,
                              "resets_at": resets_secondary},
                "plan_type": "pro",
            },
        },
    })


def write_session(home: Path, thread=THREAD, events=()):
    day = home / "sessions" / "2026" / "09" / "25"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-25T10-00-00-{thread}.jsonl"
    meta = json.dumps({"timestamp": "2026-09-25T10:00:00Z", "type": "session_meta",
                       "payload": {"id": thread}})
    path.write_text("\n".join([meta, *events]) + "\n")
    return path


# --- parsing -----------------------------------------------------------------


def test_parse_exec_sums_turns_and_billable():
    run = parse_exec_jsonl(exec_output(turns=((1000, 400, 200), (500, 500, 100))))
    assert run.thread_id == THREAD
    assert run.turns == 2
    assert run.usage.input_tokens == 1500
    # non-cached input (600 + 0) + output (300)
    assert run.usage.billable == 900


def test_thread_id_from_rollout_name():
    assert thread_id_from_path(Path(f"rollout-2026-09-25T10-00-00-{THREAD}.jsonl")) == THREAD
    assert thread_id_from_path(Path("other.jsonl")) is None


def test_parse_session_log_reads_limits(tmp_path):
    reset = int((NOW + timedelta(days=3)).timestamp())
    path = write_session(tmp_path, events=[
        token_count(NOW - timedelta(hours=1), 5000, 10, 40, reset - 10_000, reset),
        token_count(NOW, 8000, 20, 42, reset - 10_000, reset),
    ])
    log = parse_session_log(path)
    assert log.thread_id == THREAD
    assert log.usage.input_tokens == 8000
    assert len(log.snapshots) == 2
    secondary = [w for w in log.snapshots[-1].windows if w.name == "secondary"][0]
    assert secondary.used_percent == 42 and secondary.window_minutes == WEEK
    assert log.snapshots[-1].plan_type == "pro"


# --- forecasting -------------------------------------------------------------


def weekly_samples(start_pct, pct_per_hour, hours, reset):
    return [
        Sample(NOW - timedelta(hours=hours - h), start_pct + pct_per_hour * h, reset, WEEK)
        for h in range(hours + 1)
    ]


def test_forecast_on_pace_stays_under_limit():
    reset = NOW + timedelta(days=3)  # window started 4 days ago
    # 40 % after 4 days = 0.4167 %/h average; recent pace the same.
    samples = weekly_samples(40 - 24 * 0.4167, 0.4167, 24, int(reset.timestamp()))
    f = forecast_window("secondary", samples, NOW)
    assert 69 < f.projected_percent < 72
    assert f.exhausted_at is None
    assert policy.assess([f]).level == "ok"


def test_forecast_burst_triggers_throttle():
    reset = NOW + timedelta(days=3)
    samples = weekly_samples(40, 1.5, 24, int(reset.timestamp()))  # 76 % now, 1.5 %/h recently
    f = forecast_window("secondary", samples, NOW)
    assert f.projected_percent > 100
    assert f.exhausted_at is not None and f.exhausted_at < reset
    result = policy.assess([f])
    assert result.level == "throttle"
    assert "týdenní okno" in result.reasons[0]


def test_forecast_ignores_previous_window_and_expired_one():
    old_reset = int((NOW - timedelta(hours=1)).timestamp())
    new_reset = int((NOW + timedelta(hours=4)).timestamp())
    samples = [
        Sample(NOW - timedelta(hours=2), 90, old_reset, 300),
        Sample(NOW - timedelta(minutes=30), 5, new_reset, 300),
        Sample(NOW, 10, new_reset, 300),
    ]
    f = forecast_window("primary", samples, NOW)
    assert f.samples == 2 and f.used_percent == 10
    assert forecast_window("primary", samples[:1], NOW) is None


def test_pause_when_nearly_used_up():
    reset = NOW + timedelta(hours=10)
    f = forecast_window("secondary", [Sample(NOW, 96, int(reset.timestamp()), WEEK)], NOW)
    assert policy.assess([f]).level == "pause"
    assert policy.assess([], limit_reached="rate_limit_reached").level == "pause"


def test_billing_period_calendar_and_renewal_day():
    assert billing_period(NOW) == (datetime(2026, 9, 1, tzinfo=timezone.utc),
                                   datetime(2026, 10, 1, tzinfo=timezone.utc))
    start, end = billing_period(datetime(2026, 12, 5, tzinfo=timezone.utc), renewal_day=10)
    assert (start.month, end.year, end.month) == (11, 2026, 12)


def test_tokens_per_percent():
    assert tokens_per_percent([(10, 0), (20, 1_000_000)]) == 100_000
    assert tokens_per_percent([(10, 0), (10.5, 500)]) is None


# --- policy ------------------------------------------------------------------


def test_caps_follow_level_and_class():
    classes = {"hr": "system", "mail": "normal", "idea": "low"}
    ok = policy.allocate_caps(600_000, classes, "ok")
    assert ok["hr"] > ok["mail"] > ok["idea"] > 0
    throttled = policy.allocate_caps(600_000, classes, "throttle")
    assert throttled["idea"] == 0 and throttled["mail"] > 0
    paused = policy.allocate_caps(600_000, classes, "pause")
    assert paused["mail"] == 0 and paused["hr"] > 0
    assert policy.allocate_caps(None, classes, "throttle") == {"hr": None, "mail": None, "idea": 0}


def test_gate():
    assert policy.gate("ok", "normal", 10, 100).allowed
    assert not policy.gate("ok", "normal", 100, 100).allowed
    assert policy.gate("ok", "system", 500, 100).allowed  # system agents are never capped
    assert not policy.gate("throttle", "low", 0, None).allowed
    assert not policy.gate("pause", "normal", 0, None).allowed


# --- service -----------------------------------------------------------------


def test_record_exec_then_check_escalates_and_notifies(tmp_path):
    home = tmp_path / "codex"
    reset = int((NOW + timedelta(days=3)).timestamp())
    events = [
        token_count(NOW - timedelta(hours=24 - h), 1_000_000 * h, 5, 40 + 1.5 * h, reset - 3600, reset)
        for h in range(25)
    ]
    write_session(home, events=events)
    settings = service.BudgetSettings(codex_home=home)
    conn = connect(tmp_path / "pos.db")
    service.set_agent_class(conn, "mail", "normal")
    service.set_agent_class(conn, "idea", "low")

    run = service.record_exec(conn, exec_output(), agent_id="mail", task_id="t1", now=NOW,
                              settings=settings)
    assert run.usage.billable == 800
    row = conn.execute("SELECT * FROM budget_runs").fetchone()
    # The session log and the exec output are one run: tokens from the log, agent from exec.
    assert (row["agent_id"], row["task_id"], row["billable_tokens"]) == ("mail", "t1", 24_000_000)

    report = service.run_check(conn, now=NOW, settings=settings)
    assert report.level == "throttle"
    assert {a["type"] for a in report.actions} == {"level_changed", "notify_owner"}
    assert report.caps["idea"] == 0
    assert not service.can_run(conn, "idea", now=NOW).allowed
    assert service.can_run(conn, "unknown-agent", now=NOW).budget_class == "normal"

    # Same level next hour: no new owner task.
    again = service.run_check(conn, now=NOW + timedelta(minutes=5), settings=settings)
    assert again.actions == []
    assert service.status(conn)["level"] == "throttle"


def test_check_without_data_is_ok(tmp_path):
    conn = connect(tmp_path / "pos.db")
    report = service.run_check(conn, now=NOW, settings=service.BudgetSettings(codex_home=tmp_path))
    assert report.level == "ok" and report.windows == []
    assert service.can_run(conn, "anyone", now=NOW).allowed


def test_monthly_cap_lowers_budget(tmp_path):
    conn = connect(tmp_path / "pos.db")
    store.ensure_schema(conn)
    from pos.budget.codex_usage import TokenUsage

    store.record_run(conn, at=NOW - timedelta(days=1), usage=TokenUsage(0, 0, 9_000_000, 0),
                     source="exec", agent_id="mail")
    settings = service.BudgetSettings(codex_home=tmp_path, monthly_tokens=10_000_000)
    report = service.run_check(conn, now=NOW, settings=settings)
    assert report.level == "throttle"  # 9 M used by the 25th of a 10 M month
    assert report.agents_daily_budget is not None and report.agents_daily_budget < 1_000_000


def test_api(tmp_path):
    app = create_app(Settings(data_dir=tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/budget").json()["level"] == "ok"
        assert client.put("/api/budget/agents/hr", json={"budget_class": "system"}).status_code == 200
        gate = client.get("/api/budget/gate/hr").json()
        assert gate["allowed"] and gate["budget_class"] == "system"
        assert client.post("/api/budget/check").json()["level"] == "ok"
