"""Turn forecasts into a level, per-agent daily caps and a run gate (spec 4.2).

Escalation, as the spec orders it:
    ok        nothing to do
    watch     a window is on course to end above WATCH_AT; caps tighten
    throttle  a window is on course to run out before it resets:
              'low' agents stop, 'normal' agents get smaller caps
    pause     a window is nearly empty (or Codex says the limit is hit):
              only 'system' agents run, and the owner gets a task
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .forecast import PeriodForecast, WindowForecast

LEVELS = ("ok", "watch", "throttle", "pause")

WATCH_AT = 85.0  # projected % at reset
THROTTLE_AT = 100.0  # projected % at reset
PAUSE_USED_AT = 95.0  # % already used

# Share of the remaining budget each class gets, per agent, relative to 'normal'.
CLASS_WEIGHT = {"system": 3.0, "normal": 2.0, "low": 1.0}
# Classes allowed to run at each level.
ALLOWED = {
    "ok": {"system", "normal", "low"},
    "watch": {"system", "normal", "low"},
    "throttle": {"system", "normal"},
    "pause": {"system"},
}
# How much of the ideal daily budget agents may use at each level. The rest is
# left for the owner's own Codex use and for catching up.
LEVEL_FACTOR = {"ok": 1.0, "watch": 0.75, "throttle": 0.5, "pause": 0.25}


@dataclass
class Assessment:
    level: str
    reasons: list[str] = field(default_factory=list)


def _worse(a: str, b: str) -> str:
    return a if LEVELS.index(a) >= LEVELS.index(b) else b


def assess(
    windows: list[WindowForecast],
    period: PeriodForecast | None = None,
    limit_reached: str | None = None,
) -> Assessment:
    result = Assessment("ok")

    def raise_to(level: str, reason: str) -> None:
        result.level = _worse(result.level, level)
        result.reasons.append(reason)

    if limit_reached:
        raise_to("pause", f"Codex hlásí vyčerpaný limit ({limit_reached}).")
    for w in windows:
        label = window_label(w)
        if w.used_percent >= PAUSE_USED_AT:
            raise_to("pause", f"{label}: vyčerpáno {w.used_percent:.0f} %.")
        elif w.projected_percent >= THROTTLE_AT:
            when = w.exhausted_at.strftime("%d.%m. %H:%M UTC") if w.exhausted_at else "před obnovou"
            raise_to("throttle", f"{label}: při tomto tempu dojde {when} ({w.projected_percent:.0f} % k obnově).")
        elif w.projected_percent >= WATCH_AT:
            raise_to("watch", f"{label}: odhad k obnově {w.projected_percent:.0f} %.")
    if period and period.projected_share is not None:
        share = period.projected_share
        if share >= 1.0:
            raise_to("throttle", f"Měsíční strop: odhad {share:.0%} do konce období.")
        elif share >= WATCH_AT / 100:
            raise_to("watch", f"Měsíční strop: odhad {share:.0%} do konce období.")
    return result


def window_label(w: WindowForecast) -> str:
    minutes = w.window_minutes or 0
    if minutes and minutes <= 24 * 60:
        return f"{minutes // 60}h okno"
    if minutes >= 6 * 24 * 60:
        return "týdenní okno"
    return f"okno {w.name}"


def daily_agent_budget(
    now: datetime,
    long_window: WindowForecast | None,
    tokens_per_pct: float | None,
    owner_reserve: float,
    period: PeriodForecast | None = None,
) -> int | None:
    """Billable tokens per day that agents together may spend, or None if unknown.

    From the long (weekly) window: what is left, spread evenly over the days to
    its reset, minus the owner's reserve for their own Codex use. An owner-set
    monthly cap, if any, can only lower it.
    """
    budgets = []
    if long_window and tokens_per_pct and long_window.hours_to_reset > 0:
        left_pct = max(100.0 - long_window.used_percent, 0.0)
        days = max(long_window.hours_to_reset / 24, 1 / 24)
        budgets.append(left_pct * tokens_per_pct / days)
    if period and period.budget_tokens:
        left = max(period.budget_tokens - period.used_tokens, 0)
        days = max((period.end - now).total_seconds() / 86400, 1 / 24)
        budgets.append(left / days)
    if not budgets:
        return None
    return int(min(budgets) * (1 - owner_reserve))


def allocate_caps(
    total_per_day: int | None, agent_classes: dict[str, str], level: str
) -> dict[str, int | None]:
    """Split the agents' daily budget by class weight; blocked classes get 0."""
    if total_per_day is None:
        return {a: (None if c in ALLOWED[level] else 0) for a, c in agent_classes.items()}
    budget = total_per_day * LEVEL_FACTOR[level]
    weights = {
        a: CLASS_WEIGHT.get(c, CLASS_WEIGHT["normal"])
        for a, c in agent_classes.items()
        if c in ALLOWED[level]
    }
    total_weight = sum(weights.values()) or 1.0
    return {a: int(budget * weights[a] / total_weight) if a in weights else 0 for a in agent_classes}


@dataclass
class Decision:
    allowed: bool
    reason: str
    level: str
    budget_class: str
    used_today: int
    daily_cap: int | None


def gate(level: str, budget_class: str, used_today: int, daily_cap: int | None) -> Decision:
    """May this agent start a `codex exec` run right now?"""
    if budget_class not in ALLOWED[level]:
        reason = (
            "Rozpočet je ve stavu „pozastaveno“: běží jen systémoví agenti."
            if level == "pause"
            else "Rozpočet šetří: agenti s nízkou prioritou čekají."
        )
        return Decision(False, reason, level, budget_class, used_today, daily_cap)
    if daily_cap is not None and used_today >= daily_cap and budget_class != "system":
        return Decision(
            False,
            f"Denní strop {daily_cap:,} tokenů vyčerpán ({used_today:,}).".replace(",", " "),
            level, budget_class, used_today, daily_cap,
        )
    return Decision(True, "ok", level, budget_class, used_today, daily_cap)
