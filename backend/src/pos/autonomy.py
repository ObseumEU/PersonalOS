"""The owner's switch to autonomous agents (2026-09-27), one-off for an existing install.

"Allow really everything, right away, always ... lower the bar to 5 % of what it is now,
for everyone and everything." New agents get this from the code (pos.access.autonomy_caps,
the loosened defaults); this script brings the agents that already exist along:

    python -m pos.autonomy              # dry run: what would change (nothing is written)
    python -m pos.autonomy --apply      # do it

1. grants    every active agent gets every autonomy capability (all the platform's tool
             groups except access:manage; outbound stays as it is, constitution rule 1),
             also those ended earlier.
2. budgets   every agent's permanent own budget 20x (usd_run 5x); once (marked in the
             settings, a second run does not scale again).
3. hr        the stored HR limit on active agents is at least the new default (520).
4. spikes    the Access manager's stored spike thresholds are at least the new defaults
             (pause only on a truly extreme runaway).

Idempotent; every change is audit-logged as the owner ("via autonomy").
"""

import argparse
import json
import sqlite3

from . import actors, audit, settings_store
from .access import service as access
from .access import store as access_store
from .core import Ctx

SCALED_KEY = "autonomy.budgets_scaled_at"


def step_grants(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    out = []
    caps = access.autonomy_caps()
    for a in conn.execute("SELECT id, name FROM actors WHERE kind != 'human' AND archived_at IS NULL "
                          "AND runtime != 'service' ORDER BY id").fetchall():
        have = access.effective(conn, a["id"]) or set()
        missing = [c for c in caps if c not in have]
        if not missing:
            continue
        out.append(f"{a['name']}: +{len(missing)} ({', '.join(missing[:6])}{'…' if len(missing) > 6 else ''})")
        if apply:
            if not access_store.seeded(conn, a["id"]):
                access.seed_agent(conn, a["id"], ctx.actor_id)
            access.grant_autonomy(conn, a["id"], ctx.actor_id, again=True)
    return out


def step_budgets(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    if settings_store.get(conn, SCALED_KEY):
        return []
    out = []
    now = access._iso(access.utcnow())
    rows = conn.execute(
        f"""SELECT b.*, a.name FROM access_budgets b JOIN actors a ON a.id = b.agent_id
            WHERE b.agent_id IS NOT NULL AND b.expires_at IS NULL AND b.amount IS NOT NULL
              AND a.archived_at IS NULL AND {access_store.ACTIVE} ORDER BY b.agent_id, b.metric""",
        (now,)).fetchall()
    for b in rows:
        new = access.scaled(b["metric"], b["amount"])
        out.append(f"{b['name']}: {b['metric']} {b['amount']:g} -> {new:g}")
        if apply:
            access._end(conn, "access_budgets", [b["id"]], ctx.actor_id, "replaced", "autonomie: 20x (běh 5x)")
            access._insert_budget(conn, b["agent_id"], b["metric"], new, ctx.actor_id, "autonomy",
                                  f"autonomie (majitel, 2026-09-27): {b['amount']:g} -> {new:g}")
    if apply:
        settings_store.put(conn, ctx, SCALED_KEY, access.now_iso())
    return out


def step_hr(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    from .hr.policy import SETTING_MAX_ACTIVE, HRPolicy

    have = settings_store.get(conn, SETTING_MAX_ACTIVE)
    want = HRPolicy().max_active_agents
    if have is None or int(have) >= want:
        return []
    if apply:
        settings_store.put(conn, ctx, SETTING_MAX_ACTIVE, want)
    return [f"hr.max_active_agents {have} -> {want}"]


def step_spikes(conn: sqlite3.Connection, ctx: Ctx, apply: bool) -> list[str]:
    stored = settings_store.get(conn, access.SETTINGS_KEY) or {}
    raised = {k: access.DEFAULT_SETTINGS[k] for k, v in stored.items()
              if k.startswith("spike_") and k in access.DEFAULT_SETTINGS and float(v) < access.DEFAULT_SETTINGS[k]}
    if not raised:
        return []
    if apply:
        settings_store.put(conn, ctx, access.SETTINGS_KEY, {**stored, **raised})
    return [f"access.{k} {stored[k]} -> {v}" for k, v in raised.items()]


STEPS = (("grants", step_grants), ("budgets", step_budgets), ("hr", step_hr), ("spikes", step_spikes))


def run(conn: sqlite3.Connection, apply: bool = False) -> dict:
    ctx = Ctx(actors.owner_id(conn), via="autonomy")
    access_store.ensure_schema(conn)
    report = {key: fn(conn, ctx, apply) for key, fn in STEPS}
    if apply:
        audit.log(conn, ctx, "autonomy", None, None, **{k: len(v) for k, v in report.items()})
        conn.commit()
    else:
        conn.rollback()
    return report


def main() -> None:
    from .config import get_settings
    from .db import connect

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes (default: a dry run)")
    a = ap.parse_args()
    conn = connect(get_settings().db_path)
    print(json.dumps(run(conn, apply=a.apply), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
