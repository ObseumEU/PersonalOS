"""Forecast where subscription usage will be when each limit window resets.

Pure functions over plain data, so they are easy to test and reuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass
class Sample:
    at: datetime
    used_percent: float
    resets_at: int | None
    window_minutes: int | None


@dataclass
class WindowForecast:
    name: str
    used_percent: float
    window_minutes: int | None
    resets_at: datetime | None
    hours_to_reset: float
    rate_per_hour: float  # percent points per hour
    projected_percent: float  # at reset, if the current pace holds
    exhausted_at: datetime | None  # when 100 % is hit at this pace, if before the reset
    samples: int

    @property
    def headroom_per_hour(self) -> float:
        """Percent points per hour that can still be spent and end at 100 % on reset."""
        if self.hours_to_reset <= 0:
            return 0.0
        return max(100.0 - self.used_percent, 0.0) / self.hours_to_reset


# Snapshots whose reset times differ by less than this belong to the same window.
SAME_WINDOW_TOLERANCE_S = 180


def current_window(samples: list[Sample], now: datetime) -> list[Sample]:
    """Samples from the window that is active now (same reset time as the latest one)."""
    if not samples:
        return []
    latest = samples[-1]
    if latest.resets_at is None:
        return samples[-1:]
    if latest.resets_at <= now.timestamp():
        return []  # the window has already reset; nothing known about the new one
    return [
        s for s in samples
        if s.resets_at is not None and abs(s.resets_at - latest.resets_at) <= SAME_WINDOW_TOLERANCE_S
    ]


def forecast_window(name: str, samples: list[Sample], now: datetime) -> WindowForecast | None:
    """Project `used_percent` to the reset time.

    The pace is a 50/50 blend of the recent pace (last ~15 % of the window) and the
    average pace since the window started. The recent pace reacts to bursts; the
    average keeps a quiet night from looking like "we will use nothing".
    """
    window = current_window(sorted(samples, key=lambda s: s.at), now)
    if not window:
        return None
    last = window[-1]
    reset = datetime.fromtimestamp(last.resets_at, timezone.utc) if last.resets_at else None
    hours_left = max((reset - now).total_seconds() / 3600, 0.0) if reset else 0.0

    avg_rate = None
    if last.window_minutes and reset:
        start = reset - timedelta(minutes=last.window_minutes)
        elapsed = (last.at - start).total_seconds() / 3600
        if elapsed > 0:
            avg_rate = last.used_percent / elapsed

    recent_rate = None
    lookback = timedelta(minutes=max((last.window_minutes or 300) * 0.15, 30))
    recent = [s for s in window if s.at >= last.at - lookback]
    if len(recent) >= 2:
        span = (recent[-1].at - recent[0].at).total_seconds() / 3600
        if span >= 1 / 6:  # at least 10 minutes of data
            recent_rate = max(recent[-1].used_percent - recent[0].used_percent, 0.0) / span

    rates = [r for r in (recent_rate, avg_rate) if r is not None]
    rate = sum(rates) / len(rates) if rates else 0.0

    # Usage may have moved on since the last snapshot; project from "now".
    since_last = max((now - last.at).total_seconds() / 3600, 0.0)
    used_now = min(last.used_percent + rate * since_last, 100.0) if rate else last.used_percent
    projected = used_now + rate * hours_left

    exhausted_at = None
    if rate > 0 and projected > 100:
        exhausted_at = now + timedelta(hours=max(100.0 - used_now, 0.0) / rate)

    return WindowForecast(
        name=name,
        used_percent=round(used_now, 2),
        window_minutes=last.window_minutes,
        resets_at=reset,
        hours_to_reset=round(hours_left, 2),
        rate_per_hour=round(rate, 4),
        projected_percent=round(projected, 1),
        exhausted_at=exhausted_at,
        samples=len(window),
    )


@dataclass
class PeriodForecast:
    """Tokens in the billing period (default: calendar month, spec chapter 8)."""

    start: datetime
    end: datetime
    used_tokens: int
    tokens_per_day: float
    projected_tokens: int
    budget_tokens: int | None  # optional owner-set cap; the subscription itself has none

    @property
    def projected_share(self) -> float | None:
        if not self.budget_tokens:
            return None
        return self.projected_tokens / self.budget_tokens


def billing_period(now: datetime, renewal_day: int = 1) -> tuple[datetime, datetime]:
    """Current period [start, end). `renewal_day` 1 = calendar month."""
    renewal_day = min(max(renewal_day, 1), 28)
    start = now.replace(day=renewal_day, hour=0, minute=0, second=0, microsecond=0)
    if start > now:
        start = _add_months(start, -1)
    return start, _add_months(start, 1)


def _add_months(at: datetime, months: int) -> datetime:
    month = at.month - 1 + months
    return at.replace(year=at.year + month // 12, month=month % 12 + 1)


def forecast_period(
    now: datetime,
    used_tokens: int,
    last_7_days_tokens: int,
    budget_tokens: int | None,
    renewal_day: int = 1,
) -> PeriodForecast:
    start, end = billing_period(now, renewal_day)
    elapsed_days = max((now - start).total_seconds() / 86400, 1 / 24)
    days_left = max((end - now).total_seconds() / 86400, 0.0)
    # Recent week if the period is long enough to have one, else the period so far.
    per_day = last_7_days_tokens / 7 if elapsed_days >= 7 else used_tokens / elapsed_days
    return PeriodForecast(
        start=start,
        end=end,
        used_tokens=used_tokens,
        tokens_per_day=round(per_day, 1),
        projected_tokens=int(used_tokens + per_day * days_left),
        budget_tokens=budget_tokens,
    )


def tokens_per_percent(points: list[tuple[float, int]]) -> float | None:
    """Estimate how many billable tokens one percent of a window is worth.

    `points` are (used_percent, cumulative tokens recorded) at snapshot times in
    one window. Needs at least one full percent of movement to be meaningful.
    """
    if len(points) < 2:
        return None
    (p0, t0), (p1, t1) = points[0], points[-1]
    if p1 - p0 < 1 or t1 <= t0:
        return None
    return (t1 - t0) / (p1 - p0)
