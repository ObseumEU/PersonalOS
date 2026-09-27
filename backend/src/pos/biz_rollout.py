"""One-off production change for the business-value release (2026-09-27). Dry run by default:

    python -m pos.biz_rollout            # what would change
    python -m pos.biz_rollout --apply    # do it (versioned and audited, as the owner)

It is idempotent (a second --apply changes nothing) and does, as the owner:

1. **Every agent on Claude Opus 5.5** (the owner's decision): each active agent that runs in a worker
   gets engine `claude`, model `claude-opus-5-5` (its effort, medium, comes from agents/*/agent.json).
   The chat fast lane and the triage checks stay on Haiku.
2. **Budgets scaled for Opus**: each agent's active limits become the ones in its agent.json (scaled by
   the price step: Sonnet 5 → Opus 5.5 ×2, Haiku 4.5 → ×4, and ×1.5 more where the effort went up from
   low), never lower than a limit the Access manager or the owner already raised.
3. **Spike watch**: `spike_floor_usd` 1 → 3 USD per hour, so one Opus run after a quiet week does not
   pause an agent as a "spike" (the factor, 5× the weekly baseline, stays).
4. **T-016 "Connect Gmail"** is done: knowlage's own Gmail connector ingests both mailboxes and announces
   new mail to /api/events (routed to Customer Success, the CFO and Growth); T-025 (give the Mail agent
   a Gmail MCP), which only blocked it, is done too.
5. The runs' cost from the one ledger (engine_usage) and any weekly report left as a draft (W39):
   published from its numbers.
"""

import argparse
import json
import sys

# `agents` registers the "actor" entity with versioning; run as a script nothing else imports it.
from . import actors, agents, agents_code, audit, business, tasks, versioning, weekly  # noqa: F401
from .core import Ctx, now_iso

OPUS = "claude-opus-5-5"
SPIKE_FLOOR_USD = 3.0
DONE_TASKS = {
    16: "Hotovo bez MCP pro agenta: Gmail ingestuje knowlage vlastním konektorem (oba účty) a nové maily "
        "oznamuje do /api/events; routing je posílá Customer Success (zákaznické), CFO (faktury) a Growth "
        "(poptávky). Agenti hledají v poště nástrojem `knowledge`.",
    25: "Už není potřeba: Gmail pokrývá knowlage (viz T-016); agenti čtou poštu přes `knowledge`, "
        "odesílání dál jen přes schválení (request_outbound).",
}


def plan(conn) -> dict:
    from .access import service as access
    from .access import store as access_store

    out: dict = {"models": [], "budgets": [], "settings": {}, "tasks": []}
    for a in conn.execute("""SELECT * FROM actors WHERE kind IN ('ai', 'agent') AND archived_at IS NULL
                             AND runtime = 'codex_worker' AND a2a_url IS NULL ORDER BY id""").fetchall():
        if a["engine"] != "claude" or a["model"] != OPUS:
            out["models"].append({"id": a["id"], "name": a["name"], "from": [a["engine"], a["model"]]})
        spec = agents_code.spec_of(a["name"]) or {}
        if not access_store.ready(conn):
            continue
        for metric, amount in (spec.get("budget") or {}).items():
            if metric not in access.METRICS:
                continue
            active = conn.execute(f"SELECT * FROM access_budgets WHERE agent_id = ? AND metric = ? AND "
                                  f"{access_store.ACTIVE}", (a["id"], metric, now_iso())).fetchall()
            current = [r["amount"] for r in active if r["amount"] is not None]
            want = max([float(amount), *current]) if current else float(amount)
            if len(active) == 1 and active[0]["amount"] == want:
                continue
            out["budgets"].append({"id": a["id"], "name": a["name"], "metric": metric,
                                   "from": current or None, "to": want, "rows": [r["id"] for r in active]})
    cfg = access.settings(conn)
    if cfg.get("spike_floor_usd", 0) < SPIKE_FLOOR_USD:
        out["settings"] = {"spike_floor_usd": [cfg.get("spike_floor_usd"), SPIKE_FLOOR_USD]}
    for tid, note in DONE_TASKS.items():
        t = conn.execute("SELECT id, title, status FROM tasks WHERE id = ? AND archived_at IS NULL", (tid,)).fetchone()
        if t and t["status"] != "done":
            out["tasks"].append({"ref": tasks.display_id(tid), "title": t["title"], "from": t["status"]})
    out["weekly_drafts"] = [r["week"] for r in conn.execute(
        "SELECT week FROM weekly_reports WHERE status = 'draft' AND TRIM(narrative) = ''")] \
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'weekly_reports'").fetchone() else []
    return out


def apply(conn) -> dict:
    from .access import service as access

    p = plan(conn)
    owner = Ctx(actors.owner_id(conn), via="rollout")
    for m in p["models"]:
        versioning.update(conn, owner, "actor", m["id"], {"engine": "claude", "model": OPUS}, action="opus_switch")
    for b in p["budgets"]:
        access._end(conn, "access_budgets", b["rows"], owner.actor_id, "replaced",
                    "Opus 5.5 pro všechny agenty (2026-09-27): nový limit z agent.json")
        access._insert_budget(conn, b["id"], b["metric"], b["to"], owner.actor_id, "platform",
                              "Opus 5.5 pro všechny agenty (rozhodnutí majitele 2026-09-27): limit z agent.json, "
                              "přepočtený na cenu Opus; mění Správce přístupů nebo majitel")
    if p["settings"]:
        access.set_settings(conn, owner, {"spike_floor_usd": SPIKE_FLOOR_USD})
    for t in p["tasks"]:
        tid = tasks.parse_id(t["ref"])
        versioning.update(conn, owner, tasks.ENTITY, tid, {"status": "done", "completed_at": now_iso(),
                                                           "progress": 100, "progress_note": DONE_TASKS[tid]},
                          action="rollout_done")
    ledger = business.reconcile_ledger(conn)
    published = weekly.publish_overdue(conn)
    audit.log(conn, owner, "biz_rollout", None, None, models=len(p["models"]), budgets=len(p["budgets"]),
              tasks=[t["ref"] for t in p["tasks"]], settings=p["settings"] or None)
    conn.commit()
    return {**p, "ledger": ledger, "published": published}


def main(argv: list[str] | None = None) -> int:
    from .config import get_settings
    from .db import connect, migrate

    ap = argparse.ArgumentParser(prog="pos.biz_rollout")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db")
    a = ap.parse_args(argv)
    from pathlib import Path

    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        migrate(conn)
        business.ensure_schema(conn)
        out = apply(conn) if a.apply else plan(conn)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
