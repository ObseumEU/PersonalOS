"""Rozpočtář glue: ingest usage, run the hourly check, answer the run gate.

Hooks for the core (runner and scheduler), all taking an open sqlite connection:

    record_exec(conn, stdout_lines, agent_id=..., task_id=...)   after each `codex exec --json`
    can_run(conn, agent_id)                                       before each run -> Decision
    run_check(conn)                                               hourly -> Report (with actions)
    set_agent_class(conn, agent_id, "system" | "normal" | "low")  when an agent is created
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from . import store
from .codex_usage import (
    ExecRun,
    codex_home,
    find_session_log,
    iter_session_logs,
    parse_exec_jsonl,
    parse_session_log,
)
from .forecast import (
    Sample,
    WindowForecast,
    billing_period,
    forecast_period,
    forecast_window,
    tokens_per_percent,
)
from .policy import LEVELS, Decision, allocate_caps, assess, daily_agent_budget, gate


class BudgetSettings(BaseSettings):
    """POS_BUDGET_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="POS_BUDGET_", env_file=".env", extra="ignore")

    # Day of month the subscription renews; 1 = calendar month (spec chapter 8 default).
    renewal_day: int = 1
    # Optional owner-set cap on billable tokens per period. The subscription itself
    # only has the rolling windows Codex reports, so this is off by default.
    monthly_tokens: int | None = None
    # Share of the budget kept for the owner's own Codex use (not given to agents).
    owner_reserve: float = 0.3
    # Where Codex writes its session logs; default $CODEX_HOME or ~/.codex.
    codex_home: Path | None = None
    # Minutes between automatic checks in the web app; 0 turns them off.
    check_minutes: int = 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- ingest ------------------------------------------------------------------


def record_exec(
    conn: sqlite3.Connection,
    stdout_lines: Iterable[str],
    *,
    agent_id: str | None,
    task_id: str | None = None,
    now: datetime | None = None,
    settings: BudgetSettings | None = None,
) -> ExecRun:
    """Record one finished `codex exec --json` run, plus its limit snapshots."""
    store.ensure_schema(conn)
    now = now or utcnow()
    run = parse_exec_jsonl(stdout_lines)
    store.record_run(
        conn, at=now, usage=run.usage, source="exec",
        thread_id=run.thread_id, agent_id=agent_id, task_id=task_id,
    )
    if run.limit_reached:
        # Don't wait for the hourly check: stop non-system agents right away.
        report = {"at": store.iso(now), "level": "pause", "actions": [],
                  "reasons": ["Codex odmítl běh: vyčerpaný limit předplatného."]}
        store.save_check(conn, now, "pause", json.dumps(report, ensure_ascii=False))
    if run.thread_id:
        root = (settings or BudgetSettings()).codex_home or codex_home()
        path = find_session_log(run.thread_id, root)
        if path:
            _ingest_log(conn, path)
    return run


def _ingest_log(conn: sqlite3.Connection, path: Path) -> None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    if store.ingested_mtime(conn, str(path)) == mtime:
        return
    log = parse_session_log(path)
    for snap in log.snapshots:
        store.record_snapshot(conn, snap)
    if log.usage.billable and log.last_event_at:
        store.record_run(
            conn, at=log.last_event_at, usage=log.usage, source="session_log",
            thread_id=log.thread_id or str(path),
        )
    store.mark_ingested(conn, str(path), mtime)


def scan_sessions(conn: sqlite3.Connection, root: Path | None = None, days: int = 8) -> int:
    """Read new or changed Codex session logs, including the owner's own sessions.

    Every session on this machine counts against the same subscription, so the
    budget sees all of them, not only runs PersonalOS started.
    """
    store.ensure_schema(conn)
    since = (utcnow() - timedelta(days=days)).timestamp()
    count = 0
    for path in iter_session_logs(root or codex_home(), modified_after=since):
        _ingest_log(conn, path)
        count += 1
    return count


# --- check -------------------------------------------------------------------


@dataclass
class Report:
    at: str
    level: str
    reasons: list[str]
    windows: list[dict]
    period: dict
    agents_daily_budget: int | None
    caps: dict[str, int | None]
    usage_24h: dict[str, int]
    actions: list[dict] = field(default_factory=list)


def _window_samples(conn: sqlite3.Connection, window: str, now: datetime) -> list[Sample]:
    rows = store.snapshots(conn, window, now - timedelta(days=8))
    return [
        Sample(datetime.fromisoformat(r["at"]), r["used_percent"], r["resets_at"], r["window_minutes"])
        for r in rows
    ]


def _tokens_per_pct(conn: sqlite3.Connection, samples: list[Sample], w: WindowForecast) -> float | None:
    in_window = [s for s in samples if w.resets_at and s.resets_at and abs(s.resets_at - w.resets_at.timestamp()) <= 180]
    if len(in_window) < 2:
        return None
    first = in_window[0]
    points = [
        (s.used_percent, store.tokens_between(conn, first.at, s.at + timedelta(seconds=1)))
        for s in (first, in_window[-1])
    ]
    return tokens_per_percent(points)


def run_check(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    settings: BudgetSettings | None = None,
    scan: bool = True,
) -> Report:
    """The hourly check (spec 4.2): pace, forecast, level, caps and actions."""
    store.ensure_schema(conn)
    settings = settings or BudgetSettings()
    now = now or utcnow()
    if scan:
        scan_sessions(conn, settings.codex_home)

    samples = {name: _window_samples(conn, name, now) for name in ("primary", "secondary")}
    forecasts = [f for name, s in samples.items() if (f := forecast_window(name, s, now))]
    long_window = max(forecasts, key=lambda f: f.window_minutes or 0, default=None)
    tpp = _tokens_per_pct(conn, samples[long_window.name], long_window) if long_window else None

    start, _ = billing_period(now, settings.renewal_day)
    period = forecast_period(
        now,
        used_tokens=store.tokens_between(conn, start, now + timedelta(seconds=1)),
        last_7_days_tokens=store.tokens_between(conn, now - timedelta(days=7), now + timedelta(seconds=1)),
        budget_tokens=settings.monthly_tokens,
        renewal_day=settings.renewal_day,
    )
    assessment = assess(forecasts, period)
    if not forecasts:
        assessment.reasons.append("Zatím žádná data o limitech z Codexu.")

    agent_rows = store.agents(conn)
    classes = {a: row["budget_class"] for a, row in agent_rows.items()}
    total = daily_agent_budget(now, long_window, tpp, settings.owner_reserve, period)
    caps = allocate_caps(total, classes, assessment.level)
    store.set_caps(conn, caps, now)

    previous = store.latest_check(conn)
    prev_level = previous["level"] if previous else "ok"
    actions: list[dict] = []
    if assessment.level != prev_level:
        actions.append({"type": "level_changed", "from": prev_level, "to": assessment.level})
    if LEVELS.index(assessment.level) > LEVELS.index(prev_level) and assessment.level in ("throttle", "pause"):
        actions.append({
            "type": "notify_owner",
            "title": "Rozpočet tokenů: " + ("pozastaveno" if assessment.level == "pause" else "šetřím"),
            "body": " ".join(assessment.reasons),
        })

    report = Report(
        at=store.iso(now),
        level=assessment.level,
        reasons=assessment.reasons,
        windows=[_window_dict(f) for f in forecasts],
        period={
            "start": store.iso(period.start),
            "end": store.iso(period.end),
            "used_tokens": period.used_tokens,
            "tokens_per_day": period.tokens_per_day,
            "projected_tokens": period.projected_tokens,
            "budget_tokens": period.budget_tokens,
        },
        agents_daily_budget=total,
        caps=caps,
        usage_24h=store.tokens_by_agent(conn, now - timedelta(days=1), now + timedelta(seconds=1)),
        actions=actions,
    )
    store.save_check(conn, now, assessment.level, json.dumps(asdict(report)))
    return report


def _window_dict(f: WindowForecast) -> dict:
    return {
        "name": f.name,
        "window_minutes": f.window_minutes,
        "used_percent": f.used_percent,
        "projected_percent": f.projected_percent,
        "rate_per_hour": f.rate_per_hour,
        "resets_at": store.iso(f.resets_at) if f.resets_at else None,
        "hours_to_reset": f.hours_to_reset,
        "exhausted_at": store.iso(f.exhausted_at) if f.exhausted_at else None,
        "samples": f.samples,
    }


# --- gate & status -----------------------------------------------------------


def can_run(conn: sqlite3.Connection, agent_id: str, *, now: datetime | None = None) -> Decision:
    """Ask before starting a run. Uses the last check; no check yet means 'ok'."""
    store.ensure_schema(conn)
    now = now or utcnow()
    latest = store.latest_check(conn)
    level = latest["level"] if latest else "ok"
    row = store.agents(conn).get(agent_id)
    budget_class = row["budget_class"] if row else "normal"
    cap = row["daily_cap"] if row else None
    used = store.tokens_between(conn, now - timedelta(days=1), now + timedelta(seconds=1), agent_id)
    return gate(level, budget_class, used, cap)


def set_agent_class(conn: sqlite3.Connection, agent_id: str, budget_class: str) -> None:
    store.ensure_schema(conn)
    store.set_agent_class(conn, agent_id, budget_class, utcnow())


def status(conn: sqlite3.Connection) -> dict | None:
    store.ensure_schema(conn)
    latest = store.latest_check(conn)
    return json.loads(latest["report"]) if latest else None
