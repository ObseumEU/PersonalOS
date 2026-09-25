"""When the HR review and weekly report are due. Stands in for the Nexus scheduler
(AGENTS-SPEC task 11); the web app checks this every half hour."""

import sqlite3
from datetime import datetime

from pydantic_settings import BaseSettings, SettingsConfigDict

from ..core import TZ
from . import service, store


class HRSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POS_HR_", env_file=".env", extra="ignore")

    # Run the daily review and weekly report inside the web app (off = use cron).
    scheduler: bool = True
    # Local hour (Europe/Prague) from which the day's review may run.
    daily_hour: int = 6
    # Weekday of the weekly report, 0 = Monday.
    weekly_day: int = 0


def _local_date(value: str | None):
    return datetime.fromisoformat(value).astimezone(TZ).date() if value else None


def due(conn: sqlite3.Connection, now: datetime, cfg: HRSettings = HRSettings()) -> list[str]:
    store.ensure_schema(conn)
    local = now.astimezone(TZ)
    if local.hour < cfg.daily_hour:
        return []
    out = []
    if _local_date((store.latest_review(conn, "daily") or {}).get("at")) != local.date():
        out.append("daily")
    last_weekly = _local_date((store.latest_review(conn, "weekly") or {}).get("at"))
    if local.weekday() == cfg.weekly_day and last_weekly != local.date():
        out.append("weekly")
    return out


def run_due(conn: sqlite3.Connection, now: datetime | None = None, cfg: HRSettings = HRSettings()) -> list[str]:
    now = now or service.utcnow()
    jobs = due(conn, now, cfg)
    if "daily" in jobs:
        service.daily_review(conn, now=now)
    if "weekly" in jobs:
        service.weekly_report(conn, now=now)
    return jobs
