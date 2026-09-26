"""Why an agent cannot run right now, in Czech, and the platform's reply when
a person waits for it in chat (docs/CHAT.md).

A message from the owner is never left unanswered: it becomes a task for the
agent (pos.chat._ask_to_answer). When that agent cannot run (paused, the kill
switch, both subscriptions at their usage limit, its budget used up), the
platform answers in the thread at once, code-built and without a model: that
it cannot run, why, and when it tries again. The task stays in the queue, so
the agent answers for real as soon as it can.
"""

import json
import sqlite3
from datetime import datetime, timezone

from . import actors, audit
from .core import TZ, Ctx, now_iso


def when_cz(iso: str | None) -> str:
    """'dnes ve 23:20', 'zítra v 8:47', '29. 9. v 8:47' (Prague time)."""
    if not iso:
        return "později"
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    local, today = t.astimezone(TZ), datetime.now(TZ).date()
    hm = f"{local.hour}:{local.minute:02d}"
    prep = "ve" if local.hour in (2, 3, 4, 12, 13, 14, 20, 21, 22, 23) else "v"  # ve dvě, ve dvacet…
    if local.date() == today:
        return f"dnes {prep} {hm}"
    if (local.date() - today).days == 1:
        return f"zítra {prep} {hm}"
    return f"{local.day}. {local.month}. {prep} {hm}"


def why_not(conn: sqlite3.Connection, actor_id: int) -> dict | None:
    """None when the agent can start a run now; else {"reason", "retry", "until"}
    in Czech (until: ISO time of the next attempt, when known)."""
    from . import engines, killswitch

    a = actors.get(conn, actor_id)
    if a["archived_at"]:
        return {"reason": "je archivovaný", "retry": "až ho obnovíš", "until": None}
    if a["paused_at"]:
        return {"reason": "je pozastavený", "retry": "až ho znovu spustíš", "until": None}
    if killswitch.is_frozen(conn):
        return {"reason": "je zapnutý nouzový vypínač (kill switch), žádný agent teď neběží",
                "retry": "po jeho vypnutí", "until": None}
    engine, _why, _model = engines.choose(conn, actor_id)
    if engine is None:
        row = conn.execute("SELECT engine, model FROM actors WHERE id = ?", (actor_id,)).fetchone()
        wanted = (row["engine"] if row and row["engine"] else None) or engines.default_engine()
        order = [wanted] if wanted in engines.ENGINES else engines.auto_order()
        limits = {e: engines.paused_until(conn, e) for e in order}
        parts = [f"{e.capitalize()} do {when_cz(u)}" for e, u in limits.items() if u]
        until = min((u for u in limits.values() if u), default=None)
        names = " ani ".join(e.capitalize() for e in order)
        return {"reason": f"AI teď není k dispozici: {names} má vyčerpaný limit předplatného"
                          + (f" ({', '.join(parts)})" if parts else ""),
                "retry": when_cz(until) if until else "za hodinu", "until": until}
    budget = _budget_block(conn, actor_id)
    if budget:
        return budget
    return None


def _budget_block(conn: sqlite3.Connection, actor_id: int) -> dict | None:
    from .access import service as access
    from .access import store as access_store

    if not access_store.ready(conn):
        return None
    for who, label in ((None, "firemní strop"), (actor_id, "jeho rozpočet")):
        for metric in access.GATED:
            lim = access.limit(conn, who, metric)
            if lim is not None and access.used(conn, who, metric) >= lim:
                return {"reason": f"{label} je vyčerpaný ({access.METRICS[metric]}: "
                                  f"{access._fmt(metric, access.used(conn, who, metric))} z {access._fmt(metric, lim)})",
                        "retry": "až se uvolní okno rozpočtu, nebo hned, když limit zvýšíš", "until": None}
    return None


def chat_link(conn: sqlite3.Connection, task_id: int) -> dict | None:
    """The chat channel and message a 'Chat: answer' task answers (pos.chat)."""
    row = conn.execute("SELECT detail FROM audit_log WHERE action = 'chat_task' AND entity = 'task' "
                       "AND entity_id = ? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row["detail"]) if row else None


def autoreply(conn: sqlite3.Connection, task_id: int, message_id: int | None = None,
              why: dict | None = None, refused: str | None = None) -> dict | None:
    """The platform's reply in the thread when the agent of a chat task cannot
    run. Once per message; returns the posted message, or None."""
    from . import chat

    link = chat_link(conn, task_id)
    t = conn.execute("SELECT assignee_id, status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not link or t is None or t["status"] not in ("inbox", "next", "working"):
        return None
    aid = t["assignee_id"]
    why = why or why_not(conn, aid)
    if why is None and refused:  # refused for a reason this module does not know: say it as it is
        why = {"reason": f"platforma odmítla spustit běh ({refused[:200]})", "retry": "za pár minut", "until": None}
    if why is None:
        return None
    mid = message_id or link.get("message")
    done = conn.execute("SELECT 1 FROM audit_log WHERE action = 'chat_autoreply' AND entity = 'task' AND entity_id = ? "
                        "AND json_extract(detail, '$.message') = ?", (task_id, mid)).fetchone()
    if done:
        return None
    a = actors.get(conn, aid)
    name = a["name"]
    body = (f"Automatická odpověď platformy: {name} teď nemůže odpovědět, protože {why['reason']}. "
            f"Zprávu má ve frontě a zkusí to znovu {why['retry']}; odpoví hned, jak to půjde.")
    try:
        chat._add_member(conn, link["channel"], aid)
        out = chat.send(conn, Ctx(aid, via="system"), link["channel"], body, reply_to=mid, system=True)
    except Exception:  # noqa: BLE001 - the task stays queued either way
        return None
    audit.log(conn, Ctx(aid, via="system"), "chat_autoreply", "task", task_id, message=mid, reason=why["reason"],
              until=why.get("until"), at=now_iso())
    conn.commit()
    return out
