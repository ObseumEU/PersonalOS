"""Rozpočtář: keeps Codex subscription usage within its limits (docs/AGENTS-SPEC.md, 4.2).

The ChatGPT subscription has no monthly token counter. Codex reports two rolling
limit windows instead: a short one (5 hours) and a long one (a week), each as
"percent used" with a reset time. The budget agent therefore:

1. records every `codex exec` run (tokens, agent, task) and every limit snapshot,
2. forecasts, per window, where "percent used" will be when the window resets,
3. turns that forecast into a level (ok / watch / throttle / pause), daily token
   caps per agent and a gate the runner asks before starting a run.

Modules:
    codex_usage  parse `codex exec --json` output and Codex session logs
    store        own SQLite tables (no edits to the core migrations)
    forecast     pure forecasting maths
    policy       levels, caps and the run gate
    service      glue: ingest, hourly check, status for the API
    api          FastAPI router under /api/budget
"""
