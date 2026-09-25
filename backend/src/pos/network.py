"""The agent network: who works with whom, and how much (for the 3D view).

Nodes are members (people and agents) plus a hub for PersonalOS itself.
Edges aggregate interactions in a time window:

- assign:   someone hands a task to someone else (task creation or re-assignment)
- handoff:  a member passed its task to another with a note (handoff_task)
- message:  a message between members
- approval: an approval request to the owner
- mcp:      calls a member made to PersonalOS over MCP
- run:      codex runs a member did
- org:      who reports to whom (not windowed, no events)

Each edge has a `kind` (same as `type`) and a `scope`: `peer` between members,
`platform` to the PersonalOS hub, `org` for the hierarchy.
`events` lists the most recent interactions, for animating particles.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, killswitch

WINDOWS = {"live": timedelta(hours=1), "24h": timedelta(days=1), "7d": timedelta(days=7)}
HUB = 0


def build(conn: sqlite3.Connection, window: str = "24h") -> dict:
    if window not in WINDOWS:
        raise ValueError(f"window must be one of {sorted(WINDOWS)}")
    now = datetime.now(timezone.utc)
    since = (now - WINDOWS[window]).isoformat(timespec="seconds")
    owner = actors.owner_id(conn)
    frozen = killswitch.is_frozen(conn)

    from .budget import store as budget_store

    budget_store.ensure_schema(conn)
    from .engine_view import Viewer

    runtime_view = Viewer(conn)
    nodes = [{"id": HUB, "name": "PersonalOS", "kind": "hub", "open": 0, "working": 0, "review": 0,
              "tokens": 0, "status": "frozen" if frozen else "online", "is_owner": False}]
    for a in conn.execute("SELECT * FROM actors ORDER BY id"):
        load = conn.execute(
            """SELECT SUM(status = 'next') AS open, SUM(status = 'working') AS working, SUM(status = 'review') AS review
               FROM tasks WHERE assignee_id = ? AND archived_at IS NULL""", (a["id"],)
        ).fetchone()
        status = ("archived" if a["archived_at"] else "paused" if a["paused_at"]
                  else "frozen" if frozen and a["kind"] != "human" else "working" if load["working"] else "idle")
        nodes.append({
            "id": a["id"], "name": a["name"], "kind": a["kind"], "is_owner": bool(a["is_owner"]),
            "open": load["open"] or 0, "working": load["working"] or 0, "review": load["review"] or 0,
            "tokens": budget_store.tokens_between(conn, now - WINDOWS[window], now, str(a["id"])),
            "last_seen_at": a["last_seen_at"], "status": status,
            "role": a["role"], "team": a["team"], "reports_to": a["reports_to"],
            "engine_view": runtime_view.for_actor(a),
        })

    edges: dict[tuple[int, int, str], int] = {}
    events: list[dict] = []

    def add(src, dst, kind, at):
        if src is None or dst is None or src == dst:
            return
        edges[(src, dst, kind)] = edges.get((src, dst, kind), 0) + 1
        events.append({"from": src, "to": dst, "type": kind, "at": at})

    # Hand-offs: a task created for, or re-assigned to, someone else.
    for r in conn.execute(
        """SELECT h.actor_id, h.at, json_extract(h.data, '$.assignee_id') AS to_id FROM history h
           WHERE h.entity = 'task' AND h.at >= ? AND h.action IN ('create', 'assign', 'claim', 'clarify:accept')""",
        (since,),
    ):
        add(r["actor_id"], r["to_id"], "assign", r["at"])
    # Handoff messages are drawn once, as the handoff.
    for r in conn.execute("SELECT from_actor, to_actor, created_at FROM messages WHERE created_at >= ? "
                          "AND id NOT IN (SELECT message_id FROM handoffs WHERE message_id IS NOT NULL)", (since,)):
        add(r["from_actor"], r["to_actor"], "message", r["created_at"])
    for r in conn.execute("SELECT from_actor, to_actor, created_at FROM handoffs WHERE created_at >= ?", (since,)):
        add(r["from_actor"], r["to_actor"], "handoff", r["created_at"])
    for r in conn.execute("SELECT requested_by, created_at FROM approvals WHERE created_at >= ?", (since,)):
        add(r["requested_by"], owner, "approval", r["created_at"])
    for r in conn.execute(
        "SELECT actor_id, at FROM audit_log WHERE via = 'mcp' AND at >= ? AND action LIKE 'mcp:%'", (since,)
    ):
        add(r["actor_id"], HUB, "mcp", r["at"])
    for r in conn.execute("SELECT actor_id, started_at FROM runs WHERE started_at >= ?", (since,)):
        add(r["actor_id"], HUB, "run", r["started_at"])

    for a in conn.execute("SELECT id, reports_to FROM actors WHERE archived_at IS NULL AND reports_to IS NOT NULL"):
        if a["reports_to"] != a["id"]:
            edges[(a["id"], a["reports_to"], "org")] = 1

    def scope(dst: int, kind: str) -> str:
        return "org" if kind == "org" else "platform" if dst == HUB else "peer"

    events.sort(key=lambda e: e["at"], reverse=True)
    for e in events:
        e["kind"], e["scope"] = e["type"], scope(e["to"], e["type"])
    return {
        "window": window, "frozen": frozen, "nodes": nodes,
        "edges": [{"from": s, "to": d, "type": k, "kind": k, "scope": scope(d, k), "count": n}
                  for (s, d, k), n in edges.items()],
        "events": events[:60],
    }


def as_json(conn: sqlite3.Connection, window: str) -> str:
    return json.dumps(build(conn, window))
