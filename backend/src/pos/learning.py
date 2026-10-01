"""The learning loop: what went wrong once becomes a lesson the agent keeps.

Measured on 2026-10-01: `feedback` was empty and only 17 of 30 agents had a memory note, so a
returned result or an owner's correction was forgotten by the next run. Now three events write a
lesson:

- a **returned review** (pos.tasks.review with a comment);
- an **owner correction**: the owner writes to an agent (a DM, or a comment on its task) right after
  its result and the text corrects it ("ne, …", "špatně", "místo toho …", "wrong", …);
- a **failed run with a clear lesson** (tool calls written as text, the step cap, the run's budget,
  a timeout, a missing permission).

Each writes a `feedback` row (critique, from the reviewer / the owner / the platform; the same lesson
is not recorded twice in 24 h) and a line in the agent's memory note under "## Poučení"
("- Poučení: …", deduplicated, at most MAX_LESSONS, the oldest dropped first so the note stays
under its cap). The memory is put into every run's prompt (pos.api_worker /memory, the worker's
memory_section), so the next run already knows.

The Performance Coach gets a weekly task (coach_weekly) with the week's lessons by agent; a lesson
that repeats becomes an instruction change (propose_instructions). Every active agent has a memory
note (ensure_memories: a starter note), so the lessons and the facts have a place.

Prod: `python -m pos.learning ensure-memories [--apply]`.
"""

import argparse
import difflib
import json
import re
from pathlib import Path
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit, roles, versioning
from .core import Ctx, now_iso

SECTION = "## Poučení"
PREFIX = "- Poučení: "
MAX_LESSONS = 12
MAX_LESSON_CHARS = 300
SIMILAR = 0.82
FEEDBACK_DEDUP_HOURS = 24
CORRECTION_HOURS = 48

STARTER = ("# Paměť: {name}\n\n"
           "Fakta, která příští běh nemusí znovu zjišťovat (hostitelé, cesty, verze, rozhodnutí, známé "
           "problémy). Udržuj je stručné (memory_update, celý text).\n\n"
           "## Fakta\n- (zatím nic)\n\n" + SECTION + "\n")

CORRECTION_RE = re.compile(
    r"(?:^|[\s,.!])(?:ne[,.!]|špatně|spatne|chybně|chybne|to není|to neni|tohle není|nemělo|nemelo|neměl[ai]?|"
    r"nemel[ai]?|proč jsi|proc jsi|oprav(?:it|te|)\b|místo toho|misto toho|nedělej|nedelej|nechci|"
    r"wrong|incorrect|not what i|instead|don't|do not|that's not|thats not)", re.IGNORECASE)

FAILURE_LESSONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"tool calls? (?:written )?as text|pseudo|<function_calls>|fake tool", re.IGNORECASE),
     "Nástroje volej skutečně; volání napsané jako text se nikdy neprovede a výsledek je prázdný."),
    (re.compile(r"max(?:imum)?[ _-]?steps|step cap|too many steps|turn limit|max[ _-]?turns", re.IGNORECASE),
     "Úkol narazil na limit kroků: rozděl ho na kroky (create_task s parent_id) a průběh si zapisuj "
     "(report_progress, paměť), ať další běh navazuje."),
    (re.compile(r"budget|cost cap|max_budget|usd", re.IGNORECASE),
     "Běh přečerpal rozpočet běhu: méně průzkumu naslepo, cílené dotazy (knowledge, search), velké úkoly rozděl."),
    (re.compile(r"timed? ?out|timeout", re.IGNORECASE),
     "Běh vypršel: dlouhé příkazy spouštěj s timeoutem a kontroluj průběžně; neblokuj se na čekání."),
    (re.compile(r"permission|not granted|forbidden|lacks |not allowed|unauthori[sz]ed", re.IGNORECASE),
     "Chyběl nástroj nebo oprávnění: na začátku úkolu ověř my_access a o chybějící požádej hned (request_access)."),
]


# ------------------------------------------------------------------ memory

def _norm(text: str) -> str:
    return re.sub(r"[^\w ]+", "", re.sub(r"\s+", " ", (text or "").lower())).strip()[:160]


def _is_agent(conn: sqlite3.Connection, actor_id: int | None) -> bool:
    if not actor_id:
        return False
    r = conn.execute("SELECT kind, archived_at FROM actors WHERE id = ?", (actor_id,)).fetchone()
    return r is not None and r["kind"] != "human" and not r["archived_at"]


def lessons_of(body: str) -> list[str]:
    return [ln[len(PREFIX):].strip() for ln in (body or "").splitlines() if ln.startswith(PREFIX)]


def with_lesson(body: str, lesson: str, max_chars: int) -> str | None:
    """The memory text with the lesson added under "## Poučení" (None: already there, or no room)."""
    lesson = re.sub(r"\s+", " ", lesson).strip()[:MAX_LESSON_CHARS]
    key = _norm(lesson)
    for old in lessons_of(body):
        if difflib.SequenceMatcher(None, _norm(old.rsplit(" (", 1)[0]), key).ratio() >= SIMILAR:
            return None
    stamp = datetime.now(timezone.utc).date().isoformat()
    line = f"{PREFIX}{lesson} ({stamp})"
    lines = (body or "").rstrip().splitlines()
    if SECTION not in lines:
        lines += ["", SECTION]
    at = lines.index(SECTION)
    end = at + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1
    section = [ln for ln in lines[at + 1:end] if ln.strip()]
    section.append(line)
    while True:
        kept = [ln for ln in section if ln.startswith(PREFIX)]
        if len(kept) > MAX_LESSONS:
            section.remove(kept[0])  # the oldest lesson goes first
            continue
        text = "\n".join([*lines[:at + 1], *section, *([""] if end < len(lines) else []), *lines[end:]]).strip() + "\n"
        if len(text) <= max_chars:
            return text
        if len(kept) <= 1:
            return None  # the facts alone fill the note: the feedback row still carries the lesson
        section.remove(kept[0])


def remember(conn: sqlite3.Connection, agent_id: int, lesson: str) -> bool:
    """Add a lesson to the agent's memory note (deduplicated, capped). True when it was added."""
    from . import agent_memory

    if not _is_agent(conn, agent_id):
        return False
    body = agent_memory.text(conn, agent_id)
    if not body.strip():
        body = STARTER.format(name=actors.get(conn, agent_id)["name"])
    new = with_lesson(body, lesson, agent_memory.MAX_CHARS)
    if new is None:
        return False
    agent_memory.set_body(conn, _system(conn), new, agent_id=agent_id, system=True)
    return True


def ensure_memory(conn: sqlite3.Connection, actor_id: int) -> bool:
    """This agent's memory note exists (a starter note where it had none). True when created."""
    from . import agent_memory

    if not _is_agent(conn, actor_id) or agent_memory.text(conn, actor_id).strip():
        return False
    agent_memory.set_body(conn, _system(conn), STARTER.format(name=actors.get(conn, actor_id)["name"]),
                          agent_id=actor_id, system=True)
    return True


def ensure_memories(conn: sqlite3.Connection, apply: bool = True) -> dict:
    """Every active agent has a memory note (a starter note where it had none)."""
    from . import agent_memory

    missing = [r for r in conn.execute("SELECT id, name FROM actors WHERE kind != 'human' AND archived_at IS NULL "
                                       "ORDER BY id").fetchall()
               if not agent_memory.text(conn, r["id"]).strip()]
    if apply:
        for r in missing:
            agent_memory.set_body(conn, _system(conn), STARTER.format(name=r["name"]), agent_id=r["id"], system=True)
    return {"created" if apply else "would_create": [r["name"] for r in missing]}


# ------------------------------------------------------------------ lessons

def _system(conn: sqlite3.Connection) -> Ctx:
    return Ctx(actors.assistant_id(conn), via="system")


def record(conn: sqlite3.Connection, agent_id: int | None, lesson: str, *, source: str,
           from_id: int | None = None, task_id: int | None = None) -> dict | None:
    """One lesson: a feedback row (critique) and a line in the agent's memory. The caller commits."""
    lesson = (lesson or "").strip()
    if not lesson or not _is_agent(conn, agent_id):
        return None
    from_id = from_id or actors.assistant_id(conn)
    body = f"Poučení ({source}): {lesson}"[:4000]
    since = (datetime.now(timezone.utc) - timedelta(hours=FEEDBACK_DEDUP_HOURS)).isoformat(timespec="seconds")
    dup = conn.execute("SELECT id FROM feedback WHERE to_id = ? AND body = ? AND created_at >= ?",
                       (agent_id, body, since)).fetchone()
    fb_id = dup["id"] if dup else None
    if fb_id is None:
        ctx = Ctx(from_id, via="system")
        now = now_iso()
        from . import feedback  # noqa: F401 - registers the versioned entity

        fb_id = versioning.insert(conn, ctx, "feedback", {
            "from_id": from_id, "to_id": agent_id, "task_id": task_id, "kind": "critique", "body": body,
            "rating": None, "status": "open", "created_by": from_id, "created_at": now, "updated_at": now})["id"]
    added = remember(conn, agent_id, lesson)
    audit.log(conn, _system(conn), "lesson", "actor", agent_id, source=source, task_id=task_id,
              feedback_id=fb_id, memory=added)
    return {"feedback_id": fb_id, "memory": added}


def on_return(conn: sqlite3.Connection, ctx: Ctx, row, comment: str | None) -> dict | None:
    """A reviewer returned a result: the reviewer's comment is the lesson."""
    if not (comment or "").strip() or ctx.actor_id == row["assignee_id"]:
        return None
    from .tasks import display_id

    who = actors.get(conn, ctx.actor_id)["name"]
    return record(conn, row["assignee_id"], f"{display_id(row['id'])} „{row['title'][:80]}“ vrátil {who}: "
                  f"{comment.strip()}", source="vrácená revize", from_id=ctx.actor_id, task_id=row["id"])


def is_correction(text: str | None) -> bool:
    return bool(CORRECTION_RE.search(text or ""))


def _recent_result(conn: sqlite3.Connection, agent_id: int, task_ids: list[int] | None = None) -> sqlite3.Row | None:
    since = (datetime.now(timezone.utc) - timedelta(hours=CORRECTION_HOURS)).isoformat(timespec="seconds")
    if task_ids:
        q = ",".join("?" * len(task_ids))
        return conn.execute(f"SELECT * FROM tasks WHERE id IN ({q}) AND assignee_id = ? ORDER BY updated_at DESC "
                            "LIMIT 1", (*task_ids, agent_id)).fetchone()
    return conn.execute("""SELECT * FROM tasks WHERE assignee_id = ? AND status IN ('review', 'done')
                           AND updated_at >= ? AND archived_at IS NULL AND COALESCE(topic, '') != 'chat'
                           ORDER BY updated_at DESC LIMIT 1""", (agent_id, since)).fetchone()


def on_owner_message(conn: sqlite3.Connection, ctx: Ctx, agent_ids: list[int], body: str,
                     task_ids: list[int] | None = None) -> list[dict]:
    """The owner wrote to agents: a correction of a recent result of theirs becomes a lesson."""
    if not is_correction(body) or not actors.get(conn, ctx.actor_id)["is_owner"]:
        return []
    from .tasks import display_id

    out = []
    for aid in agent_ids:
        if not _is_agent(conn, aid):
            continue
        t = _recent_result(conn, aid, task_ids)
        if t is None:
            continue
        r = record(conn, aid, f"Majitel opravil {display_id(t['id'])} „{t['title'][:80]}“: {body.strip()[:400]}",
                   source="oprava od majitele", from_id=ctx.actor_id, task_id=t["id"])
        if r:
            out.append(r)
    return out


def on_owner_comment(conn: sqlite3.Connection, ctx: Ctx, task, body: str) -> dict | None:
    """The owner commented on an agent's handed-in or finished task, correcting it."""
    if task["status"] not in ("review", "done") or not _is_agent(conn, task["assignee_id"]):
        return None
    r = on_owner_message(conn, ctx, [task["assignee_id"]], body, [task["id"]])
    return r[0] if r else None


def lesson_for_failure(detail: str | None) -> str | None:
    for pattern, lesson in FAILURE_LESSONS:
        if pattern.search(detail or ""):
            return lesson
    return None


def on_failed_run(conn: sqlite3.Connection, run) -> dict | None:
    """A run that failed for a reason with a clear lesson."""
    if run["status"] != "error":
        return None
    lesson = lesson_for_failure(run["detail"])
    if lesson is None:
        return None
    return record(conn, run["actor_id"], lesson, source="neúspěšný běh", task_id=run["task_id"])


# ------------------------------------------------------------------ the Performance Coach's week

def repeated(bodies: list[str]) -> list[tuple[str, int]]:
    """Lessons that repeat (similar text at least twice): (the first of them, how many)."""
    groups: list[tuple[str, str, int]] = []  # (core, first body, count)
    for b in bodies:
        core = re.sub(r"^t\d+ ", "", _norm(re.sub(r"^Poučení \([^)]*\):\s*", "", b)))
        for i, (c, first, n) in enumerate(groups):
            if difflib.SequenceMatcher(None, c, core).ratio() >= 0.6:
                groups[i] = (c, first, n + 1)
                break
        else:
            groups.append((core, b, 1))
    return [(first, n) for _, first, n in groups if n >= 2]


def coach_weekly(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Monday: one task for the Performance Coach with the week's lessons and feedback by agent;
    lessons that repeat are marked: those become instruction changes."""
    from . import tasks

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=7)).isoformat(timespec="seconds")
    rows = conn.execute("""SELECT f.*, a.name AS to_name FROM feedback f JOIN actors a ON a.id = f.to_id
                           WHERE f.created_at >= ? AND f.archived_at IS NULL AND a.kind != 'human'
                           ORDER BY a.name, f.id""", (since,)).fetchall()
    coach = actors.find_by_name(conn, roles.COACH)
    if coach is None or coach["archived_at"] or not rows:
        return {"feedback": len(rows)}
    week = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
    title = f"Týdenní revize poučení agentů · {week}"
    if conn.execute("SELECT 1 FROM tasks WHERE title = ? AND archived_at IS NULL", (title,)).fetchone():
        return {"feedback": len(rows), "skipped": "already this week"}
    by_agent: dict[str, list] = {}
    for r in rows:
        by_agent.setdefault(r["to_name"], []).append(r)
    parts, repeats = [], 0
    for name, items in by_agent.items():
        rep = repeated([r["body"] for r in items])
        repeats += len(rep)
        lines = [f"- #{r['id']} {r['kind']} [{r['status']}]: {r['body'][:240]}" for r in items[:12]]
        lines += [f"- **Opakuje se {n}×:** {b[:200]}" for b, n in rep]
        parts.append(f"### {name} ({len(items)})\n" + "\n".join(lines))
    t = tasks.create(conn, Ctx(actors.owner_id(conn), via="scheduler"), {
        "title": title, "assignee": {"type": "agent", "id": coach["id"]}, "status": "next", "priority": 2,
        "topic": "agents", "source": "scheduler",
        "notes": ("### Proč\nPoučení z vrácených revizí, oprav od majitele a neúspěšných běhů za poslední týden "
                  "(pos.learning). Co se opakuje, patří do instrukcí, ne jen do paměti agenta.\n\n"
                  "### Co udělat\nU každého opakovaného poučení navrhni změnu instrukcí (`propose_instructions`) a "
                  "zpětnou vazbu vyřeš (`feedback_resolve` applied s odkazem). Jednorázové nech v paměti agenta, "
                  "nebo je zamítni s důvodem.\n\n" + "\n\n".join(parts)),
        "definition_of_done": "Každé opakované poučení má návrh instrukcí nebo zdůvodněné zamítnutí; "
                              "vyřešená zpětná vazba je označená.",
        "reviewer": coach["id"]})
    conn.commit()
    return {"feedback": len(rows), "agents": len(by_agent), "repeated": repeats, "task": t["ref"]}


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(description="The learning loop's maintenance.")
    p.add_argument("command", choices=["ensure-memories"])
    p.add_argument("--apply", action="store_true", help="change the data (default: a dry run)")
    p.add_argument("--db", default=None)
    a = p.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        out = ensure_memories(conn, apply=a.apply)
        if a.apply:
            conn.commit()
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
