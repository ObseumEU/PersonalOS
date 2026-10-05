"""The agent company improves itself: a weekly meeting, a backlog, a Friday retro with the numbers.

- **#platform** (created here): the CTO, the Software Engineer, the QA Reviewer, the SRE and the Security
  Engineer, and the owner. The project **"PersonalOS zlepšení"** is tied to it, so the tasks a meeting
  decides land in that project's backlog.
- **Monday 10:00 "Platforma: zlepšení týdne"** (job `platform_meeting`): a meeting (pos.meetings) led by
  the CTO. Its agenda is built in code: the scorecard's platform section (runs, reviews, loops, spend
  against the ≤ 30 % cap), last week's failed runs, failed deploys and incidents, and the owner's
  frustration (flagged messages, double answers, unanswered asks; pos.frustration). Output: 3–5 backlog
  items (2 when the platform is over its cap), each with evidence, a metric, the file area and an owner,
  created by `meeting_decide` in the project; the Software Engineer ships them through the normal deploy
  flow.
- **Friday 12:00 retro** (job `platform_retro`): the platform metrics this week against last week and the
  backlog's state, posted in #platform; the same section goes into the Chief of Staff's weekly packet
  (`packet_section`, read by pos.weekly_packet), so the 14:00 report carries it.

The cap (platform ≤ 30 % of spend) is the CEO's to enforce: its Monday plan carries the share
(pos.scorecard.with_scorecard) and the meeting's agenda shrinks the backlog when it is over.
"""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors, audit
from .core import Ctx

log = logging.getLogger(__name__)

CHANNEL = "platform"
PROJECT = "PersonalOS zlepšení"
PROJECT_SLUG = "personalos-zlepseni"
TOPIC = "Platforma: zlepšení týdne"
FACILITATOR = "CTO"
PARTICIPANTS = ("Software Engineer", "QA Reviewer", "SRE", "Security Engineer")
AGENDA_MAX = 2900
RETRO_KPIS = (("runs_failed", "selhané běhy"), ("fail_rate", "podíl selhání"), ("review_queue", "fronta revizí"),
              ("review_oldest_hours", "nejstarší revize (h)"), ("loops", "smyčky"), ("frustrations", "frustrace majitele"),
              ("double_answers", "dvojí odpovědi"), ("unanswered", "bez odpovědi"), ("platform_share", "podíl platformy"))


def _active(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM actors WHERE name = ? AND archived_at IS NULL", (name,)).fetchone()


def team(conn: sqlite3.Connection) -> tuple[int | None, list[int]]:
    """(the CTO, the participants that exist and can take part)."""
    cto = _active(conn, FACILITATOR)
    people = []
    for n in PARTICIPANTS:
        r = _active(conn, n)
        if r is not None and r["kind"] != "human" and r["runtime"] != "service":
            people.append(r["id"])
    return (cto["id"] if cto else None), people


def ensure(conn: sqlite3.Connection) -> dict:
    """#platform and the project "PersonalOS zlepšení" exist, with the team in them. Idempotent."""
    from . import chat, projects

    owner = Ctx(actors.owner_id(conn), via="system")
    cto, people = team(conn)
    if not cto:  # no engineering lead (a fresh install, the tests): nothing to run the loop
        return {"channel_id": None, "project_id": None}
    row = conn.execute("SELECT * FROM channels WHERE kind = 'group' AND name = ? COLLATE NOCASE AND archived_at IS NULL",
                       (CHANNEL,)).fetchone()
    if row is None:
        cid = chat.create_channel(conn, owner, CHANNEL, [], visibility="team",
                                  topic="Zlepšování PersonalOS: pondělní porada (CTO), backlog „PersonalOS zlepšení“, "
                                        "páteční retro s čísly.")["id"]
    else:
        cid = row["id"]
    for aid in [a for a in (cto, *people) if a]:
        chat._add_member(conn, cid, aid)
    p = conn.execute("SELECT * FROM projects WHERE slug = ? OR name = ?", (PROJECT_SLUG, PROJECT)).fetchone()
    if p is None:
        made = projects.create(conn, owner, name=PROJECT, slug=PROJECT_SLUG, channel=False,
                               lead=cto if cto else None, member_refs=[a for a in people if a != cto],
                               goal="Agentní firma zlepšuje sama sebe: méně selhání, kratší fronty, spokojený majitel; "
                                    "platforma nejvýš 30 % nákladů.",
                               definition_of_done="Každá položka má důkaz, metriku, oblast souborů a vlastníka; "
                                                  "v pátek je změřená.")
        pid = made["id"]
    else:
        pid = p["id"]
    conn.execute("UPDATE projects SET channel_id = ? WHERE id = ? AND channel_id IS NOT ?", (cid, pid, cid))
    conn.commit()
    return {"channel_id": cid, "project_id": pid}


def _window(now: datetime, days: int = 7) -> tuple[str, str]:
    return (now - timedelta(days=days)).isoformat(timespec="seconds"), now.isoformat(timespec="seconds")


def failures(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """Last week's failed runs (by agent), failed deploys and incidents, as short lines."""
    now = now or datetime.now(timezone.utc)
    s, u = _window(now)
    out = []
    for r in conn.execute("""SELECT a.name, COUNT(*) AS n, MAX(r.detail) AS detail FROM runs r JOIN actors a ON a.id = r.actor_id
                             WHERE r.status = 'error' AND r.started_at >= ? AND r.started_at < ?
                             GROUP BY a.name ORDER BY n DESC LIMIT 5""", (s, u)):
        out.append(f"selhané běhy {r['name']}: {r['n']}× ({' '.join((r['detail'] or '').split())[:90]})")
    for r in conn.execute("""SELECT status, stage, COUNT(*) AS n FROM deploys WHERE status != 'ok' AND created_at >= ?
                             AND created_at < ? GROUP BY status, stage ORDER BY n DESC LIMIT 3""", (s, u)):
        out.append(f"deploy {r['status']} ve fázi {r['stage'] or '?'}: {r['n']}×")
    try:
        for r in conn.execute("""SELECT title, severity FROM sentinel_incidents WHERE opened_at >= ? AND opened_at < ?
                                 ORDER BY id DESC LIMIT 3""", (s, u)):
            out.append(f"incident ({r['severity']}): {r['title'][:100]}")
    except sqlite3.OperationalError:
        pass
    return out


def frustrations(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    from . import frustration

    now = now or datetime.now(timezone.utc)
    s, u = _window(now)
    return [f"{(f['created_at'] or '')[:10]} [{', '.join(f['markers'])}]: „{' '.join((f.get('body') or '').split())[:110]}“"
            for f in frustration.flagged(conn, s, u)[-5:]]


def _signal_lines(conn: sqlite3.Connection, limit: int = 5) -> list[str]:
    """The top signals of the daily digest (pos.improve.signals); the CTO's 08:30 triage already made
    tasks from them, so the meeting does not duplicate those."""
    try:
        from .improve import signals

        day, rows = signals.latest(conn)
        top = signals.ranked(rows, limit)
    except Exception:  # noqa: BLE001 - the meeting goes on without them
        log.exception("signal digest unavailable")
        return []
    if not top:
        return []
    return ["**Signály (denní digest, už v pondělní triáži CTO; neduplikuj):**",
            *[f"- `{r['key']}` {signals._num(r['count_7d'])}/7 d (předtím {signals._num(r.get('prev_7d'))})"
              for r in top]]


def agenda(conn: sqlite3.Connection, now: datetime | None = None) -> str:
    """The meeting's agenda: the input in numbers and what the meeting must produce."""
    from . import scorecard

    card = scorecard.view(conn, now)
    share = card["spend"].get("platform_share")
    over = share is not None and share > scorecard.PLATFORM_CAP
    items = "2" if over else "3–5"
    fails = failures(conn, now) or ["nic"]
    frus = frustrations(conn, now) or ["žádná označená zpráva"]
    text = "\n".join([
        "**Vstup (čísla z kódu, 7 dní):**", scorecard.render_platform(card),
        "**Selhání a incidenty:**", *[f"- {x}" for x in fails],
        "**Frustrace majitele:**", *[f"- {x}" for x in frus],
        *_signal_lines(conn),
        f"**Výstup:** {items} položky backlogu v projektu „{PROJECT}“ (meeting_decide tasks). Každá: název; v notes "
        "**Důkaz** (číslo nebo zpráva odsud), **Metrika** (co se zlepší a o kolik do pátku), **Oblast** (soubory "
        "a moduly); assignee vlastník (implementace Software Engineer přes normální deploy); definition_of_done "
        "s metrikou. Nejdřív to, co trápí majitele.",
        ("**Strop překročen:** platforma " + f"{round(share * 100)} % > 30 % nákladů: jen položky, které snižují "
         "náklady nebo selhání." if over else "**Strop:** platforma ≤ 30 % nákladů (hlídá CEO)."),
    ])
    return text[:AGENDA_MAX]


def start_meeting(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The job (Monday 10:00): open the week's improvement meeting in #platform."""
    from . import meetings

    where = ensure(conn)
    cto, people = team(conn)
    if not cto or not people or not where["channel_id"]:
        return {"skipped": "no CTO or no participants"}
    try:
        m = meetings.start(conn, Ctx(cto, via="schedule"), where["channel_id"], TOPIC, agenda(conn, now),
                           participants=people, rounds=2, facilitator=cto)
    except (meetings.MeetingError, Exception) as e:  # noqa: BLE001 - a running meeting, a paused agent: next week
        conn.rollback()
        return {"skipped": f"meeting not started: {e}"[:300]}
    return {"meeting": m["id"]}


# ------------------------------------------------------------------ the Friday retro

def backlog(conn: sqlite3.Connection, s: str, u: str) -> dict:
    from . import tasks

    p = conn.execute("SELECT id FROM projects WHERE slug = ?", (PROJECT_SLUG,)).fetchone()
    if p is None:
        return {"done": [], "open": []}
    rows = conn.execute("""SELECT id, title, status, assignee_name, completed_at FROM tasks WHERE project_id = ?
                           AND archived_at IS NULL AND parent_id IS NULL AND COALESCE(topic, '') != 'meeting'
                           ORDER BY id""", (p["id"],)).fetchall()
    done = [r for r in rows if r["status"] == "done" and (r["completed_at"] or "") >= s and (r["completed_at"] or "") <= u]
    open_ = [r for r in rows if r["status"] != "done"]
    f = lambda r: {"ref": tasks.display_id(r["id"]), "title": r["title"][:120], "assignee": r["assignee_name"],  # noqa: E731
                   "status": r["status"]}
    return {"done": [f(r) for r in done], "open": [f(r) for r in open_[:10]], "open_total": len(open_)}


def packet_section(conn: sqlite3.Connection, s: str, u: str, now: datetime | None = None) -> dict:
    """The platform improvement section of the weekly packet: metric deltas and the backlog."""
    from . import scorecard

    if now is not None and now < datetime.now(timezone.utc) - timedelta(hours=1):  # a past week: its snapshot
        card = scorecard.historic(conn, scorecard._day(now))
        if card is None:
            return {"available": False, "note": "no scorecard snapshot for that day"}
    else:
        card = scorecard.view(conn)
    k = card["kpis"]
    return {"compared_with": card.get("compared_with"),
            "metrics": {key: {"label": label, **k[key]} for key, label in RETRO_KPIS},
            "backlog": backlog(conn, s, u), "platform_share": card["spend"].get("platform_share"),
            "platform_cap": scorecard.PLATFORM_CAP}


def _fmt(key: str, v) -> str:
    if v is None:
        return "—"
    if key in ("fail_rate", "platform_share"):
        return f"{round(v * 100)} %"
    return str(int(v)) if float(v).is_integer() else f"{v:.1f}"


def render_retro(sec: dict) -> str:
    lines = [f"🔁 **{TOPIC}: retro**" + (f" (proti {sec['compared_with']})" if sec.get("compared_with") else "")]
    for key, m in sec["metrics"].items():
        arrow = "" if m.get("good") is None else (" ✅" if m["good"] else " ⚠️")
        lines.append(f"- {m['label']}: {_fmt(key, m.get('prev'))} → **{_fmt(key, m.get('value'))}**{arrow}")
    b = sec["backlog"]
    lines.append(f"**Backlog „{PROJECT}“:** hotovo {len(b['done'])}, otevřeno {b.get('open_total', len(b['open']))}")
    lines += [f"- ✔ {t['ref']} {t['title']} ({t['assignee'] or '—'})" for t in b["done"]]
    share = sec.get("platform_share")
    if share is not None and share > sec["platform_cap"]:
        lines.append(f"⚠️ Platforma {round(share * 100)} % nákladů, strop 30 %: CEO krátí příští backlog.")
    return "\n".join(lines)


def retro(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """The job (Friday 12:00): the retro in #platform; the weekly packet reads the same section."""
    from . import chat

    now = now or datetime.now(timezone.utc)
    where = ensure(conn)
    if not where["channel_id"]:
        return {"skipped": "no CTO"}
    s, u = _window(now)
    sec = packet_section(conn, s, u, now)
    cto, _ = team(conn)
    author = cto or actors.system_id(conn)
    msg = chat.send(conn, Ctx(author, via="system"), where["channel_id"], render_retro(sec), system=True)
    audit.log(conn, Ctx(author, via="system"), "platform_retro", "channel", where["channel_id"],
              done=len(sec["backlog"]["done"]), message=msg["id"])
    conn.commit()
    return {"posted": msg["id"], "done": len(sec["backlog"]["done"])}
