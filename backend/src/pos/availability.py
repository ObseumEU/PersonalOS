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

from . import actors, audit, roles
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
    from . import workers

    down = workers.worker_down(conn, a)
    if down:
        return {"reason": down, "retry": "jakmile worker znovu naběhne (Hlídač o tom dostal incident)",
                "until": None}
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


def post_answer(conn: sqlite3.Connection, task_id: int, author_id: int, text: str) -> dict | None:
    """The answer of a remote (A2A) agent to a chat task goes into the thread it
    answers (its worker is another app and cannot post itself)."""
    from . import chat

    link = chat_link(conn, task_id)
    text = (text or "").strip()
    if not link or not text:
        return None
    try:
        chat._add_member(conn, link["channel"], author_id)
        out = chat.send(conn, Ctx(author_id, via="a2a"), link["channel"], text[:chat.MAX_BODY],
                        reply_to=link.get("message"), system=True)
    except Exception:  # noqa: BLE001 - the task result keeps the answer either way
        return None
    audit.log(conn, Ctx(author_id, via="a2a"), "chat_a2a_answer", "task", task_id, message=out["id"])
    conn.commit()
    return out


def answer_if_silent(conn: sqlite3.Connection, ctx: Ctx, task_id: int, note: str | None) -> dict | None:
    """An agent closed its "Chat: answer" task without posting in the chat (it
    could not, or forgot): its result goes into the thread, marked as delivered
    by the platform, so the person is never left without an answer."""
    from . import chat

    if ctx.via == "a2a":  # the A2A bridge posts the remote answer itself (post_answer)
        return None
    link = chat_link(conn, task_id)
    t = conn.execute("SELECT assignee_id, created_at, progress_note FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not link or t is None or t["assignee_id"] != ctx.actor_id:
        return None
    a = actors.get(conn, ctx.actor_id)
    if a["kind"] == "human":
        return None
    after = link.get("message") or 0
    if conn.execute("""SELECT 1 FROM chat_messages WHERE channel_id = ? AND author_id = ? AND archived_at IS NULL
                       AND (id > ? OR created_at >= ?)""", (link["channel"], a["id"], after, t["created_at"])).fetchone():
        return None
    text = (note or t["progress_note"] or "").strip()
    body = (f"{text}\n\n_(Doručila platforma: {a['name']} úkol uzavřel bez odpovědi v chatu; toto je jeho výsledek.)_"
            if text else f"{a['name']} úkol uzavřel bez odpovědi a bez výsledku. Napiš mu prosím znovu, "
                         "nebo se podívej do jeho úkolů.")
    try:
        chat._add_member(conn, link["channel"], a["id"])
        out = chat.send(conn, Ctx(a["id"], via="system"), link["channel"], body[:chat.MAX_BODY],
                        reply_to=link.get("message"), system=True)
    except Exception:  # noqa: BLE001
        return None
    audit.log(conn, Ctx(a["id"], via="system"), "chat_answer_from_result", "task", task_id, message=out["id"])
    return out


def forward_unserved(conn: sqlite3.Connection, ctx: Ctx, ch, target, message_id: int, body: str,
                     why: str | None = None) -> dict | None:
    """The owner wrote to a member that has no worker (a service such as the
    Deployer, or an agent whose worker is missing): the platform answers at once
    in the thread, in Czech and without a model, and hands the message to the
    CEO, who answers the owner. Once per message."""
    from . import chat, tasks, workers
    from .guard.external import wrap_external

    if conn.execute("SELECT 1 FROM audit_log WHERE action = 'chat_forwarded' AND entity = 'chat_message' "
                    "AND entity_id = ?", (message_id,)).fetchone():
        return None
    name = target["name"]
    service = workers.is_service(target)
    what = (f"je služba platformy, ne agent ({workers.SERVICES.get(name, 'automatizace bez AI')})"
            if service else "nemá workera, který by mohl odpovědět")
    if why:
        what = f"teď neběží, protože {why}"
    author = actors.get(conn, ctx.actor_id)
    # The top of the chain answers for services (the CEO; the COO where there is no CEO yet).
    pm = actors.find_by_name(conn, roles.CEO) or actors.find_by_name(conn, roles.COO)
    if pm is None or pm["id"] == target["id"] or pm["archived_at"] or workers.reply_path(conn, pm) is None:
        pm = None
    task = None
    if pm is not None:
        if ch["kind"] == "dm":  # the PM is not in this DM: it answers in its own DM with the owner
            answer_ch = chat.dm_channel(conn, author["id"], pm["id"], Ctx(author["id"], via="system"))
            reply_to, where = None, "v přímé zprávě"
        else:
            answer_ch, reply_to, where = ch, message_id, "tady ve vlákně"
        said = wrap_external(f"chat:{author['name']}", body, ref=f"message {message_id}")
        kind = "is a platform service" if service else ("cannot run now" if why else "has no worker")
        how = f"chat_send(channel={answer_ch['id']}" + (f", reply_to={reply_to})" if reply_to else ")")
        task = tasks.create(conn, ctx, {
            "title": f"Chat: answer {author['name']} (za {name})",
            "assignee": {"type": "agent", "id": pm["id"]}, "status": "next", "priority": 1, "topic": "chat",
            "notes": f"Purpose: {author['name']} wrote to {name}, which {kind} and cannot answer. The platform "
                     f"told them you will answer instead.\nSource: chat channel {ch['id']}, message {message_id}."
                     f"\n\n{said}\n\nAnswer with {how}, in Czech unless they wrote otherwise; say it is about "
                     f"their message to {name}. Do what they ask or delegate it to the agent whose job it is "
                     "(create_task, handoff_task), and say in the answer who does it.",
            "definition_of_done": "The answer is in the chat.",
            "reviewer": pm["id"],
        })
        audit.log(conn, ctx, "chat_task", "task", task["id"], channel=answer_ch["id"], message=reply_to)
        text = (f"Automatická odpověď platformy: {name} {what}, takže sám neodpoví. Tvou zprávu jsem předal "
                f"dál: {pm['name']} ({task['ref']}), odpoví ti {where}.")
    else:
        text = (f"Automatická odpověď platformy: {name} {what}, takže sám neodpoví, a vedení ({roles.CEO}) teď "
                "není k dispozici. Napiš prosím přímo agentovi, který to má na starosti.")
    out = None
    try:
        chat._add_member(conn, ch["id"], target["id"])
        out = chat.send(conn, Ctx(target["id"], via="system"), ch["id"], text, reply_to=message_id, system=True)
    except Exception:  # noqa: BLE001 - the PM's task stays either way
        pass
    audit.log(conn, ctx, "chat_forwarded", "chat_message", message_id, target=target["id"],
              task=task["id"] if task else None, at=now_iso())
    if task:
        autoreply(conn, task["id"])  # the PM cannot run either: the owner hears why
    conn.commit()
    return out


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
