"""The closed self-improvement loop: signals → one weekly triage → fix tasks with targets → verified.

1. **Daily (05:40, job `improve_daily`, code only):** the web smoke (pos.improve.smoke), the signal
   digest (pos.improve.signals), registering the targets written on fix tasks, and verifying the
   ones whose 7 days after deploy are over.
2. **Monday 08:30 (job `improve_triage`, one LLM run):** ONE task for the CTO with the week's top
   signals (impact × trend), the open items of "PersonalOS zlepšení" and last week's verdicts. The
   CTO's instructions ("Self-improvement triage"): at most 5 improvements, deduplicated against the
   open items, fix tasks for the Software Engineer or instruction tasks for the Performance Coach,
   each with evidence and a target block tied to a signal key:

       ### Cíl
       - signal: `tool_error:update_task.follow_up`
       - baseline: 6
       - target: 0

   Tasks have no metric columns, so the block in the notes *is* the stored target; the daily job
   copies it into `improve_targets` (task, signal key, baseline, target, verdict).
3. **Verification (daily, code):** a target's task is done and deployed (a deploy tied to the task, or
   the first successful deploy from a day before its completion on; work that needs no deploy counts
   from its completion, after 7 days without a deploy too); 7 days later the digest's 7-day count of
   its signal is compared with the baseline:
   - **improved** (at or under the target, or ≥ 25 % under the baseline): verified, a comment;
   - **not improved**: the task goes back to the CTO with the numbers (at most twice, then it stays
     "not improved" with a comment);
   - **worse** (≥ 25 % and ≥ 2 over the baseline): an urgent task for the Software Engineer to revert
     or fix, with the same target block (so it is verified too).
4. **Monday 08:40 (job `improve_coach`, one LLM run):** the 3 agents with the worst success rate or the
   most tool errors go to the Performance Coach in ONE task, with their failing runs and refused tool
   calls; it proposes instruction patches through `propose_instructions` (the existing review path).
5. **The owner:** the weekly packet carries `self_improved` and the published report a section
   "Samo se zlepšilo tento týden": verified improvements with before → after, at most 3 lines. No new
   approvals.

Cost: two agent runs a week (CTO ~$0.30, Performance Coach ~$0.35 on prod averages) plus a CTO run per
reopened target; the rest is code. Both tasks are self-reviewed (no review queue), skip a week when
the previous one is still open, and go through the normal budget gates (the company cap, the kill
switch: a frozen company runs no job). Nothing here touches the constitution, the guard, the company
cap or the kill switch.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from .. import actors, audit
from ..core import TZ, Ctx, now_iso
from . import signals

log = logging.getLogger(__name__)

VERIFY_DAYS = 7
NO_DEPLOY_AFTER_DAYS = 7  # done work without a deploy this long: it needed none, measured from completion
MAX_REOPEN = 2
MAX_IMPROVEMENTS = 5
TOP_SIGNALS = 12
COACH_AGENTS = 3
COACH_MIN_RUNS = 3
OWNER_LINES = 3
NO_DEPLOY_ROLES = ("coach", "hr")  # instruction work: in effect when done
TRIAGE_SOURCE = "improve_triage"
COACH_SOURCE = "improve_coach"
FOLLOWUP_SOURCE = "improve_verify"

_SCHEMA = """CREATE TABLE IF NOT EXISTS improve_targets (
    task_id          INTEGER PRIMARY KEY,
    signal_key       TEXT NOT NULL,
    baseline         REAL NOT NULL,
    target           REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'open',
    registered_at    TEXT NOT NULL,
    done_at          TEXT,
    deployed_at      TEXT,
    measured_day     TEXT,
    measured_value   REAL,
    reopened         INTEGER NOT NULL DEFAULT 0,
    reopened_at      TEXT,
    followup_task_id INTEGER,
    updated_at       TEXT NOT NULL
)"""
STATUSES = ("open", "measuring", "reopened", "verified", "not_improved", "worse", "closed")
ACTIVE = ("open", "measuring", "reopened")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    signals.ensure_schema(conn)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _week(now: datetime) -> str:
    y, w, _ = now.astimezone(TZ).date().isocalendar()
    return f"{y}-W{w:02d}"


def _num(v) -> str:
    return signals._num(v)


def _owner_ctx(conn: sqlite3.Connection) -> Ctx:
    return Ctx(actors.owner_id(conn), via="scheduler")


def _agent(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    r = actors.find_by_name(conn, name)
    return r if r is not None and not r["archived_at"] else None


# ------------------------------------------------------------------ the target block on a task

_SIGNAL = re.compile(r"^\s*[-*]?\s*(?:signal|sign[aá]l)\s*:\s*`?([a-z_]+:[^\s`]+)`?", re.I | re.M)
_BASELINE = re.compile(r"^\s*[-*]?\s*(?:baseline|v[yý]choz[ií](?:\s+stav)?)\s*:\s*([0-9]+(?:[.,][0-9]+)?)", re.I | re.M)
_TARGET = re.compile(r"^\s*[-*]?\s*(?:target|c[ií]l)\s*:\s*([0-9]+(?:[.,][0-9]+)?)", re.I | re.M)


def target_block(key: str, baseline: float, target: float) -> str:
    return f"### Cíl\n- signal: `{key}`\n- baseline: {_num(baseline)}\n- target: {_num(target)}"


def parse_target(notes: str) -> dict | None:
    """{signal, baseline, target} from a task's notes (baseline/target None when not written)."""
    m = _SIGNAL.search(notes or "")
    if not m:
        return None
    num = lambda rx: (lambda x: float(x[1].replace(",", ".")) if x else None)(rx.search(notes))  # noqa: E731
    return {"signal": m[1].rstrip(".,;"), "baseline": num(_BASELINE), "target": num(_TARGET)}


def register(conn: sqlite3.Connection, now: datetime | None = None) -> list[int]:
    """Tasks with a target block that are not tracked yet become targets. The baseline defaults to the
    signal's 7-day count in the newest digest; the target to half of it."""
    ensure_schema(conn)
    now = _now(now)
    day = signals.latest_day(conn)
    made = []
    for t in conn.execute("""SELECT id, notes FROM tasks WHERE archived_at IS NULL AND parent_id IS NULL
                             AND (notes LIKE '%signal:%' OR notes LIKE '%signál:%' OR notes LIKE '%Signal:%'
                                  OR notes LIKE '%Signál:%')
                             AND id NOT IN (SELECT task_id FROM improve_targets)""").fetchall():
        p = parse_target(t["notes"])
        if p is None:
            continue
        base = p["baseline"]
        if base is None:
            base = (signals.value(conn, p["signal"], day) if day else None) or 0.0
        target = p["target"] if p["target"] is not None else float(int(base // 2))
        conn.execute("""INSERT INTO improve_targets (task_id, signal_key, baseline, target, status, registered_at,
                        updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?)""",
                     (t["id"], p["signal"], base, target, _iso(now), _iso(now)))
        made.append(t["id"])
    return made


def targets(conn: sqlite3.Connection, statuses: tuple[str, ...] | None = None) -> list[dict]:
    ensure_schema(conn)
    sql = """SELECT g.*, t.title, t.status AS task_status, t.assignee_name FROM improve_targets g
             JOIN tasks t ON t.id = g.task_id"""
    args: tuple = ()
    if statuses:
        sql += f" WHERE g.status IN ({','.join('?' * len(statuses))})"
        args = statuses
    return [dict(r) for r in conn.execute(sql + " ORDER BY g.task_id", args)]


# ------------------------------------------------------------------ verification

def deployed_at(conn: sqlite3.Connection, task: sqlite3.Row, now: datetime) -> str | None:
    """When the done task's change went live: its own deploy, else the first successful deploy from a day
    before its completion on; instruction work (and work with no deploy for 7 days) at its completion."""
    if task["status"] != "done" or not task["completed_at"]:
        return None
    done = task["completed_at"]
    role = None
    if task["assignee_id"]:
        r = conn.execute("SELECT role FROM actors WHERE id = ?", (task["assignee_id"],)).fetchone()
        role = r["role"] if r else None
    if role in NO_DEPLOY_ROLES:
        return done
    if signals._has(conn, "deploys"):
        own = conn.execute("SELECT MIN(created_at) FROM deploys WHERE task_id = ? AND status = 'ok'",
                           (task["id"],)).fetchone()[0]
        if own:
            return own
        since = _iso(datetime.fromisoformat(done) - timedelta(days=1))
        first = conn.execute("SELECT MIN(created_at) FROM deploys WHERE status = 'ok' AND created_at >= ?",
                             (since,)).fetchone()[0]
        if first:  # deployed shortly before it was handed in and accepted: live from its completion
            return max(first, done)
    if now - datetime.fromisoformat(done) >= timedelta(days=NO_DEPLOY_AFTER_DAYS):
        return done
    return None


def outcome(baseline: float, target: float, value: float) -> str:
    """'improved' | 'not_improved' | 'worse' for a signal's count after the fix."""
    if value <= target or (baseline > 0 and value <= baseline * 0.75):
        return "improved"
    if value > baseline * 1.25 and value >= baseline + 2:
        return "worse"
    return "not_improved"


def _set(conn: sqlite3.Connection, task_id: int, **fields) -> None:
    fields["updated_at"] = now_iso()
    conn.execute(f"UPDATE improve_targets SET {', '.join(f'{k} = ?' for k in fields)} WHERE task_id = ?",
                 (*fields.values(), task_id))


def _comment(conn: sqlite3.Connection, task_id: int, body: str) -> None:
    from .. import comments, notices

    try:
        comments.add(conn, notices.system_ctx(conn), task_id, body, notify=False)
    except Exception:  # noqa: BLE001 - the verdict stands in improve_targets and the audit log
        log.exception("could not comment on T-%s", task_id)


def _numbers(g: dict, value: float) -> str:
    return (f"signál `{g['signal_key']}`: před opravou {_num(g['baseline'])}/7 d → teď {_num(value)}/7 d "
            f"(cíl {_num(g['target'])})")


def verify(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Targets whose 7 days after deploy are over: compared with the baseline and acted on."""
    from .. import tasks

    ensure_schema(conn)
    now = _now(now)
    today = signals.day_of(now)
    out = {"verified": [], "reopened": [], "worse": [], "not_improved": [], "measuring": 0}
    for g in targets(conn, ACTIVE):
        t = conn.execute("SELECT * FROM tasks WHERE id = ?", (g["task_id"],)).fetchone()
        if t is None or t["archived_at"]:
            _set(conn, g["task_id"], status="closed")
            continue
        if g["status"] == "reopened":
            if t["status"] != "done" or (t["completed_at"] or "") <= (g["reopened_at"] or ""):
                continue  # still with the CTO
        dep = deployed_at(conn, t, now)
        if dep is None:
            if g["status"] != "reopened":
                _set(conn, g["task_id"], status="open", done_at=t["completed_at"])
            continue
        measure_on = (datetime.fromisoformat(dep).astimezone(TZ).date() + timedelta(days=VERIFY_DAYS)).isoformat()
        if today < measure_on:
            _set(conn, g["task_id"], status="measuring", done_at=t["completed_at"], deployed_at=dep)
            out["measuring"] += 1
            continue
        v = signals.value(conn, g["signal_key"], today)
        if v is None:
            continue  # no digest today: tomorrow
        verdict = outcome(g["baseline"], g["target"], v)
        ref = tasks.display_id(g["task_id"])
        nums = _numbers(g, v)
        ctx = _owner_ctx(conn)
        if verdict == "improved":
            _set(conn, g["task_id"], status="verified", done_at=t["completed_at"], deployed_at=dep,
                 measured_day=today, measured_value=v)
            _comment(conn, g["task_id"], f"✅ Ověřeno 7 dní po nasazení: {nums}.")
            out["verified"].append(ref)
        elif verdict == "worse":
            fid = _worse_task(conn, g, t, v)
            _set(conn, g["task_id"], status="worse", done_at=t["completed_at"], deployed_at=dep, measured_day=today,
                 measured_value=v, followup_task_id=fid)
            _comment(conn, g["task_id"], f"⚠️ Po nasazení je to horší: {nums}. Urgentní úkol pro Software Engineera: "
                                         f"{tasks.display_id(fid) if fid else '—'}.")
            out["worse"].append(ref)
        elif g["reopened"] < MAX_REOPEN and _reopen(conn, g, t, v):
            _set(conn, g["task_id"], status="reopened", reopened=g["reopened"] + 1, reopened_at=_iso(now),
                 done_at=t["completed_at"], deployed_at=dep, measured_day=today, measured_value=v)
            out["reopened"].append(ref)
        else:
            _set(conn, g["task_id"], status="not_improved", done_at=t["completed_at"], deployed_at=dep,
                 measured_day=today, measured_value=v)
            _comment(conn, g["task_id"], f"Bez zlepšení ani po {g['reopened']}× vrácení: {nums}. Uzavřeno jako "
                                         "nezlepšené; signál zůstává v týdenní triáži.")
            out["not_improved"].append(ref)
        audit.log(conn, ctx, "improve_verify", "task", g["task_id"], signal=g["signal_key"], baseline=g["baseline"],
                  target=g["target"], value=v, verdict=verdict)
    conn.commit()
    return {k: v for k, v in out.items() if v}


def _reopen(conn: sqlite3.Connection, g: dict, t: sqlite3.Row, value: float) -> bool:
    """The task goes back to the CTO with the numbers."""
    from .. import tasks, wake

    cto = _agent(conn, "CTO")
    if cto is None:
        return False
    try:
        tasks.update(conn, _owner_ctx(conn), t["id"], {"status": "next", "assignee": {"type": "agent", "id": cto["id"]},
                                                       "priority": 2})
    except Exception:  # noqa: BLE001 - a task that cannot be reopened: the verdict is recorded as not improved
        log.exception("could not reopen T-%s", t["id"])
        return False
    _comment(conn, t["id"], f"↩️ Vráceno CTO: oprava je nasazená 7 dní, ale {_numbers(g, value)}. Rozhodni: další "
                            "oprava (úkol pro Software Engineera s blokem `### Cíl`), jiný přístup, nebo uzavři s "
                            "vysvětlením proč to nejde. Po dokončení se měří znovu.")
    wake.wake(cto["id"])
    return True


def _worse_task(conn: sqlite3.Connection, g: dict, t: sqlite3.Row, value: float) -> int | None:
    from .. import platform_loop, tasks, wake

    se = _agent(conn, "Software Engineer")
    if se is None:
        return None
    where = platform_loop.ensure(conn)
    ref = tasks.display_id(t["id"])
    made = tasks.create(conn, _owner_ctx(conn), {
        "title": f"Zhoršení po {ref}: revert nebo oprava ({g['signal_key']})"[:200],
        "assignee": {"type": "agent", "id": se["id"]}, "status": "next", "priority": 1, "topic": "engineering",
        "source": FOLLOWUP_SOURCE, **({"project": where["project_id"]} if where.get("project_id") else {}),
        "deadline": datetime.now(timezone.utc).astimezone(TZ).date().isoformat(),
        "notes": (f"### Proč\nOprava {ref} „{t['title']}“ je 7 dní nasazená a signál je **horší**: "
                  f"{_numbers(g, value)}.\n\n### Co udělat\n1. Najdi, co v {ref} zhoršilo signál (commit, deploy).\n"
                  "2. Revertni tu změnu, nebo ji oprav, pokud je příčina jasná a malá.\n"
                  "3. Normální deploy; v předání napiš, co bylo příčinou.\n\n" +
                  target_block(g["signal_key"], g["baseline"], g["target"])),
        "definition_of_done": f"Signál `{g['signal_key']}` je zpět aspoň na {_num(g['baseline'])}/7 d (ověří kód "
                              "7 dní po nasazení); příčina je zapsaná.",
    })
    wake.wake(se["id"])
    return made["id"]


# ------------------------------------------------------------------ the daily job

def daily(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """05:40: smoke → digest → targets registered → verification. Code only."""
    from . import smoke

    now = _now(now)
    try:
        sm = smoke.run()
    except Exception as e:  # noqa: BLE001 - the digest without the smoke
        log.exception("ux smoke failed")
        sm = {"ran": False, "skipped": str(e)[:200], "items": {}}
    dig = signals.daily(conn, now, extra=sm.get("items") or {})
    reg = register(conn, now)
    conn.commit()
    ver = verify(conn, now)
    out = {**dig, "smoke": ("ran: " + str(len(sm.get("items") or {})) + " findings") if sm.get("ran")
           else sm.get("skipped"), **({"registered": len(reg)} if reg else {}), **ver}
    return out


# ------------------------------------------------------------------ the weekly triage (one CTO run)

def _digest(conn: sqlite3.Connection, now: datetime) -> tuple[str, list[dict]]:
    day = signals.latest_day(conn)
    if day is None or day < (now.astimezone(TZ).date() - timedelta(days=1)).isoformat():
        signals.daily(conn, now)
        day = signals.latest_day(conn)
    return day, signals.stored(conn, day) if day else []


def _open_items(conn: sqlite3.Connection, project_id: int | None) -> list[dict]:
    if not project_id:
        return []
    from .. import tasks

    ensure_schema(conn)
    rows = conn.execute("""SELECT t.id, t.title, t.status, t.assignee_name, g.signal_key FROM tasks t
                           LEFT JOIN improve_targets g ON g.task_id = t.id
                           WHERE t.project_id = ? AND t.status != 'done' AND t.archived_at IS NULL
                           AND t.parent_id IS NULL AND COALESCE(t.source, '') NOT IN (?, ?)
                           ORDER BY t.id DESC LIMIT 25""", (project_id, TRIAGE_SOURCE, COACH_SOURCE)).fetchall()
    return [{"ref": tasks.display_id(r["id"]), "title": r["title"][:110], "status": r["status"],
             "assignee": r["assignee_name"], "signal": r["signal_key"]} for r in rows]


def _recent_verdicts(conn: sqlite3.Connection, since_day: str) -> list[str]:
    from .. import tasks

    out = []
    for g in targets(conn, ("verified", "not_improved", "worse", "reopened")):
        if (g["measured_day"] or "") >= since_day:
            out.append(f"{tasks.display_id(g['task_id'])} `{g['signal_key']}` {_num(g['baseline'])} → "
                       f"{_num(g['measured_value'])} (cíl {_num(g['target'])}): {g['status']}")
    return out


def _previous_open(conn: sqlite3.Connection, source: str, title: str) -> sqlite3.Row | None:
    """This week's task already exists, or last week's is still open: (the row)."""
    same = conn.execute("SELECT id FROM tasks WHERE title = ? AND archived_at IS NULL", (title,)).fetchone()
    if same:
        return same
    return conn.execute("""SELECT id FROM tasks WHERE source = ? AND status != 'done' AND archived_at IS NULL
                           ORDER BY id DESC LIMIT 1""", (source,)).fetchone()


def triage_notes(day: str, top: list[dict], open_items: list[dict], verdicts: list[str]) -> str:
    sig = [f"{i}. (skóre {r['score']}) {signals.render_line(r)}" for i, r in enumerate(top, 1)]
    items = [f"- {o['ref']} [{o['status']}] {o['title']} ({o['assignee'] or '—'})"
             + (f" · `{o['signal']}`" if o.get("signal") else "") for o in open_items] or ["- nic"]
    return "\n".join([
        "### Proč",
        "PersonalOS se zlepšuje sám: kód každý den počítá signály (selhané běhy, chyby nástrojů, zamítnuté "
        "deploye, smyčky, frustrace majitele …), tady je týdenní výběr podle dopadu × trendu. Postupuj podle "
        "sekce **Self-improvement triage** ve svých instrukcích (nejvýš 10 kroků).",
        "",
        f"### Signály (digest {day}, 7 dní proti předchozím 7)",
        *sig,
        "",
        "### Otevřené položky „PersonalOS zlepšení“ (nezakládej duplicity)",
        *items,
        "",
        "### Výsledky ověření (posledních 7 dní)",
        *([f"- {v}" for v in verdicts] or ["- žádné"]),
        "",
        "### Co udělat",
        f"1. Vyber nejvýš {MAX_IMPROVEMENTS} zlepšení (nejvyšší skóre, co ještě nemá otevřenou položku).",
        "2. Pro každé `create_task` v projektu `personalos-zlepseni`: oprava kódu → Software Engineer; změna "
        "instrukcí agenta → Performance Coach. V notes: **Důkaz** (příklady odsud), **Oblast** (soubory), a blok:",
        "",
        "   ### Cíl",
        "   - signal: `<klíč signálu odsud>`",
        "   - baseline: <počet za 7 dní>",
        "   - target: <cílový počet za 7 dní>",
        "",
        "3. Kód blok najde sám, 7 dní po nasazení změří signál a ověří, vrátí ti úkol nebo založí urgentní opravu.",
        "4. `complete_task` s přehledem: založené úkoly (ref → signál → cíl) a co jsi vynechal a proč.",
    ])


def triage_review(conn: sqlite3.Connection, project_id: int | None, now: datetime | None = None) -> dict:
    """What the Monday 10:00 #platform meeting discusses (pos.platform_loop): this week's triage task,
    the items made in the project since it (with their signal and target state) and the verdicts of
    the last 7 days. The meeting makes no backlog of its own."""
    from .. import tasks

    now = _now(now)
    ensure_schema(conn)
    tri = conn.execute("""SELECT id, status, created_at FROM tasks WHERE source = ? AND archived_at IS NULL
                          AND created_at >= ? ORDER BY id DESC LIMIT 1""",
                       (TRIAGE_SOURCE, _iso(now - timedelta(days=7)))).fetchone()
    items = []
    if tri is not None and project_id:
        for r in conn.execute("""SELECT t.id, t.title, t.status, t.assignee_name, t.notes, g.signal_key,
                                        g.status AS target_status FROM tasks t
                                 LEFT JOIN improve_targets g ON g.task_id = t.id
                                 WHERE t.project_id = ? AND t.created_at >= ? AND t.archived_at IS NULL
                                 AND t.parent_id IS NULL AND COALESCE(t.source, '') NOT IN (?, ?)
                                 AND COALESCE(t.source, '') NOT LIKE 'meeting:%'
                                 ORDER BY t.id LIMIT 10""",
                              (project_id, tri["created_at"], TRIAGE_SOURCE, COACH_SOURCE)):
            p = parse_target(r["notes"] or "")
            items.append({"ref": tasks.display_id(r["id"]), "title": r["title"][:100], "status": r["status"],
                          "assignee": r["assignee_name"], "signal": r["signal_key"] or (p or {}).get("signal"),
                          "baseline": (p or {}).get("baseline"), "target": (p or {}).get("target"),
                          "target_status": r["target_status"]})
    since = (now.astimezone(TZ).date() - timedelta(days=7)).isoformat()
    return {"triage": {"ref": tasks.display_id(tri["id"]), "status": tri["status"]} if tri else None,
            "items": items, "verdicts": _recent_verdicts(conn, since)}


def weekly_triage(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Monday 08:30: one task for the CTO with the digest (no tokens here)."""
    from .. import platform_loop, tasks

    now = _now(now)
    ensure_schema(conn)
    cto = _agent(conn, "CTO")
    if cto is None:
        return {"skipped": "no CTO"}
    title = f"Samozlepšení: týdenní triáž · {_week(now)}"
    prev = _previous_open(conn, TRIAGE_SOURCE, title)
    if prev is not None:
        return {"skipped": f"{tasks.display_id(prev['id'])} is still open or this week's exists"}
    day, rows = _digest(conn, now)
    top = signals.ranked(rows, TOP_SIGNALS)
    if not top:
        return {"skipped": "no signals worth a run"}
    where = platform_loop.ensure(conn)
    since = (now.astimezone(TZ).date() - timedelta(days=7)).isoformat()
    t = tasks.create(conn, _owner_ctx(conn), {
        "title": title, "assignee": {"type": "agent", "id": cto["id"]}, "reviewer": cto["id"], "status": "next",
        "priority": 2, "topic": "engineering", "source": TRIAGE_SOURCE, "estimate_min": 10,
        **({"project": where["project_id"]} if where.get("project_id") else {}),
        "notes": triage_notes(day, top, _open_items(conn, where.get("project_id")), _recent_verdicts(conn, since)),
        "definition_of_done": f"Nejvýš {MAX_IMPROVEMENTS} úkolů s důkazem a blokem `### Cíl` (signál, baseline, "
                              "target) bez duplicit; v závěru přehled ref → signál → cíl.",
    })
    audit.log(conn, _owner_ctx(conn), "improve_triage", "task", t["id"], signals=len(top), day=day)
    conn.commit()
    return {"task": t["ref"], "signals": len(top)}


# ------------------------------------------------------------------ instruction tuning (one Performance Coach run)

def worst_agents(conn: sqlite3.Connection, now: datetime | None = None, limit: int = COACH_AGENTS) -> list[dict]:
    """The agents with the worst success rate or the most refused tool calls over 7 days."""
    now = _now(now)
    s, u = _iso(now - timedelta(days=7)), _iso(now)
    stats: dict[int, dict] = {}
    for r in conn.execute("""SELECT a.id, a.name, SUM(r.status = 'ok') AS ok, SUM(r.status = 'error') AS err
                             FROM runs r JOIN actors a ON a.id = r.actor_id
                             WHERE r.started_at >= ? AND r.started_at < ? AND r.status IN ('ok', 'error')
                             AND a.kind != 'human' AND a.archived_at IS NULL GROUP BY a.id""", (s, u)):
        stats[r["id"]] = {"id": r["id"], "name": r["name"], "ok": r["ok"] or 0, "err": r["err"] or 0, "tool_errors": 0}
    for r in conn.execute("""SELECT a.id, a.name, COUNT(*) AS n FROM audit_log l JOIN actors a ON a.id = l.actor_id
                             WHERE l.action LIKE 'mcp:%:refused' AND l.at >= ? AND l.at < ? AND a.kind != 'human'
                             AND a.archived_at IS NULL GROUP BY a.id""", (s, u)):
        stats.setdefault(r["id"], {"id": r["id"], "name": r["name"], "ok": 0, "err": 0, "tool_errors": 0})
        stats[r["id"]]["tool_errors"] = r["n"]
    out = []
    for a in stats.values():
        runs = a["ok"] + a["err"]
        a["success"] = round(a["ok"] / runs, 3) if runs else None
        bad_rate = a["err"] > 0 and runs >= COACH_MIN_RUNS
        if bad_rate or a["tool_errors"] >= 3:
            out.append(a)
    out.sort(key=lambda a: (a["success"] if a["success"] is not None and a["err"] else 1.0, -a["tool_errors"],
                            -a["err"], a["name"]))
    return out[:limit]


def agent_evidence(conn: sqlite3.Connection, agent_id: int, now: datetime | None = None) -> list[str]:
    """An agent's failing runs and refused tool calls over 7 days, as short lines."""
    from .. import tasks

    now = _now(now)
    s, u = _iso(now - timedelta(days=7)), _iso(now)
    lines = []
    for r in conn.execute("""SELECT r.id, r.task_id, r.detail, t.title FROM runs r LEFT JOIN tasks t ON t.id = r.task_id
                             WHERE r.actor_id = ? AND r.status = 'error' AND r.started_at >= ? AND r.started_at < ?
                             ORDER BY r.id DESC LIMIT 5""", (agent_id, s, u)):
        where = f"{tasks.display_id(r['task_id'])} „{(r['title'] or '')[:60]}“" if r["task_id"] else "bez úkolu"
        lines.append(f"- běh {r['id']} ({where}): {' '.join((r['detail'] or '').split())[:200]}")
    refused: dict[str, list] = {}
    for r in conn.execute("""SELECT action, detail, run_id FROM audit_log WHERE actor_id = ? AND action LIKE 'mcp:%:refused'
                             AND at >= ? AND at < ? ORDER BY id DESC""", (agent_id, s, u)):
        try:
            reason = (json.loads(r["detail"] or "{}") or {}).get("reason") or ""
        except (TypeError, ValueError):
            reason = ""
        key = f"{r['action'][4:-len(':refused')]}: {' '.join(reason.split())[:160]}"
        refused.setdefault(key, []).append(r["run_id"])
    for key, runs in sorted(refused.items(), key=lambda kv: -len(kv[1]))[:5]:
        ids = ", ".join(str(x) for x in runs[:3] if x)
        lines.append(f"- odmítnuté volání {len(runs)}× — {key}" + (f" (běhy {ids})" if ids else ""))
    if signals._has(conn, "task_summaries"):
        for r in conn.execute("""SELECT ts.task_id, ts.text FROM task_summaries ts JOIN runs r ON r.task_id = ts.task_id
                                 WHERE r.actor_id = ? AND r.status = 'error' AND r.started_at >= ? AND r.started_at < ?
                                 GROUP BY ts.task_id ORDER BY ts.task_id DESC LIMIT 2""", (agent_id, s, u)):
            lines.append(f"- shrnutí {tasks.display_id(r['task_id'])}: {' '.join((r['text'] or '').split())[:240]}")
    return lines


def weekly_coach(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Monday 08:40: one task for the Performance Coach with the 3 worst agents' failures."""
    from .. import roles, tasks

    now = _now(now)
    coach = _agent(conn, roles.COACH)
    if coach is None:
        return {"skipped": "no Performance Coach"}
    title = f"Samozlepšení: ladění instrukcí · {_week(now)}"
    prev = _previous_open(conn, COACH_SOURCE, title)
    if prev is not None:
        return {"skipped": f"{tasks.display_id(prev['id'])} is still open or this week's exists"}
    worst = worst_agents(conn, now)
    if not worst:
        return {"skipped": "no agent with failing runs or refused tool calls"}
    parts = []
    for a in worst:
        rate = "—" if a["success"] is None else f"{round(a['success'] * 100)} %"
        parts.append(f"### {a['name']}: úspěšnost {rate} ({a['ok']} ok, {a['err']} selhalo), odmítnutých volání "
                     f"{a['tool_errors']}\n" + "\n".join(agent_evidence(conn, a["id"], now) or ["- (bez detailu)"]))
    t = tasks.create(conn, _owner_ctx(conn), {
        "title": title, "assignee": {"type": "agent", "id": coach["id"]}, "reviewer": coach["id"], "status": "next",
        "priority": 2, "topic": "agents", "source": COACH_SOURCE, "estimate_min": 10,
        "notes": ("### Proč\nTři agenti s nejhorší úspěšností nebo nejvíc odmítnutými voláními nástrojů za 7 dní "
                  "(kód, pos.improve). Postupuj podle sekce **Instruction tuning** ve svých instrukcích.\n\n"
                  "### Co udělat\nU každého agenta: je-li příčina v jeho instrukcích (špatný formát argumentu, "
                  "zakázaný postup, chybějící krok), navrhni **jednu** krátkou opravu přes `propose_instructions` "
                  "(důvod = příklad odsud). Je-li příčina v platformě (timeout, výpadek, chyba nástroje), nic "
                  "nenavrhuj a napiš to do závěru (CTO to uvidí v triáži). Nanejvýš 3 návrhy.\n\n" + "\n\n".join(parts)),
        "definition_of_done": "U každého agenta návrh instrukcí (odkaz) nebo zdůvodnění, proč ne.",
    })
    audit.log(conn, _owner_ctx(conn), "improve_coach", "task", t["id"], agents=[a["name"] for a in worst])
    conn.commit()
    return {"task": t["ref"], "agents": [a["name"] for a in worst]}


# ------------------------------------------------------------------ the owner's one section

SECTION = "Samo se zlepšilo tento týden"


def packet_section(conn: sqlite3.Connection, s: str, u: str) -> dict:
    """Verified improvements measured in [s, u]: at most 3 lines with before → after."""
    from .. import tasks

    ensure_schema(conn)
    sd = datetime.fromisoformat(s).astimezone(TZ).date().isoformat()
    ud = datetime.fromisoformat(u).astimezone(TZ).date().isoformat()
    rows = [g for g in targets(conn, ("verified",)) if sd <= (g["measured_day"] or "") <= ud]
    rows.sort(key=lambda g: -((g["baseline"] or 0) - (g["measured_value"] or 0)))
    lines = [f"{g['title'][:90]} ({tasks.display_id(g['task_id'])}): {_label(g['signal_key'])} "
             f"{_num(g['baseline'])} → {_num(g['measured_value'])} za týden" for g in rows[:OWNER_LINES]]
    return {"title": SECTION, "lines": lines, "verified": len(rows)}


def _label(key: str) -> str:
    cat = key.split(":", 1)[0]
    return {"run_error": "selhané běhy", "tool_error": "chyby nástroje", "deploy_rejected": "odmítnuté deploye",
            "loop": "smyčky", "owner": "nevyřízené zprávy majitele", "cost_cap": "zastavení o strop",
            "guard": "zamítnuté příkazy", "stuck": "zaseknuté úkoly", "ux": "chyby webu",
            "agent_fail": "selhané běhy agenta"}.get(cat, "výskyty")


def render_section(sec: dict | None) -> str:
    if not sec or not sec.get("lines"):
        return ""
    return f"## {SECTION}\n" + "\n".join(f"- {line}" for line in sec["lines"])


def with_section(narrative: str, sec: dict | None) -> str:
    """The report's narrative with the section appended (once; nothing when nothing was verified)."""
    block = render_section(sec)
    if not block or SECTION in (narrative or ""):
        return narrative
    return (narrative or "").rstrip() + "\n\n" + block


def stats(conn: sqlite3.Connection) -> dict:
    """Counts by status, for the CLI and tests."""
    ensure_schema(conn)
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM improve_targets GROUP BY status")}
