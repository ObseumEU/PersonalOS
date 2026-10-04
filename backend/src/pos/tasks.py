"""Task core (AGENTS-SPEC 6a): fields, projects and steps, assignees, views.

Every write goes through `versioning`, so it is versioned and audited, and
every read is filtered by the caller's visibility layer. Callers are the REST
API (people), the MCP server (agents, Codex, Claude) and the runner.
"""

import json
import sqlite3
from datetime import date, timedelta

from . import actors, capture as capture_syntax, task_descriptions, versioning
from .core import Ctx, Forbidden, NotFound, now_iso, today
from .visibility import DEFAULT, LAYERS, check_read, check_write, visible_sql

ENTITY = "task"
STATUSES = ("inbox", "next", "working", "review", "waiting", "someday", "done")
ASSIGNEE_TYPES = ("human", "ai", "agent", "external")
OPEN = ("inbox", "next", "working", "review", "waiting", "someday")

EDITABLE = {
    "title", "notes", "status", "priority", "do_date", "deadline", "estimate_min", "energy", "topic",
    "definition_of_done", "visibility", "follow_up", "position", "progress", "progress_note",
    "value_kind",  # business | platform | demo, or empty for automatic (pos.business)
}

VIEWS = ("inbox", "today", "upcoming", "next", "agents", "waiting", "review", "to_review", "someday", "done")


class Invalid(ValueError):
    pass


# ------------------------------------------------------------------ helpers

def display_id(task_id: int) -> str:
    return f"T-{task_id:03d}"


def parse_id(value: str | int) -> int:
    if isinstance(value, int):
        return value
    v = value.strip().upper()
    if v.startswith("T-"):
        v = v[2:]
    if not v.isdigit():
        raise Invalid(f"not a task id: {value}")
    return int(v)


def _validate(fields: dict) -> None:
    if "status" in fields and fields["status"] not in STATUSES:
        raise Invalid(f"status must be one of {STATUSES}")
    if fields.get("priority") not in (None, 1, 2, 3):
        raise Invalid("priority must be 1, 2 or 3")
    if fields.get("energy") not in (None, "high", "low"):
        raise Invalid("energy must be high or low")
    if "visibility" in fields and fields["visibility"] not in LAYERS:
        raise Invalid(f"visibility must be one of {LAYERS}")
    for k in ("do_date", "deadline", "follow_up"):
        if fields.get(k):
            try:
                date.fromisoformat(fields[k])
            except ValueError as e:
                raise Invalid(f"{k} must be YYYY-MM-DD") from e
    if "title" in fields and not str(fields["title"]).strip():
        raise Invalid("title is empty")
    if "value_kind" in fields:
        from .business import VALUE_KINDS

        if fields["value_kind"] in ("", "auto"):
            fields["value_kind"] = None
        if fields["value_kind"] not in (None, *VALUE_KINDS):
            raise Invalid(f"value_kind must be one of {VALUE_KINDS} (or empty for automatic)")


def resolve_assignee(conn: sqlite3.Connection, ctx: Ctx, value) -> dict:
    """Turn 'me' / 'ai' / an actor name / an outside name (or a dict) into columns."""
    if value in (None, "", "none"):
        return {"assignee_type": None, "assignee_id": None, "assignee_name": None}
    if isinstance(value, dict):
        kind = value.get("type")
        if kind not in ASSIGNEE_TYPES:
            raise Invalid(f"assignee type must be one of {ASSIGNEE_TYPES}")
        if kind == "external":
            name = (value.get("name") or "").strip()
            if not name:
                raise Invalid("an outside assignee needs a name")
            return {"assignee_type": "external", "assignee_id": None, "assignee_name": name}
        if value.get("id"):
            row = actors.get(conn, int(value["id"]))
            if row["archived_at"]:
                raise Invalid(f"{row['name']} is archived: give the task to its successor or its lead")
            return {"assignee_type": row["kind"], "assignee_id": row["id"], "assignee_name": row["name"]}
        value = value.get("name") or kind
    v = str(value).strip().lstrip("@")
    if v.lower() in ("me", "ja", "já"):
        me = actors.get(conn, ctx.actor_id)
        row = me if me["kind"] == "human" else actors.get(conn, actors.owner_id(conn))
    elif v.lower() in ("ai", "assistant"):
        row = actors.get(conn, actors.assistant_id(conn))
    else:
        row = actors.find_by_name(conn, v)
    if row is None:
        return {"assignee_type": "external", "assignee_id": None, "assignee_name": v}
    return {"assignee_type": row["kind"], "assignee_id": row["id"], "assignee_name": row["name"]}


OWNER_ASSIGN_SOURCES = ("ask_owner",)  # an agent's question for the owner goes through pos.asks
TO_OWNER_REFUSED = ("Agents do not give tasks to the owner. Ask him with ask_owner (a question or a decision "
                    "only he can make), or give the task to your lead or the CEO.")


def refuse_owner_assignee(conn: sqlite3.Connection, ctx: Ctx, assignee_id: int | None,
                          source: str | None = None) -> None:
    """An agent's own tool call (MCP) never assigns a task to the owner (prod: 6 open tasks agents
    gave him, nobody worked on them). Platform flows (worker hand-backs, the command guard, support
    drafts) keep their own rules."""
    if ctx.via != "mcp" or not assignee_id or (source or "") in OWNER_ASSIGN_SOURCES:
        return
    if assignee_id != actors.owner_id(conn):
        return
    me = conn.execute("SELECT kind FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()
    if me is not None and me["kind"] != "human":
        raise Invalid(TO_OWNER_REFUSED)


def _row(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(display_id(task_id))
    check_read(conn, ENTITY, row, ctx.actor_id)
    return row


def to_dict(row: sqlite3.Row | dict) -> dict:
    d = dict(row)
    d["ref"] = display_id(d["id"])
    d.setdefault("value_kind", None)
    d["suggestion"] = json.loads(d["suggestion"]) if d.get("suggestion") else None
    return d


# ------------------------------------------------------------------ reads

def get(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    row = _row(conn, ctx, task_id)
    task = to_dict(row)
    # Who reviews the result (set, or by default) and whether the caller may.
    rid = reviewer_of(conn, row)
    task["reviewer_effective_id"] = rid
    task["reviewer_name"] = actors.get(conn, rid)["name"]
    task["can_review"] = row["status"] == "review" and may_review(conn, ctx, row)[0]
    cond, params = visible_sql(ENTITY, ctx.actor_id)
    steps = conn.execute(
        f"SELECT * FROM tasks WHERE parent_id = ? AND archived_at IS NULL AND {cond} ORDER BY position, id",
        [task_id, *params],
    ).fetchall()
    task["steps"] = [to_dict(s) for s in steps]
    task["steps_done"] = sum(1 for s in steps if s["status"] == "done")
    # What the runs on it cost so far (Claude in USD; Codex only in tokens).
    u = conn.execute(
        """SELECT COUNT(*) AS runs, COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens,
                  COALESCE(SUM(cache_read_tokens), 0) AS cached, COALESCE(SUM(cost_usd), 0) AS cost
           FROM runs WHERE task_id = ? AND status != 'blocked'""", (task_id,)).fetchone()
    task["usage"] = {"runs": u["runs"], "tokens": u["tokens"], "cache_read_tokens": u["cached"],
                     "cost_usd": round(u["cost"], 4)}
    from . import business

    task["value_kind_effective"] = business.classify(conn, row)  # business | platform | demo
    if row["parent_id"]:
        p = conn.execute("SELECT id, title FROM tasks WHERE id = ?", (row["parent_id"],)).fetchone()
        task["parent"] = {"id": p["id"], "ref": display_id(p["id"]), "title": p["title"]}
    return task


def _view_sql(view: str, actor_id: int | None = None) -> tuple[str, list, str]:
    """(condition, params, order) for a named view."""
    t = today().isoformat()
    top = "parent_id IS NULL"
    if view == "inbox":
        return "status = 'inbox'", [], "created_at DESC"
    if view == "today":  # steps too: a step planned for today is today's work
        return ("status IN ('next', 'working', 'review', 'waiting') "
                "AND ((do_date IS NOT NULL AND do_date <= ?) OR (deadline IS NOT NULL AND deadline <= ?))",
                [t, t], "COALESCE(priority, 4), CASE energy WHEN 'high' THEN 0 ELSE 1 END, position, id")
    if view == "upcoming":
        return (f"{top} AND status IN ('next', 'working', 'waiting') AND do_date > ?", [t],
                "do_date, COALESCE(priority, 4), id")
    if view == "next":
        return (f"{top} AND status = 'next' AND do_date IS NULL "
                "AND (assignee_type IS NULL OR assignee_type = 'human')", [], "COALESCE(priority, 4), position, id")
    if view == "agents":
        return "assignee_type IN ('ai', 'agent') AND status != 'done'", [], "status, COALESCE(priority, 4), id"
    if view == "waiting":
        return ("status != 'done' AND (status = 'waiting' OR assignee_type = 'external')", [],
                "COALESCE(follow_up, '9999'), id")
    if view == "review":
        return "status = 'review'", [], "updated_at"
    if view == "to_review":  # results waiting for me as their reviewer
        return "status = 'review' AND reviewer_id = ?", [actor_id], "updated_at"
    if view == "someday":
        return f"{top} AND status = 'someday'", [], "created_at DESC"
    if view == "done":
        return "status = 'done'", [], "completed_at DESC"
    if view == "board":  # the task board: every open task, and what finished in the last seven days
        since = (today() - timedelta(days=7)).isoformat()
        return ("(status != 'done' OR completed_at >= ?)", [since], "updated_at DESC, id DESC")
    raise Invalid(f"view must be one of {VIEWS}")


def _scope_sql(conn: sqlite3.Connection, ctx: Ctx, scope: str) -> tuple[str, list]:
    """mine: assigned to me, or mine and unassigned; team: me and everyone below me; all."""
    if scope == "mine":
        return "(assignee_id = ? OR (assignee_id IS NULL AND owner_id = ?))", [ctx.actor_id, ctx.actor_id]
    if scope == "team":
        from .org import manages

        ids = [ctx.actor_id] + [r["id"] for r in conn.execute("SELECT id FROM actors WHERE archived_at IS NULL")
                                if manages(conn, ctx.actor_id, r["id"])]
        return f"(assignee_id IN ({','.join('?' for _ in ids)}) OR (assignee_id IS NULL AND owner_id = ?))", \
            [*ids, ctx.actor_id]
    if scope in ("all", "", None):
        return "1", []
    raise Invalid("scope must be mine, team or all")


def _view_for(conn: sqlite3.Connection, ctx: Ctx, view: str) -> tuple[str, list, str]:
    """The view's SQL for this member; lists and counts share it, so they agree."""
    cond, params, order = _view_sql(view, ctx.actor_id)
    if view == "to_review" and actors.get(conn, ctx.actor_id)["is_owner"]:
        # results handed in before reviewers existed wait for the owner
        cond, params = "status = 'review' AND (reviewer_id = ? OR reviewer_id IS NULL)", [ctx.actor_id]
    return cond, params, order


def list_tasks(conn: sqlite3.Connection, ctx: Ctx, view: str = "today", *, topic: str | None = None,
               assignee_id: int | None = None, limit: int = 200, scope: str = "all") -> list[dict]:
    cond, params, order = _view_for(conn, ctx, view)
    scond, sparams = _scope_sql(conn, ctx, scope)
    cond, params = f"{cond} AND {scond}", [*params, *sparams]
    vis, vparams = visible_sql(ENTITY, ctx.actor_id)
    sql = f"SELECT * FROM tasks WHERE archived_at IS NULL AND {cond} AND {vis}"
    params = [*params, *vparams]
    if topic:
        sql += " AND topic = ?"
        params.append(topic.lower())
    if assignee_id:
        sql += " AND assignee_id = ?"
        params.append(assignee_id)
    rows = conn.execute(f"{sql} ORDER BY {order} LIMIT ?", [*params, limit]).fetchall()
    out = [to_dict(r) for r in rows]
    # Step progress for projects, in one query.
    ids = [t["id"] for t in out]
    if ids:
        marks = ",".join("?" for _ in ids)
        for r in conn.execute(
            f"""SELECT parent_id, COUNT(*) AS n, SUM(status = 'done') AS done FROM tasks
                WHERE parent_id IN ({marks}) AND archived_at IS NULL GROUP BY parent_id""", ids
        ):
            for t in out:
                if t["id"] == r["parent_id"]:
                    t["steps_total"], t["steps_done"] = r["n"], r["done"]
    return out


def counts(conn: sqlite3.Connection, ctx: Ctx) -> dict[str, int]:
    vis, vparams = visible_sql(ENTITY, ctx.actor_id)
    out = {}
    for view in VIEWS:
        if view == "done":
            continue
        cond, params, _ = _view_for(conn, ctx, view)
        out[view] = conn.execute(
            f"SELECT COUNT(*) FROM tasks WHERE archived_at IS NULL AND {cond} AND {vis}", [*params, *vparams]
        ).fetchone()[0]
    return out


def topics(conn: sqlite3.Connection, ctx: Ctx) -> list[dict]:
    vis, vparams = visible_sql(ENTITY, ctx.actor_id)
    rows = conn.execute(
        f"""SELECT topic, COUNT(*) AS open FROM tasks
            WHERE topic IS NOT NULL AND archived_at IS NULL AND status != 'done' AND {vis}
            GROUP BY topic ORDER BY open DESC, topic""", vparams
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ writes

def create(conn: sqlite3.Connection, ctx: Ctx, fields: dict) -> dict:
    fields = dict(fields)
    assignee = fields.pop("assignee", None)
    reviewer = fields.pop("reviewer", None)
    project = fields.pop("project", None)
    parent_id = fields.pop("parent_id", None)
    owner = fields.pop("owner_id", None)
    goal = fields.pop("goal", None)
    source = fields.pop("source", ctx.via)
    unknown = set(fields) - EDITABLE
    if unknown:
        raise Invalid(f"unknown fields: {sorted(unknown)}")
    _validate(fields)
    if "value_kind" in fields:
        from . import business

        business.ensure_schema(conn)
    if parent_id is not None:
        parent = _row(conn, ctx, parse_id(parent_id))
        fields.setdefault("topic", parent["topic"])
        fields.setdefault("visibility", parent["visibility"])
        fields.setdefault("status", "next")
    me = actors.get(conn, ctx.actor_id)
    if assignee is None and me["kind"] == "human" and fields.get("status", "inbox") != "inbox":
        # What a person writes down is theirs unless they hand it over.
        assignee = {"type": "human", "id": me["id"]}
    if owner is None:
        # A person owns what they write down. An agent works on someone's behalf:
        # the owner of the task it splits, else the person it reports to, else the owner.
        if me["kind"] == "human":
            owner = ctx.actor_id
        elif parent_id is not None:
            owner = parent["owner_id"]
        else:
            lead = actors.get(conn, me["reports_to"]) if me["reports_to"] else None
            owner = lead["id"] if lead is not None and lead["kind"] == "human" else actors.owner_id(conn)
    now = now_iso()
    values = {
        "status": "inbox", "visibility": DEFAULT, **fields,
        "parent_id": parse_id(parent_id) if parent_id is not None else None,
        "owner_id": owner, "source": source, "created_by": ctx.actor_id,
        "created_at": now, "updated_at": now,
        **resolve_assignee(conn, ctx, assignee),
        **({"reviewer_id": resolve_reviewer(conn, ctx, reviewer)} if reviewer not in (None, "") else {}),
        **({"project_id": resolve_project(conn, ctx, project)} if project not in (None, "") else {}),
    }
    if values.get("project_id") is None and parent_id is not None:
        values["project_id"] = parent["project_id"]  # a step belongs to its task's project
    if values.get("topic"):
        values["topic"] = values["topic"].lower().lstrip("#")
    if values.get("project_id") is None and parent_id is None and project is None:
        # A task for a known repo, customer or label joins its project (pos.project_info.match_project).
        from . import project_info

        values["project_id"] = project_info.match_project(conn, values)
        if values["project_id"] is None:
            values.pop("project_id")
    refuse_owner_assignee(conn, ctx, values.get("assignee_id"), values.get("source"))
    if me["kind"] != "human" and parent_id is None:
        _work_context(conn, ctx, values, goal)  # every agent's task has a project or goal and a value kind
    from . import business

    dup = business.find_duplicate_escalation(conn, ctx, values)
    if dup is not None:
        # The same item escalated again (one invoice, three tasks): link it to the open escalation.
        business.link_duplicate(conn, ctx, dup, values)
        out = get(conn, ctx, dup)
        out["deduplicated"] = True
        out["note"] = (f"Not created: {display_id(dup)} already escalates this item; your note is a comment there. "
                       "Pass it on with handoff_task if it should move.")
        return out
    if values["assignee_type"] == "external":
        values["status"] = "waiting" if values["status"] in ("inbox", "next") else values["status"]
        values.setdefault("follow_up", (today() + timedelta(days=3)).isoformat())
    elif values["assignee_type"] in ("ai", "agent") and values["status"] == "inbox":
        values["status"] = "next"
    if values["status"] == "done":
        values["completed_at"] = now
    if task_descriptions.needs_description(values.get("notes")):
        # Every task says what it is for, where it came from and what done looks like.
        values["notes"] = task_descriptions.build(conn, values)
        values["description_generated"] = 1
    new_id = versioning.insert(conn, ctx, ENTITY, values)["id"]
    if goal not in (None, ""):
        from . import goals

        goals.link_to(conn, ctx, int(goal), display_id(new_id))
    if me["kind"] != "human" and values.get("assignee_id") == actors.owner_id(conn):
        from . import head_alerts

        head_alerts.assigned(conn, ctx, new_id)  # an agent's task for the owner: its head hears of it
    return get(conn, ctx, new_id)


def _default_project(conn: sqlite3.Connection, actor_id: int | None) -> int | None:
    """The project an agent's work belongs to by default: the active project named after its team
    (#kniha for the Kniha team), else the one active project it leads."""
    if not actor_id:
        return None
    a = conn.execute("SELECT team, kind FROM actors WHERE id = ?", (actor_id,)).fetchone()
    if a is None or a["kind"] == "human":
        return None
    try:
        if a["team"]:
            p = conn.execute("SELECT id FROM projects WHERE lower(slug) = lower(?) AND status = 'active'",
                             (a["team"],)).fetchone()
            if p:
                return p["id"]
        led = conn.execute("SELECT id FROM projects WHERE lead_id = ? AND status = 'active'", (actor_id,)).fetchall()
    except sqlite3.OperationalError:
        return None
    return led[0]["id"] if len(led) == 1 else None


def _work_context(conn: sqlite3.Connection, ctx: Ctx, values: dict, goal) -> None:
    """An agent's new top-level task carries its project (or a goal) and its value kind, so the cost
    and the work count where they belong (business vs platform). Defaults: the creator's team project
    or the project it leads (else the assignee's); the value kind from pos.business.classify, which
    counts the project and the team (Kniha roles are business)."""
    from . import business

    if goal not in (None, ""):
        from . import goals

        goals.get(conn, int(goal))  # exists (a wrong goal fails before the task is written)
    elif values.get("project_id") is None:
        pid = _default_project(conn, ctx.actor_id) or _default_project(conn, values.get("assignee_id"))
        if pid:
            values["project_id"] = pid
    if not values.get("value_kind"):
        business.ensure_schema(conn)
        kind = business.classify(conn, {"id": None, "parent_id": None, "value_kind": None, "title": "",
                                        "topic": None, "source": None, "assignee_id": None, "created_by": None,
                                        "project_id": None, **values})
        if kind in ("business", "platform"):
            values["value_kind"] = kind


def capture(conn: sqlite3.Connection, ctx: Ctx, text: str, source: str | None = None) -> dict:
    """Quick capture. Plain text lands in the inbox; with a date or an
    assignee the task is already clear enough to skip it."""
    parsed = capture_syntax.parse(text)
    status = "next" if parsed.get("do_date") or parsed.get("assignee") or parsed.get("deadline") else "inbox"
    return create(conn, ctx, {"status": status, **parsed, "source": source or ctx.via})


def update(conn: sqlite3.Connection, ctx: Ctx, task_id: int, changes: dict) -> dict:
    changes = dict(changes)
    row = _row(conn, ctx, task_id)
    if set(changes) - {"progress", "progress_note", "status"} or changes.get("status") not in (None, "done", "review"):
        # Handing work in or reporting on it is the assignee's; everything else needs the right to write.
        check_write(conn, ENTITY, row, ctx.actor_id)
    extra = {}
    if "project" in changes:
        value = changes.pop("project")
        extra["project_id"] = resolve_project(conn, ctx, value) if value not in (None, "") else None
    if "reviewer" in changes:
        value = changes.pop("reviewer")
        extra["reviewer_id"] = resolve_reviewer(conn, ctx, value) if value not in (None, "") else None
    if "assignee" in changes:
        extra.update(resolve_assignee(conn, ctx, changes.pop("assignee")))
        refuse_owner_assignee(conn, ctx, extra.get("assignee_id"), row["source"])
    unknown = set(changes) - EDITABLE
    if unknown:
        raise Invalid(f"unknown fields: {sorted(unknown)}")
    _validate(changes)
    if "value_kind" in changes:
        from . import business

        business.ensure_schema(conn)
    if changes.get("status") in ("inbox", "next", "working") and "assignee_id" not in extra:
        _refuse_archived(conn, row)  # reopening work for a member who is gone (T-026 went to the archived Dev agent)
    if "visibility" in changes and changes["visibility"] != row["visibility"]:
        from .integrations import check_visibility_change

        check_visibility_change(conn, ctx, row, changes["visibility"])
    if changes.get("topic"):
        changes["topic"] = changes["topic"].lower().lstrip("#")
    if "notes" in changes and (changes["notes"] or "") != row["notes"]:
        if task_descriptions.needs_description(changes["notes"]):
            # Clearing the description brings the generated one back, never an empty task.
            changes["notes"] = task_descriptions.build(conn, {**dict(row), **changes}, task_id)
            extra["description_generated"] = 1
        else:
            extra["description_generated"] = 0
    accepted = False
    routine_closed = False
    if changes.get("status") == "done" and row["status"] != "done":
        # Nobody gets around review: marking a result under review done is an
        # accept (by someone who may review it); anyone else hands work in.
        if row["status"] == "review":
            ok, why = may_review(conn, ctx, row)
            if not ok:
                raise Forbidden(why)
            accepted = True
        elif not may_finish(conn, ctx, row):
            from . import schedules

            if schedules.closes_itself(conn, ctx, row):
                routine_closed = True  # a daily check: green is done, findings become work (pos.schedules)
            else:
                changes["status"] = "review"
    handed_in = changes.get("status") == "review" and row["status"] != "review"
    triaged = None
    policy = None
    if handed_in and "reviewer_id" not in extra:
        # The risk-class review policy (pos.review_policy): low risk is accepted at once, code goes to
        # the QA Reviewer. An explicit request_review and a reviewer the owner set are respected.
        from . import review_policy

        if row["reviewer_id"] != actors.owner_id(conn):
            policy = review_policy.decide(conn, row, changes.get("progress_note") or row["progress_note"])
            if policy.action == "route" and not row["reviewer_id"]:
                extra["reviewer_id"] = policy.reviewer_id
    if handed_in and not row["reviewer_id"] and "reviewer_id" not in extra:
        extra["reviewer_id"] = reviewer_of(conn, row)
        if extra["reviewer_id"] == actors.owner_id(conn):
            # The owner's reviews go to a stand-in first (the team lead, the CEO for what needs
            # judgment); it leaves him only what truly needs him (pos.review_policy.owner_stand_in).
            from . import business

            triaged = business.review_triage_target(conn, row, changes.get("progress_note") or row["progress_note"])
            if triaged:
                extra["reviewer_id"] = triaged
    if "status" in changes:
        if changes["status"] == "done" and row["status"] != "done":
            extra["completed_at"] = now_iso()
        elif changes["status"] != "done":
            extra["completed_at"] = None
    versioning.update(conn, ctx, ENTITY, task_id, {**changes, **extra}, action="accept" if accepted else "update")
    if row["status"] == "review" and changes.get("status") not in (None, "review"):
        from . import review_work

        review_work.close_for(conn, task_id, "accepted" if accepted else f"the result is {changes['status']} now")
    if triaged:
        from . import audit

        audit.log(conn, ctx, "review_triage", ENTITY, task_id, to=triaged)
    if not accepted and set(changes) - {"status", "progress", "progress_note", "position", "value_kind"}:
        from . import business

        business.record_intervention(conn, ctx, task_id, "edit", ", ".join(sorted(changes)))
    auto_accepted = bool(handed_in and policy and policy.action == "accept")
    if auto_accepted:
        from . import review_policy

        review_policy.auto_accept(conn, task_id, policy.reason, follow_ups=False)
    if handed_in:
        from . import verification

        nudge = verification.on_hand_in(conn, ctx, row, changes.get("progress_note"))
    if routine_closed:
        from . import schedules

        schedules.after_check(conn, ctx, task_id, changes.get("progress_note") or row["progress_note"])
    out = get(conn, ctx, task_id)
    if out["status"] == "done" and row["status"] != "done":
        from . import asks

        asks.on_task_changed(conn, ctx, row, out)  # an answered ask_owner ticket reaches the asker
    if out["status"] in ("review", "done") and row["status"] not in ("review", "done")             and (row["source"] or "").startswith("support:"):
        from .support import service as support_service

        support_service.on_task_changed(conn, ctx, row, out)  # a customer's fix handed in: the reply draft is next
    if handed_in and not auto_accepted:
        _ask_reviewer(conn, ctx, out)
    if handed_in and nudge:
        out["platform_note"] = nudge
    if auto_accepted:
        from .agents import retire_if_done

        retire_if_done(conn, ctx, out["assignee_id"])
    if accepted:
        from . import comments
        from .agents import retire_if_done

        comments.log(conn, ctx, task_id, "Accepted" + (f": {changes['progress_note']}"
                                                       if changes.get("progress_note") else ""), "review")
        retire_if_done(conn, ctx, out["assignee_id"])
    return out


# ------------------------------------------------------------------ review between colleagues (3.2)

def _refuse_archived(conn: sqlite3.Connection, row) -> None:
    if row["assignee_id"] and row["assignee_type"] in ("ai", "agent", "human"):
        a = conn.execute("SELECT name, archived_at FROM actors WHERE id = ?", (row["assignee_id"],)).fetchone()
        if a and a["archived_at"]:
            raise Invalid(f"{display_id(row['id'])} is assigned to {a['name']}, who is archived: "
                          "reassign it to its successor first")


def hand_review(conn: sqlite3.Connection, ctx: Ctx, task_id: int, reviewer_id: int, why: str) -> bool:
    """Move a task's review to another member the proper way: a versioned change, and when the
    result is waiting for review now, the new reviewer hears of it (DM + wake), unlike a raw
    UPDATE (the 2026-09 reorganisation moved T-070/071/072/075/092/105 silently). True when
    the reviewer was told."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        raise NotFound(display_id(task_id))
    if row["reviewer_id"] != reviewer_id:
        versioning.update(conn, ctx, ENTITY, task_id, {"reviewer_id": reviewer_id}, action="reviewer")
    if row["status"] != "review":
        return False
    return notify_reviewer(conn, ctx, task_id, why)


def notify_reviewer(conn: sqlite3.Connection, ctx: Ctx, task_id: int, why: str) -> bool:
    """Tell the reviewer of a task waiting for review (again). An agent reviewer gets a review work
    item in its queue and is woken (pos.review_work: a DM alone started no run); a person (not the
    owner, who sees Needs review on the board) gets a DM, never in the owner's name."""
    from . import chat, review_work, wake

    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None or row["status"] != "review" or row["archived_at"]:
        return False
    rid = reviewer_of(conn, row)
    r = actors.get(conn, rid)
    if r["archived_at"] or r["is_owner"] or rid == ctx.actor_id:
        return False  # the owner sees Needs review on the board; nobody DMs themself
    if r["kind"] != "human":
        if review_work.ensure(conn, task_id, why) is None:
            return False
        wake.wake(rid)
        return True
    chat.send_dm(conn, _not_owner(conn, ctx), rid,
                 f"{display_id(task_id)} '{row['title']}' waits for your review ({why}). "
                 "Accept it or return it with what should change (review_task).",
                 priority="fyi", attachments=[{"type": "task", "id": task_id}], system=True)
    return True


def _not_owner(conn: sqlite3.Connection, ctx: Ctx) -> Ctx:
    """The platform's notices are never sent in the owner's name (pos.business.system_ctx)."""
    if conn.execute("SELECT is_owner FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()[0]:
        from .business import system_ctx

        return system_ctx(conn)
    return ctx


def resolve_project(conn: sqlite3.Connection, ctx: Ctx, value) -> int:
    """A project by id or slug that the caller can see."""
    from . import projects

    return projects._row(conn, ctx, value)["id"]


def list_project(conn: sqlite3.Connection, ctx: Ctx, project_id: int) -> list[dict]:
    """A project's tasks the caller can see, for its board."""
    vis, vparams = visible_sql(ENTITY, ctx.actor_id)
    rows = conn.execute(
        f"""SELECT * FROM tasks WHERE project_id = ? AND archived_at IS NULL AND {vis}
            ORDER BY CASE status WHEN 'working' THEN 0 WHEN 'review' THEN 1 WHEN 'next' THEN 2 ELSE 3 END,
            COALESCE(priority, 4), position, id""", [project_id, *vparams]).fetchall()
    return [to_dict(r) for r in rows]


def resolve_reviewer(conn: sqlite3.Connection, ctx: Ctx, value) -> int:
    """A member (id, name, 'me' or 'ai') who reviews the result; never someone outside."""
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        value = {"type": "human", "id": int(value)}  # the actor's real kind comes from its row
    cols = resolve_assignee(conn, ctx, value)
    if cols["assignee_type"] in (None, "external"):
        raise Invalid(f"reviewer must be a member of PersonalOS, not {value!r}")
    return cols["assignee_id"]


def _can_review_as_agent(conn: sqlite3.Connection, actor_id: int) -> bool:
    row = actors.get(conn, actor_id)
    if row["kind"] == "human":
        return not row["archived_at"]
    from .agents import has_permission

    return not row["archived_at"] and has_permission(conn, actor_id, "tasks:review")


def reviewer_of(conn: sqlite3.Connection, row) -> int:
    """Who reviews this task's result: the one set on it, else whoever asked
    for it, else the assignee's lead, else the owner. Agents only with tasks:review."""
    if row["reviewer_id"]:
        return row["reviewer_id"]
    assignee = row["assignee_id"]
    from .hiring import on_probation

    if on_probation(conn, assignee):
        lead = actors.get(conn, assignee)["reports_to"]
        if lead and _can_review_as_agent(conn, lead):
            return lead
    from .projects import lead_of_task

    project_lead = lead_of_task(conn, row)
    if project_lead and project_lead != assignee and _can_review_as_agent(conn, project_lead):
        return project_lead
    creator = row["created_by"]
    if creator and creator != assignee and _can_review_as_agent(conn, creator):
        return creator
    if assignee:
        lead = actors.get(conn, assignee)["reports_to"]
        if lead and lead != assignee and _can_review_as_agent(conn, lead):
            return lead
    return actors.owner_id(conn)


def may_review(conn: sqlite3.Connection, ctx: Ctx, row) -> tuple[bool, str]:
    """(allowed, why not): the reviewer, the assignee's lead or the owner; nobody
    approves their own work; an agent needs tasks:review and may not review what
    it has just handed to the assignee (no circles)."""
    me = actors.get(conn, ctx.actor_id)
    if me["is_owner"]:
        return True, ""
    ref = display_id(row["id"])
    if row["assignee_id"] == ctx.actor_id:
        return False, f"nobody approves their own work ({ref})"
    if me["kind"] != "human":
        if not _can_review_as_agent(conn, ctx.actor_id):
            return False, "reviewing needs the permission tasks:review"
        last = conn.execute("SELECT from_actor FROM handoffs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                            (row["id"],)).fetchone()
        if last and last["from_actor"] == ctx.actor_id:
            return False, f"you handed {ref} over yourself; someone else reviews it"
    from .org import manages

    if ctx.actor_id == reviewer_of(conn, row) or manages(conn, ctx.actor_id, row["assignee_id"]):
        return True, ""
    return False, f"only the reviewer of {ref}, the assignee's lead or the owner review it"


def may_finish(conn: sqlite3.Connection, ctx: Ctx, row) -> bool:
    """May mark the task done without a review: your own task you review
    yourself (no one else reviews it), or someone who may review it anyway."""
    if row["assignee_id"] in (None, ctx.actor_id) and reviewer_of(conn, row) == ctx.actor_id:
        return True
    if row["source"] == "ask_owner" and row["assignee_id"] == ctx.actor_id:
        return True  # an answer to an agent's question is not work for the asker to review (pos.asks)
    return row["assignee_id"] != ctx.actor_id and may_review(conn, ctx, row)[0]


def request_review(conn: sqlite3.Connection, ctx: Ctx, task_id: int, reviewer, note: str = "") -> dict:
    """Hand the result in to a chosen colleague for review."""
    row = _row(conn, ctx, task_id)
    rid = resolve_reviewer(conn, ctx, reviewer)
    if rid == row["assignee_id"]:
        raise Invalid("nobody reviews their own work: pick someone else")
    if row["status"] == "done":
        raise Invalid(f"{display_id(task_id)} is done")
    changes = {"reviewer": rid, "status": "review", "progress": 100}
    if note:
        changes["progress_note"] = note
    return update(conn, ctx, task_id, changes)


def _ask_reviewer(conn: sqlite3.Connection, ctx: Ctx, task: dict) -> None:
    """A result was handed in: the reviewer hears of it (the owner sees Needs review)."""
    rid = task.get("reviewer_id")
    if not rid or rid == ctx.actor_id:
        return
    r = actors.get(conn, rid)
    if r["is_owner"] or r["archived_at"]:
        return
    from . import chat, review_work, wake

    by = actors.get(conn, ctx.actor_id)["name"]
    if r["kind"] != "human":
        # An agent reviews as work: an item in its queue starts its run (pos.review_work).
        if review_work.ensure(conn, task["id"], f"handed in by {by}") is not None:
            wake.wake(rid)
        return
    chat.send_dm(conn, ctx, rid, f"{by} handed in {task['ref']} '{task['title']}' for your review. "
                                 "Accept it or return it with what should change (review_task). "
                                 f"Report: /report/{task['ref']}",
                 priority="fyi", attachments=[{"type": "task", "id": task["id"]}], system=True)


def _no_pseudo_tools(conn: sqlite3.Connection, ctx: Ctx, text: str | None) -> None:
    """An agent's result or progress note with tool calls written as text (pos.pseudo_tools) is
    refused: those calls never ran, so the "work" it describes did not happen."""
    from . import pseudo_tools

    if not pseudo_tools.contains(text):
        return
    who = conn.execute("SELECT kind FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()
    if who is not None and who["kind"] == "human":
        return
    raise Invalid("Your text contains tool calls written as text (<function_calls>/<invoke …>); they were NOT "
                  "executed, so nothing of it happened. Call the real tools, then report what they actually returned.")


def complete(conn: sqlite3.Connection, ctx: Ctx, task_id: int, note: str | None = None) -> dict:
    """People finish tasks; AI and agents hand results in for review. A person
    completing a task that waits for review accepts it."""
    row = _row(conn, ctx, task_id)
    _no_pseudo_tools(conn, ctx, note)
    if row["status"] == "review" and may_review(conn, ctx, row)[0]:
        return review(conn, ctx, task_id, True, note)
    changes: dict = {"progress": 100, "status": "done"}  # update() turns it into a hand-in when needed
    if note:
        changes["progress_note"] = note
    out = update(conn, ctx, task_id, changes)
    if row["topic"] == "chat":  # an answer task closed without an answer: the result goes into the thread
        from . import availability

        availability.answer_if_silent(conn, ctx, task_id, note)
    if out["status"] == "done":
        from .agents import retire_if_done

        retire_if_done(conn, ctx, out["assignee_id"])
    return out


def review(conn: sqlite3.Connection, ctx: Ctx, task_id: int, accept: bool, comment: str | None = None) -> dict:
    row = _row(conn, ctx, task_id)
    if row["status"] != "review":
        raise Invalid(f"{display_id(task_id)} is not waiting for review")
    ok, why = may_review(conn, ctx, row)
    if not ok:
        raise Forbidden(why)
    if accept:  # update() records the accept and retires a one-shot agent
        return update(conn, ctx, task_id, {"status": "done", **({"progress_note": comment} if comment else {})})
    _refuse_archived(conn, row)
    versioning.update(conn, ctx, ENTITY, task_id, {
        "status": "next", "progress": 0, "completed_at": None,
        "returned_count": row["returned_count"] + 1,
        "progress_note": f"Returned: {comment}" if comment else "Returned",
    }, action="return")
    from . import business, comments, review_work

    review_work.close_for(conn, task_id, "returned")

    comments.log(conn, ctx, task_id, f"Returned: {comment}" if comment else "Returned", "return")
    business.record_intervention(conn, ctx, task_id, "return", comment or "")
    from . import learning

    learning.on_return(conn, ctx, row, comment)  # the reviewer's comment becomes the agent's lesson
    return get(conn, ctx, task_id)


def intervene(conn: sqlite3.Connection, ctx: Ctx, task_id: int, note: str = "") -> dict:
    """The owner had to step in on work an AI or agent was doing."""
    row = _row(conn, ctx, task_id)
    if actors.get(conn, ctx.actor_id)["kind"] != "human":
        raise Forbidden("only people record an intervention")
    versioning.update(conn, ctx, ENTITY, task_id, {"interventions": row["interventions"] + 1},
                      action="intervene")
    if note:
        update(conn, ctx, task_id, {"progress_note": f"Owner stepped in: {note}"})
    return get(conn, ctx, task_id)


def assign(conn: sqlite3.Connection, ctx: Ctx, task_id: int, assignee) -> dict:
    row = _row(conn, ctx, task_id)
    cols = resolve_assignee(conn, ctx, assignee)
    refuse_owner_assignee(conn, ctx, cols.get("assignee_id"), row["source"])
    changes: dict = {}
    if cols["assignee_type"] == "external":
        changes["status"] = "waiting"
        if not row["follow_up"]:
            changes["follow_up"] = (today() + timedelta(days=3)).isoformat()
    elif row["status"] in ("inbox", "waiting"):
        changes["status"] = "next"
    if row["retry_after"] and actors.get(conn, ctx.actor_id)["kind"] == "human":
        changes["retry_after"] = None  # a person hands it out again: no back-off
    versioning.update(conn, ctx, ENTITY, task_id, {**cols, **changes}, action="assign")
    if cols.get("assignee_id") == actors.owner_id(conn) and row["assignee_id"] != cols.get("assignee_id"):
        from . import head_alerts

        head_alerts.assigned(conn, ctx, task_id, row["assignee_id"])
    return get(conn, ctx, task_id)


def claim(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    """An agent takes a task from its queue and starts working."""
    from .killswitch import check_agent_may_act

    check_agent_may_act(conn, ctx)
    row = _row(conn, ctx, task_id)
    if row["assignee_id"] not in (None, ctx.actor_id):
        raise Forbidden(f"{display_id(task_id)} is assigned to someone else")
    if row["status"] not in ("next", "inbox"):
        raise Invalid(f"{display_id(task_id)} is {row['status']}, not claimable")
    changes = {"status": "working", "progress": 0}
    if row["assignee_id"] is None:
        changes.update(resolve_assignee(conn, ctx, {"type": "agent", "id": ctx.actor_id}))
    versioning.update(conn, ctx, ENTITY, task_id, changes, action="claim")
    return get(conn, ctx, task_id)


def report_progress(conn: sqlite3.Connection, ctx: Ctx, task_id: int, percent: int, message: str = "") -> dict:
    if not 0 <= percent <= 100:
        raise Invalid("percent must be 0 to 100")
    _no_pseudo_tools(conn, ctx, message)
    out = update(conn, ctx, task_id, {"progress": percent, "progress_note": message or None})
    if message:
        from . import comments

        comments.log(conn, ctx, task_id, f"{percent} %: {message}", "progress")
    return out


def archive(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    check_write(conn, ENTITY, _row(conn, ctx, task_id), ctx.actor_id)
    versioning.archive(conn, ctx, ENTITY, task_id)
    return to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())


def unarchive(conn: sqlite3.Connection, ctx: Ctx, task_id: int) -> dict:
    _row(conn, ctx, task_id)
    versioning.unarchive(conn, ctx, ENTITY, task_id)
    return get(conn, ctx, task_id)


def set_suggestion(conn: sqlite3.Connection, ctx: Ctx, task_id: int, suggestion: dict) -> dict:
    _row(conn, ctx, task_id)
    versioning.update(conn, ctx, ENTITY, task_id, {"suggestion": json.dumps(suggestion, ensure_ascii=False)},
                      action="suggest")
    return get(conn, ctx, task_id)


# ------------------------------------------------------------------ clarify (GTD)

CLARIFY_ACTIONS = ("accept", "do_now", "delegate", "someday", "reference", "trash")


def clarify(conn: sqlite3.Connection, ctx: Ctx, task_id: int, action: str, fields: dict | None = None) -> dict:
    """Process one inbox item. `accept` applies the AI suggestion (optionally
    edited through `fields`), including its steps."""
    if action not in CLARIFY_ACTIONS:
        raise Invalid(f"action must be one of {CLARIFY_ACTIONS}")
    row = _row(conn, ctx, task_id)
    fields = dict(fields or {})
    if action == "accept":
        sug = json.loads(row["suggestion"]) if row["suggestion"] else {}
        steps = fields.pop("steps", sug.get("steps") or [])
        base = {k: sug.get(k) for k in ("title", "topic", "priority", "do_date", "deadline", "estimate_min",
                                        "energy", "definition_of_done") if sug.get(k) is not None}
        changes = {**base, **fields, "status": "next", "suggestion": None}
        assignee = changes.pop("assignee", sug.get("assignee"))
        if assignee:
            changes.update(resolve_assignee(conn, ctx, assignee))
        clean = {k: v for k, v in changes.items() if k in EDITABLE or k.startswith("assignee_") or k == "suggestion"}
        _validate(clean)
        versioning.update(conn, ctx, ENTITY, task_id, clean, action="clarify:accept")
        for i, step in enumerate(steps):
            create(conn, ctx, {
                "title": step["title"], "parent_id": task_id, "position": i,
                "estimate_min": step.get("estimate_min"), "assignee": step.get("assignee"),
                "notes": step.get("reason") or "",
            })
        return get(conn, ctx, task_id)
    if action == "do_now":
        return update(conn, ctx, task_id, {"status": "done"})
    if action == "delegate":
        if not fields.get("assignee"):
            raise Invalid("delegate needs an assignee")
        return assign(conn, ctx, task_id, fields["assignee"])
    if action == "someday":
        return update(conn, ctx, task_id, {"status": "someday"})
    if action == "reference":
        # Not actionable but worth keeping: becomes a note, the item is archived.
        now = now_iso()
        versioning.insert(conn, ctx, "note", {
            "title": row["title"], "body": "" if row["description_generated"] else row["notes"], "topic": row["topic"],
            "visibility": row["visibility"], "owner_id": row["owner_id"], "created_at": now, "updated_at": now,
        })
    return archive(conn, ctx, task_id)
