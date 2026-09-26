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
import os
import re
import sqlite3

from . import actors, audit, tasks, versioning
from .core import Ctx, NotFound, now_iso

versioning.register("route", "routing_rules")

SOURCES = ("gmail", "github", "discord", "calendar", "nexus", "web", "manual", "sentinel", "any")


def dev_repos() -> list[str]:
    """Repositories the Dev agent works on (its worktree has only these):
    POS_DEV_REPOS, comma-separated `owner/name` as GitHub's full_name."""
    raw = os.environ.get("POS_DEV_REPOS", "ObseumEU/PersonalOS")
    return [r.strip() for r in raw.split(",") if r.strip()]


DEV_RULES = ("GitHub issue labelled agent → Dev agent", "GitHub review request → Dev agent")

DEFAULT_RULES = [
    # name, source, match, assignee, priority, topic
    (DEV_RULES[0], "github", {"kind": "issue", "label": "agent"}, "Dev agent", 2, "dev"),
    (DEV_RULES[1], "github", {"kind": "review_requested"}, "Dev agent", 2, "dev"),
    ("Invoice e-mail → payment task for Nexus", "gmail", {"text_regex": r"faktur|invoice|rechnung"}, "Nexus", 1, "finance"),
    ("New e-mail → Mail agent triage", "gmail", {}, "Mail agent", 3, "mail"),
    ("Discord mention or question → Community agent", "discord", {"kind": "mention"}, "Community agent", 3, "community"),
]
# Off until the Dev agent can act on it (GitHub write access, repositories other
# than PersonalOS); until then every review request is a wasted run.
OFF_BY_DEFAULT = {"GitHub review request → Dev agent"}
# On only while Nexus is reachable (POS_NEXUS_A2A_URL); else its tasks would wait for ever.
NEXUS_RULE = "Invoice e-mail → payment task for Nexus"


# ------------------------------------------------------------------ rules

def seed_defaults(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT COUNT(*) FROM routing_rules").fetchone()[0]:
        return
    ctx = Ctx(actors.owner_id(conn), via="system")
    for i, (name, source, match, assignee, priority, topic) in enumerate(DEFAULT_RULES):
        if name in DEV_RULES:
            match = {**match, "repo": dev_repos()}
        create_rule(conn, ctx, {"name": name, "source": source, "match": match, "assignee": assignee,
                                "priority": priority, "topic": topic, "position": i,
                                "enabled": name not in OFF_BY_DEFAULT
                                and (name != NEXUS_RULE or bool(os.environ.get("POS_NEXUS_A2A_URL")))})
    conn.commit()


def sync_nexus_rule(conn: sqlite3.Connection) -> None:
    """Switch the invoice → Nexus rule on when Nexus has an A2A URL and off when
    not, unless a person changed the rule by hand (then it is theirs)."""
    row = conn.execute("SELECT id, enabled FROM routing_rules WHERE name = ? AND archived_at IS NULL",
                       (NEXUS_RULE,)).fetchone()
    if row is None:
        return
    actions = {h["action"] for h in versioning.history(conn, "route", row["id"])}
    if actions - {"create", "auto_nexus"}:
        return
    want = bool(os.environ.get("POS_NEXUS_A2A_URL"))
    if bool(row["enabled"]) != want:
        versioning.update(conn, Ctx(actors.owner_id(conn), via="system"), "route", row["id"],
                          {"enabled": 1 if want else 0}, action="auto_nexus")
        conn.commit()


def sync_dev_repos(conn: sqlite3.Connection) -> None:
    """Give the default Dev agent rules the repo allowlist (POS_DEV_REPOS) when
    they have none yet, unless a person changed the rule by hand."""
    for name in DEV_RULES:
        row = conn.execute("SELECT id, match FROM routing_rules WHERE name = ? AND archived_at IS NULL",
                           (name,)).fetchone()
        if row is None:
            continue
        match = json.loads(row["match"] or "{}")
        actions = {h["action"] for h in versioning.history(conn, "route", row["id"])}
        if "repo" in match or actions - {"create", "auto_nexus", "auto_repo"}:
            continue
        versioning.update(conn, Ctx(actors.owner_id(conn), via="system"), "route", row["id"],
                          _validate({"match": {**match, "repo": dev_repos()}}), action="auto_repo")
    conn.commit()


def _repos(value) -> list[str]:
    return [value] if isinstance(value, str) else [str(x) for x in (value or [])]


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
        if "repo" in m and not all(isinstance(x, str) and "/" in x for x in _repos(m["repo"])):
            raise tasks.Invalid("match.repo must be owner/name or a list of them")
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
    if m.get("repo"):  # an allowlist: owner/name, one or several
        repo = str((event.get("meta") or {}).get("repo") or "").lower()
        if repo not in {r.lower() for r in _repos(m["repo"])}:
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
    if source == "sentinel":  # the sentinel's incidents: escalations, resolutions and the budget caps (pos.monitor)
        from . import monitor

        handled = monitor.before_route(conn, ctx, event)
        if handled is not None:
            return handled
    if source == "github" and event.get("kind") == "comment" and ref and "/c" in ref:
        answered = _comment_on_parked(conn, ctx, event, title)
        if answered:
            return answered

    from . import mailfilter

    skipped = mailfilter.skip_reason(event)
    if skipped:  # bulk or automatic mail: stored and counted, no task, no run
        cur = conn.execute(
            """INSERT INTO events (source, kind, ref, title, payload, rule_id, task_id, received_by, received_at, signals)
               VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)""",
            (source, event.get("kind"), ref, title[:300], json.dumps(event, ensure_ascii=False, default=str),
             ctx.actor_id, now_iso(), f"skipped:{skipped}"[:300]),
        )
        return {"event_id": cur.lastrowid, "task_id": None, "skipped": skipped}

    rule = next((r for r in list_rules(conn) if matches(r, event)), None)
    body = event.get("body") or ""
    wrapped = wrap_external(source, body, ref=event.get("url") or ref) if body else ""
    signals = scan(f"{title}\n{body}").signals
    kind = f" {event['kind']}" if event.get("kind") else ""
    purpose = (f"Purpose: an incoming {source}{kind} item that may need action. Decide: reply, a task for "
               f"someone, or nothing.\nSource: {source}"
               + (f", routed by the rule “{rule['name']}”." if rule else ", no routing rule matched (inbox)."))
    if source == "sentinel":
        from . import monitor

        purpose = monitor.purpose(event)
    notes = "\n\n".join(x for x in (
        purpose,
        f"From {event['author']}" if event.get("author") else "",
        event.get("url") or "",
        wrapped,
    ) if x)
    fields = {"title": title[:300], "notes": notes, "source": f"event:{source}",
              "status": "next" if rule else "inbox",
              "definition_of_done": "The item is handled (answered, turned into a task, or judged to need "
                                    "nothing) and the note says which."}
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
    if source == "sentinel":
        from . import monitor

        monitor.after_route(conn, ctx, event, task)
    return {"event_id": cur.lastrowid, "task_id": task["id"], "task_ref": task["ref"],
            "rule": rule["name"] if rule else None, "assignee": task["assignee_name"], "suspicious": signals}


def _reroute(conn: sqlite3.Connection, ctx: Ctx, dup: sqlite3.Row, event: dict) -> dict:
    """The same (source, ref) again, e.g. an issue opened without a label and
    labelled `agent` later. If no rule caught it the first time and one matches
    now, the existing task is routed by that rule (still open tasks only)."""
    out = {"event_id": dup["id"], "task_id": dup["task_id"], "duplicate": True}
    if dup["rule_id"] is not None and dup["task_id"]:
        # A new label on an issue whose task was handed back or parked: back in the queue.
        rule = next((r for r in list_rules(conn) if matches(r, event)), None)
        if rule and _wake(conn, ctx, dup["task_id"], rule, "a new label on the issue"):
            return {**out, "requeued": True}
        return out
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


def _wake(conn: sqlite3.Connection, ctx: Ctx, task_id: int, rule: dict, why: str) -> bool:
    """Requeue a task that an agent handed back, failed or parked (it has a
    back-off, pos.api_worker.back_off) because something new happened on it."""
    t = conn.execute("SELECT status, retry_after FROM tasks WHERE id = ? AND archived_at IS NULL",
                     (task_id,)).fetchone()
    if t is None or not t["retry_after"] or t["status"] not in ("inbox", "next", "waiting") or not rule["enabled"]:
        return False
    conn.execute("UPDATE tasks SET retry_after = NULL WHERE id = ?", (task_id,))
    tasks.update(conn, ctx, task_id, {"status": "next", "progress_note": f"Back in the queue: {why}."})
    if rule["assignee"]:
        tasks.assign(conn, ctx, task_id, rule["assignee"])
    audit.log(conn, ctx, "event_requeued", "task", task_id, rule=rule["name"], why=why)
    return True


def _comment_on_parked(conn: sqlite3.Connection, ctx: Ctx, event: dict, title: str) -> dict | None:
    """A comment on an issue whose task is parked or handed back (e.g. the answer
    to the agent's question): the comment goes into that task and it is queued
    again, instead of becoming a task of its own. Our own posted question is
    only recorded."""
    from .guard.external import wrap_external

    issue_ref = event["ref"].rsplit("/c", 1)[0]
    parent = conn.execute("SELECT task_id, rule_id FROM events WHERE source = 'github' AND ref = ? "
                          "AND task_id IS NOT NULL AND rule_id IS NOT NULL", (issue_ref,)).fetchone()
    if parent is None:
        return None
    body = (event.get("body") or "").strip()
    ours = any((json.loads(a["details"] or "{}").get("payload") or {}).get("body", "").strip() == body
               for a in conn.execute("SELECT details FROM approvals WHERE task_id = ? AND action = 'github.comment'",
                                     (parent["task_id"],)))
    rule = get_rule(conn, parent["rule_id"])

    def record(task_id):
        return conn.execute(
            """INSERT INTO events (source, kind, ref, title, payload, rule_id, task_id, received_by, received_at, signals)
               VALUES ('github', 'comment', ?, ?, ?, ?, ?, ?, ?, '')""",
            (event["ref"], title[:300], json.dumps(event, ensure_ascii=False, default=str), rule["id"], task_id,
             ctx.actor_id, now_iso())).lastrowid

    if ours and body:
        return {"event_id": record(parent["task_id"]), "task_id": parent["task_id"], "own_comment": True}
    t = conn.execute("SELECT notes, retry_after FROM tasks WHERE id = ?", (parent["task_id"],)).fetchone()
    if t is None or not t["retry_after"]:
        return None
    if not _wake(conn, ctx, parent["task_id"], rule, "a new comment on the issue"):
        return None
    who = f" from {event['author']}" if event.get("author") else ""
    wrapped = wrap_external("github", body, ref=event.get("url") or event["ref"]) if body else ""
    tasks.update(conn, ctx, parent["task_id"], {"notes": f"{t['notes'] or ''}\n\nNew comment{who}:\n{wrapped}".strip()})
    tk = tasks.get(conn, ctx, parent["task_id"])
    return {"event_id": record(parent["task_id"]), "task_id": tk["id"], "task_ref": tk["ref"], "rule": rule["name"],
            "assignee": tk["assignee_name"], "requeued": True}


def skipped_mail(conn: sqlite3.Connection, days: int = 7) -> dict:
    """How many new-mail events the prefilter dropped, by reason."""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT substr(signals, 9) AS reason, COUNT(*) AS n FROM events
           WHERE signals LIKE 'skipped:%' AND received_at >= ? GROUP BY reason ORDER BY n DESC""", (since,)
    ).fetchall()
    return {"days": days, "total": sum(r["n"] for r in rows), "by_reason": {r["reason"]: r["n"] for r in rows}}


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
