"""Event routing (AGENTS-SPEC 6a, step 4): an incoming event becomes a task for
the right member.

Events come from connector agents (the Mail agent reading Gmail over MCP, the
Community agent reading Discord), from webhooks (GitHub) or by hand. Rules
are data in PersonalOS, versioned like tasks, so the HR agent and the
self-improvement loop can change them (AGENTS-SPEC 6).

Event content comes from outside: it is stored and passed on wrapped as
untrusted (constitution U2), never as instructions.

Knowledge ingestion is not done here: agents push into knowlage-agent
themselves over its /ingest/mcp with their own key (apps/knowlage-agent
docs/INGEST.md).
"""

import json
import re
import sqlite3

from . import actors, audit, tasks, versioning
from .core import Ctx, NotFound, now_iso

versioning.register("route", "routing_rules")

SOURCES = ("gmail", "github", "discord", "calendar", "nexus", "web", "manual", "any")

DEFAULT_RULES = [
    # name, source, match, assignee, priority, topic
    ("GitHub issue labelled agent → Dev agent", "github", {"kind": "issue", "label": "agent"}, "Dev agent", 2, "dev"),
    ("GitHub review request → Dev agent", "github", {"kind": "review_requested"}, "Dev agent", 2, "dev"),
    ("Invoice e-mail → payment task for Nexus", "gmail", {"text_regex": r"faktur|invoice|rechnung"}, "Nexus", 1, "finance"),
    ("New e-mail → Mail agent triage", "gmail", {}, "Mail agent", 3, "mail"),
    ("Discord mention or question → Community agent", "discord", {"kind": "mention"}, "Community agent", 3, "community"),
]
# Off until the Dev agent can act on it (GitHub write access, repositories other
# than PersonalOS); until then every review request is a wasted run.
OFF_BY_DEFAULT = {"GitHub review request → Dev agent"}


# ------------------------------------------------------------------ rules

def seed_defaults(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM routing_rules").fetchone()[0]:
        return
    ctx = Ctx(actors.owner_id(conn), via="system")
    for i, (name, source, match, assignee, priority, topic) in enumerate(DEFAULT_RULES):
        create_rule(conn, ctx, {"name": name, "source": source, "match": match, "assignee": assignee,
                                "priority": priority, "topic": topic, "position": i,
                                "enabled": name not in OFF_BY_DEFAULT})
    conn.commit()


def _rule_out(row) -> dict:
    return {**dict(row), "match": json.loads(row["match"] or "{}"), "enabled": bool(row["enabled"])}


def list_rules(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM routing_rules" + ("" if include_archived else " WHERE archived_at IS NULL")
    return [_rule_out(r) for r in conn.execute(sql + " ORDER BY position, id")]


def _validate(fields: dict) -> dict:
    out = dict(fields)
    if "source" in out and out["source"] not in SOURCES:
        raise tasks.Invalid(f"source must be one of {SOURCES}")
    if "match" in out:
        m = out["match"] or {}
        if not isinstance(m, dict):
            raise tasks.Invalid("match must be an object")
        if m.get("text_regex"):
            try:
                re.compile(m["text_regex"])
            except re.error as e:
                raise tasks.Invalid(f"bad text_regex: {e}") from e
        out["match"] = json.dumps(m, ensure_ascii=False)
    if out.get("priority") not in (None, 1, 2, 3):
        raise tasks.Invalid("priority must be 1, 2 or 3")
    if "enabled" in out:
        out["enabled"] = 1 if out["enabled"] else 0
    return out


def create_rule(conn: sqlite3.Connection, ctx: Ctx, fields: dict) -> dict:
    now = now_iso()
    values = {"enabled": 1, "position": 100, "match": "{}", **_validate(fields),
              "created_by": ctx.actor_id, "created_at": now, "updated_at": now}
    return _rule_out(versioning.insert(conn, ctx, "route", values))


def update_rule(conn: sqlite3.Connection, ctx: Ctx, rule_id: int, changes: dict) -> dict:
    changes = dict(changes)
    reason = changes.pop("reason", None)  # why, for the audit log
    allowed = {"name", "source", "match", "assignee", "priority", "topic", "enabled", "position"}
    unknown = set(changes) - allowed
    if unknown:
        raise tasks.Invalid(f"unknown fields: {sorted(unknown)}")
    row = versioning.update(conn, ctx, "route", rule_id, _validate(changes))
    if reason:
        audit.log(conn, ctx, "route_reason", "route", rule_id, reason=str(reason)[:500])
    return _rule_out(row)


def archive_rule(conn: sqlite3.Connection, ctx: Ctx, rule_id: int) -> dict:
    return _rule_out(versioning.archive(conn, ctx, "route", rule_id))


def matches(rule: dict, event: dict) -> bool:
    if not rule["enabled"]:
        return False
    if rule["source"] not in ("any", event["source"]):
        return False
    m = rule["match"]
    if m.get("kind") and m["kind"] != event.get("kind"):
        return False
    labels = [str(x).lower() for x in (event.get("meta") or {}).get("labels", [])]
    if m.get("label") and m["label"].lower() not in labels:
        return False
    if m.get("from_contains") and m["from_contains"].lower() not in (event.get("author") or "").lower():
        return False
    if m.get("text_regex"):
        text = f"{event.get('title', '')}\n{event.get('body', '')}"
        if not re.search(m["text_regex"], text, re.IGNORECASE):
            return False
    return True


# ------------------------------------------------------------------ events

def ingest(conn: sqlite3.Connection, ctx: Ctx, event: dict) -> dict:
    """Store an event and turn it into a task by the first matching rule.
    The same (source, ref) twice is ignored."""
    from .guard.external import scan, wrap_external

    source = event.get("source")
    if source not in SOURCES or source == "any":
        raise tasks.Invalid(f"source must be one of {SOURCES[:-1]}")
    title = (event.get("title") or "").strip()
    if not title:
        raise tasks.Invalid("an event needs a title")
    ref = event.get("ref")
    if ref:
        dup = conn.execute("SELECT id, task_id, rule_id FROM events WHERE source = ? AND ref = ?",
                           (source, ref)).fetchone()
        if dup:
            return _reroute(conn, ctx, dup, event)

    rule = next((r for r in list_rules(conn) if matches(r, event)), None)
    body = event.get("body") or ""
    wrapped = wrap_external(source, body, ref=event.get("url") or ref) if body else ""
    signals = scan(f"{title}\n{body}").signals
    notes = "\n\n".join(x for x in (
        f"From {event['author']}" if event.get("author") else "",
        event.get("url") or "",
        wrapped,
    ) if x)
    fields = {"title": title[:300], "notes": notes, "source": f"event:{source}",
              "status": "next" if rule else "inbox"}
    if rule:
        fields.update({"assignee": rule["assignee"], "priority": rule["priority"], "topic": rule["topic"]})
    task = tasks.create(conn, ctx, {k: v for k, v in fields.items() if v is not None})
    cur = conn.execute(
        """INSERT INTO events (source, kind, ref, title, payload, rule_id, task_id, received_by, received_at, signals)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, event.get("kind"), ref, title[:300], json.dumps(event, ensure_ascii=False, default=str),
         rule["id"] if rule else None, task["id"], ctx.actor_id, now_iso(), ",".join(signals)),
    )
    if rule:
        conn.execute("UPDATE routing_rules SET hits = hits + 1 WHERE id = ?", (rule["id"],))
    audit.log(conn, ctx, "event", "task", task["id"], source=source, rule=rule["name"] if rule else None,
              suspicious=signals or None)
    return {"event_id": cur.lastrowid, "task_id": task["id"], "task_ref": task["ref"],
            "rule": rule["name"] if rule else None, "assignee": task["assignee_name"], "suspicious": signals}


def _reroute(conn: sqlite3.Connection, ctx: Ctx, dup: sqlite3.Row, event: dict) -> dict:
    """The same (source, ref) again, e.g. an issue opened without a label and
    labelled `agent` later. If no rule caught it the first time and one matches
    now, the existing task is routed by that rule (still open tasks only)."""
    out = {"event_id": dup["id"], "task_id": dup["task_id"], "duplicate": True}
    if dup["rule_id"] is not None or not dup["task_id"]:
        return out
    rule = next((r for r in list_rules(conn) if matches(r, event)), None)
    task = conn.execute("SELECT status FROM tasks WHERE id = ?", (dup["task_id"],)).fetchone()
    if rule is None or task is None or task["status"] not in ("inbox", "next"):
        return out
    tasks.update(conn, ctx, dup["task_id"], {k: v for k, v in {
        "status": "next", "priority": rule["priority"], "topic": rule["topic"]}.items() if v is not None})
    if rule["assignee"]:
        tasks.assign(conn, ctx, dup["task_id"], rule["assignee"])
    conn.execute("UPDATE events SET rule_id = ?, payload = ? WHERE id = ?",
                 (rule["id"], json.dumps(event, ensure_ascii=False, default=str), dup["id"]))
    conn.execute("UPDATE routing_rules SET hits = hits + 1 WHERE id = ?", (rule["id"],))
    t = tasks.get(conn, ctx, dup["task_id"])
    audit.log(conn, ctx, "event_rerouted", "task", t["id"], source=event.get("source"), rule=rule["name"])
    return {**out, "rerouted": True, "task_ref": t["ref"], "rule": rule["name"], "assignee": t["assignee_name"]}


def list_events(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT e.id, e.source, e.kind, e.ref, e.title, e.received_at, e.task_id, e.signals,
                  r.name AS rule_name, a.name AS received_by_name, t.assignee_name, t.status AS task_status
           FROM events e LEFT JOIN routing_rules r ON r.id = e.rule_id LEFT JOIN actors a ON a.id = e.received_by
           LEFT JOIN tasks t ON t.id = e.task_id ORDER BY e.id DESC LIMIT ?""", (limit,)
    ).fetchall()
    return [{**dict(r), "task_ref": tasks.display_id(r["task_id"]) if r["task_id"] else None} for r in rows]


# ------------------------------------------------------------------ GitHub webhook

def github_events(kind: str, payload: dict) -> list[dict]:
    """Translate a GitHub webhook into PersonalOS events."""
    repo = (payload.get("repository") or {}).get("full_name", "")
    out = []
    if kind == "issues" and payload.get("action") in ("opened", "labeled"):
        issue = payload["issue"]
        out.append({
            "source": "github", "kind": "issue", "ref": f"{repo}#{issue['number']}",
            "title": f"{repo}#{issue['number']}: {issue['title']}", "body": issue.get("body") or "",
            "url": issue.get("html_url"), "author": (issue.get("user") or {}).get("login"),
            "meta": {"labels": [lbl["name"] for lbl in issue.get("labels", [])], "repo": repo},
        })
    elif kind == "pull_request" and payload.get("action") == "review_requested":
        pr = payload["pull_request"]
        out.append({
            "source": "github", "kind": "review_requested", "ref": f"{repo}!{pr['number']}:review",
            "title": f"Review {repo}#{pr['number']}: {pr['title']}", "body": pr.get("body") or "",
            "url": pr.get("html_url"), "author": (pr.get("user") or {}).get("login"), "meta": {"repo": repo},
        })
    elif kind == "issue_comment" and payload.get("action") == "created":
        c = payload["comment"]
        issue = payload["issue"]
        out.append({
            "source": "github", "kind": "comment", "ref": f"{repo}#{issue['number']}/c{c['id']}",
            "title": f"Comment on {repo}#{issue['number']}: {issue['title']}", "body": c.get("body") or "",
            "url": c.get("html_url"), "author": (c.get("user") or {}).get("login"),
            "meta": {"labels": [lbl["name"] for lbl in issue.get("labels", [])], "repo": repo},
        })
    return out


def get_rule(conn: sqlite3.Connection, rule_id: int) -> dict:
    row = conn.execute("SELECT * FROM routing_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise NotFound(f"rule {rule_id}")
    return _rule_out(row)
