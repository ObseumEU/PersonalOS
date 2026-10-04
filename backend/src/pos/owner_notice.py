"""Never silent about the owner's work: when an agent's task that came from the
owner ends badly (failed, handed back, blocked, step cap, cancelled), the one who
can act on it hears it at once, in Czech, signed "PersonalOS" (pos.notices): the
agent's lead, or the SRE (else the CTO) when the platform itself failed. The
owner does not get the platform's failure notices (prod 2026-09/10: they reached
him under his or the CEO's name); what truly needs him reaches him through the
CEO, as one clear ask. A task he created himself also gets the notice as a
system comment in its activity.

"Came from the owner": a "Chat: answer" task for his message, or a task whose
notes cite his chat message ("zpráva 356", "msg 356", "message 356") or a chat
task of his ("T-170"), or a task he created himself. The platform posts it
(system message, no rate or budget check), once per task and outcome within
DEDUP_HOURS. Any error here is swallowed: the hand-back itself must not fail.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, chat
from .core import now_iso

OUTCOMES = {
    "failed": "skončil chybou",
    "handed_back": "úkol vrátil",
    "blocked": "je zablokovaný",
    "capped": "narazil na limit kroků",
    "cancelled": "běh byl zastaven",
}
DEDUP_HOURS = 6
_MSG_REF = re.compile(r"(?:zpráv[aěuy]|zprava|msg|message)\s*#?\s*(\d+)", re.I)
_TASK_REF = re.compile(r"\bT-(\d+)\b")


def _owner_message(conn: sqlite3.Connection, message_id: int) -> tuple[int, int] | None:
    row = conn.execute("""SELECT m.id, m.channel_id FROM chat_messages m JOIN actors a ON a.id = m.author_id
                          WHERE m.id = ? AND a.is_owner = 1""", (message_id,)).fetchone()
    return (row["channel_id"], row["id"]) if row else None


def origin(conn: sqlite3.Connection, task_id: int, _depth: int = 0) -> dict | None:
    """Where the owner asked for this task: {"channel", "message"} (his chat
    message), {"task"} (he created it himself), or None (not his)."""
    t = conn.execute("SELECT id, notes, created_by, parent_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is None:
        return None
    direct = chat.chat_origin(conn, task_id)
    if direct and _owner_message(conn, direct[1]):
        return {"channel": direct[0], "message": direct[1]}
    notes = t["notes"] or ""
    for m in _MSG_REF.finditer(notes):
        hit = _owner_message(conn, int(m.group(1)))
        if hit:
            return {"channel": hit[0], "message": hit[1]}
    if _depth < 2:
        refs = [int(x) for x in _TASK_REF.findall(notes)] + ([t["parent_id"]] if t["parent_id"] else [])
        for ref in dict.fromkeys(refs):
            if ref != task_id:
                o = origin(conn, ref, _depth + 1)
                if o and "channel" in o:
                    return o
    owner = actors.owner_id(conn)
    if t["created_by"] == owner:
        return {"task": task_id}
    return None


def _czech_reason(why: str) -> str:
    why = (why or "").strip()
    m = re.match(r"step limit reached \((\d+) steps\); last note: (.*)", why, re.S)
    if m:
        return f"došel mu limit {m.group(1)} kroků na jeden běh (poslední poznámka: „{m.group(2).strip()[:300]}“)"
    if why.startswith("worker error"):
        return f"chyba při běhu ({why[:300]})"
    return why[:400] or "bez udaného důvodu"


def _recent(conn: sqlite3.Connection, task_id: int, outcome: str) -> bool:
    since = (datetime.now(timezone.utc) - timedelta(hours=DEDUP_HOURS)).isoformat(timespec="seconds")
    return conn.execute("""SELECT 1 FROM audit_log WHERE action = 'owner_failure_notice' AND entity = 'task'
                           AND entity_id = ? AND json_extract(detail, '$.outcome') = ? AND at >= ?""",
                        (task_id, outcome, since)).fetchone() is not None


def body_for(conn: sqlite3.Connection, task_id: int, agent_id: int, outcome: str, why: str,
             done: str = "", origin_: dict | None = None) -> str:
    """The notice for the one who acts on it (the lead, or the SRE for a platform fault), in Czech."""
    t = conn.execute("SELECT title, assignee_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    agent = actors.get(conn, agent_id)["name"]
    from . import notices
    from .tasks import display_id

    ref = display_id(task_id)
    src = ""
    if origin_ and "message" in origin_:
        src = f" Zadal ho Owner v chatu ([jeho zpráva](/chat?c={origin_['channel']}&m={origin_['message']}))."
    elif origin_:
        src = " Zadal ho Owner."
    lines = [f"{agent} {OUTCOMES.get(outcome, outcome)} u {ref} „{t['title']}“, úkol není hotový.{src}"]
    done = (done or "").strip()
    if done:
        lines.append(f"Co je hotovo: {done[:500]}")
    lines.append(f"Co selhalo a proč: {_czech_reason(why)}.")
    owner = actors.owner_id(conn)
    if notices.is_platform_fault(why):
        nxt = (f"chyba je na straně platformy (běh, klíč, engine): oprav ji a {ref} vrať agentovi {agent} "
               "(task_reassign / komentář „pokračuj“).")
    elif outcome == "capped":
        nxt = f"rozděl {ref} na menší kroky, nebo ho vrať agentovi {agent} s komentářem „pokračuj tam, kde jsi skončil“."
    elif t["assignee_id"] == owner:
        nxt = f"{ref} skončil u Ownera: převezmi ho, nebo ho předej členovi týmu; Ownerovi patří jen to, co bez něj nejde."
    elif outcome == "cancelled":
        nxt = f"{ref} zůstává agentovi {agent}; zkontroluj, proč běh zastavil (pauza, limit, stop), a uvolni ho."
    elif outcome == "blocked":
        nxt = f"uvolni blokaci {ref} (přístup, schválení, podklady), nebo ho předej jinému agentovi."
    else:
        nxt = f"upřesni {ref} komentářem a vrať ho agentovi {agent}, nebo ho předej jinému agentovi."
    lines.append(f"Co dál (pro tebe): {nxt}")
    if origin_:
        lines.append("Owner tohle hlášení nedostal. Pokud bez něj nejde pokračovat, řekne mu to CEO jednou jasnou "
                     "otázkou (ask_owner s možnostmi a doporučením).")
    return "\n".join(lines)


def notify(conn: sqlite3.Connection, task_id: int | None, agent_id: int, outcome: str, why: str,
           done: str = "") -> dict | None:
    """Tell the one who can act (see the module doc). Returns what was posted, or None.
    The caller commits."""
    if not task_id:
        return None
    try:
        from . import notices

        o = origin(conn, task_id)
        if o is None or _recent(conn, task_id, outcome):
            return None
        body = body_for(conn, task_id, agent_id, outcome, why, done, o)
        sys_ctx = notices.system_ctx(conn)
        to = notices.recipient(conn, agent_id, why)
        out = None
        where: dict = {}
        if to:
            out = notices.dm(conn, to, body, attachments=[{"type": "task", "id": task_id}])
            where = {"to": to, "message": (out or {}).get("id")}
        if "task" in o or out is None:
            from . import comments

            c = comments.add(conn, sys_ctx, task_id, body, kind="system", notify=False)
            out = out or c
            where["comment"] = c.get("id")
        audit.log(conn, sys_ctx, "owner_failure_notice", "task", task_id, outcome=outcome, at=now_iso(),
                  agent_id=agent_id, **where)
        return out
    except Exception:  # noqa: BLE001 - the notice must never break the hand-back or the run's finish
        return None
