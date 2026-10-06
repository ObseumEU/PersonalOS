"""Done means delivered: a task is not done while its acceptance criteria are unmet or its parts were dropped.

Prod 2026-10 (the Kniha audit): T-373's definition of done said "Objednávku lze založit formulářem" and it
was accepted with no order form anywhere; the public order form, photo upload and the real composition
were dropped silently while the parent was reported done. pos.evidence only nudges ("doložte"); this
module blocks, for agents (people are never gated):

- **Acceptance criteria** are the task's definition_of_done, split into items (lines, bullets, numbered
  points, else `;`). A hand-in (complete_task / request_review, status → review or done) needs evidence
  per criterion: `criteria_evidence` on complete_task, or lines `K1: <evidence>` in the result; with a
  single criterion the whole result is its evidence. Evidence is what pos.evidence verifies (a commit,
  a URL, a sent message's id, a file or note id, test output; the task's own sends and deploys), none of
  it failing. A criterion that speaks of a URL or of being live ("na webu", "nasazeno", "live",
  "https://…") also needs a URL that passes the anonymous outsider probe (pos.reality.probe: 2xx, no
  password, no login).
- **Children**: a parent cannot be handed in or accepted while a step (child task) is open, or was
  archived without a reason (no comment and no note on it).
- **Accepting**: an agent reviewer accepting a result re-runs the same check on the evidence recorded at
  the hand-in (live URLs probed again); a result that does not pass is returned, not accepted.

Exempt: review work items (`review:`), the owner's tickets (`ask_owner`), the CEO's promises, routine
checks from a schedule and triaged mail (their summary is the result), documents (the text is the
deliverable; children still count). A refusal records `delivery_incomplete` (an improve-loop signal) and
tells the agent exactly what is missing. POS_DELIVERY_GATE=0 switches it off.
"""

import contextlib
import contextvars
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import audit, reality
from .core import Ctx, now_iso

EXEMPT_SOURCES = ("review:", "ask_owner", "promise:", "hire_probe:")
MAX_CRITERIA = 12
LIVE_RE = re.compile(r"(?<![a-z])(?:url|odkaz|adres[ae]|https?://|zive|zivy|ziva|live|nasazen|deploy|produkc|"
                     r"verejne|na webu|v prohlizeci|online|formular|zakaznik muze|uzivatel muze|lze zalozit|"
                     r"lze objednat|lze vyzkouset|lze zaplatit)", re.IGNORECASE)
K_LINE_RE = re.compile(r"(?im)^\s*[-*•]?\s*(?:\*\*)?(?:k|ac|krit[ée]rium|kriterium|criterion|krit\.?)\s*#?\s*(\d{1,2})"
                       r"(?:\*\*)?\s*[:.)\-–—]\s*(.+)$")
BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)]|\[[ xX]\])\s+")

_criteria_input: contextvars.ContextVar = contextvars.ContextVar("pos_delivery_criteria", default=None)

_SCHEMA = """CREATE TABLE IF NOT EXISTS task_criteria_evidence (
    id        INTEGER PRIMARY KEY,
    task_id   INTEGER NOT NULL,
    idx       INTEGER NOT NULL,
    criterion TEXT NOT NULL,
    evidence  TEXT NOT NULL DEFAULT '',
    status    TEXT NOT NULL,
    why       TEXT NOT NULL DEFAULT '',
    actor_id  INTEGER,
    at        TEXT NOT NULL
)"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.execute("CREATE INDEX IF NOT EXISTS task_criteria_evidence_task ON task_criteria_evidence (task_id, at)")


def enabled() -> bool:
    return os.environ.get("POS_DELIVERY_GATE", "1") != "0"


@contextlib.contextmanager
def criteria_input(value):
    """complete_task's criteria_evidence, seen by check() inside."""
    token = _criteria_input.set(value)
    try:
        yield
    finally:
        _criteria_input.reset(token)


def criteria(dod: str | None) -> list[str]:
    """The acceptance criteria in a definition of done."""
    lines = []
    for raw in (dod or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(BULLET_RE.sub("", line).strip())
    lines = [x for x in lines if x]
    if len(lines) == 1:
        parts = [p.strip() for p in lines[0].split(";") if p.strip()]
        if len(parts) > 1 and all(len(p.split()) >= 2 for p in parts):
            lines = parts
    return lines[:MAX_CRITERIA]


def _mapping(raw, n: int, text: str) -> dict[int, str]:
    """{criterion number (1-based): evidence text} from criteria_evidence and K-lines in the text."""
    out: dict[int, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if str(k).strip().lstrip("Kk#").isdigit():
                out[int(str(k).strip().lstrip("Kk#"))] = str(v)
    elif isinstance(raw, (list, tuple)):
        for i, item in enumerate(raw, start=1):
            if isinstance(item, dict):
                k = item.get("criterion", item.get("k", i))
                num = int(str(k).strip().lstrip("Kk#")) if str(k).strip().lstrip("Kk#").isdigit() else i
                out[num] = str(item.get("evidence") or item.get("text") or "")
            else:
                out[i] = str(item)
    elif isinstance(raw, str) and raw.strip():
        text = raw + "\n" + text
    for m in K_LINE_RE.finditer(text or ""):
        out.setdefault(int(m.group(1)), "")
        out[int(m.group(1))] = (out[int(m.group(1))] + " " + m.group(2)).strip()
    return {k: v for k, v in out.items() if 1 <= k <= n and v.strip()}


def _verify(conn: sqlite3.Connection, criterion: str, evidence: str, task_id: int | None,
            since: str | None, items: list | None = None) -> tuple[str, str]:
    """(status, why): ok | missing | failed."""
    from . import evidence as ev

    if not evidence.strip():
        return "missing", "chybí důkaz (sha commitu, URL, id odeslané zprávy, soubor nebo poznámka, výstup testů)"
    if items is None:
        items = ev.gather(conn, evidence, task_id, since=since)
    failed = [i for i in items if i.status == "failed"]
    if failed:
        return "failed", "důkaz neprošel: " + "; ".join(i.label() for i in failed[:3])
    if not items:
        return "missing", "v textu není ověřitelný důkaz (sha commitu, URL, id odeslané zprávy, soubor/poznámka, " \
                          "výstup testů)"
    if LIVE_RE.search(reality.norm(criterion)):
        urls = list(dict.fromkeys(u.rstrip(".,;:!?)'\"") for u in ev.URL_RE.findall(evidence)))[:4]
        if not urls:
            return "missing", "kritérium mluví o živé adrese: doložte URL, kterou otevře běžný uživatel"
        probes = [reality.probe(u) for u in urls]
        if not any(p.ok or not p.verified for p in probes):
            return "failed", "adresa neprošla kontrolou zvenku: " + "; ".join(p.why for p in probes[:2])
    return "ok", ""


def _children(conn: sqlite3.Connection, task_id: int) -> list[str]:
    from .tasks import display_id

    out = []
    for c in conn.execute("SELECT id, status, archived_at, progress_note FROM tasks WHERE parent_id = ? ORDER BY id",
                          (task_id,)).fetchall():
        if c["archived_at"] is None:
            if c["status"] != "done":
                out.append(f"{display_id(c['id'])} je {c['status']}")
            continue
        if c["status"] == "done":
            continue
        has_reason = bool((c["progress_note"] or "").strip()) or conn.execute(
            "SELECT 1 FROM task_comments WHERE task_id = ? AND kind != 'system' LIMIT 1", (c["id"],)).fetchone()
        if not has_reason:
            out.append(f"{display_id(c['id'])} archivován bez důvodu")
    return out


def _gated(conn: sqlite3.Connection, ctx: Ctx, row) -> bool:
    if not enabled():
        return False
    who = conn.execute("SELECT kind FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()
    if who is None or who["kind"] == "human":
        return False
    return not (row["source"] or "").startswith(EXEMPT_SOURCES)


# Tasks agents and people create (their source is the channel: mcp, api, ...). Tasks the platform makes for
# itself (chat answers, deploy reviews, customer issues, access queues, incidents) carry their own source and
# their own completion rules.
WORK_SOURCES = ("mcp", "api", "web", "capture", "runner", "worker", "handoff", "")


def _criteria_exempt(conn: sqlite3.Connection, row, note: str | None) -> bool:
    from . import review_policy

    if (row["source"] or "") not in WORK_SOURCES or (row["topic"] or "") == "chat":
        return True  # (a chat answer is already in front of the person)
    try:
        return review_policy.is_routine(conn, row) or review_policy.is_triage(row) or review_policy.is_doc(row, note)
    except Exception:  # noqa: BLE001
        return False


def _since(conn: sqlite3.Connection, ctx: Ctx) -> str:
    if ctx.run_id:
        r = conn.execute("SELECT started_at FROM runs WHERE id = ?", (ctx.run_id,)).fetchone()
        if r is not None and r["started_at"]:
            return r["started_at"]
    return (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")


def _stored(conn: sqlite3.Connection, task_id: int) -> dict[int, str]:
    ensure_schema(conn)
    last = conn.execute("SELECT MAX(at) FROM task_criteria_evidence WHERE task_id = ?", (task_id,)).fetchone()[0]
    if not last:
        return {}
    return {r["idx"]: r["evidence"] for r in conn.execute(
        "SELECT idx, evidence FROM task_criteria_evidence WHERE task_id = ? AND at = ?", (task_id, last))}


def check(conn: sqlite3.Connection, ctx: Ctx, row, note: str | None, *, accepting: bool = False,
          extra_text: str = "") -> dict | None:
    """Before a task becomes review or done (or is accepted): raise tasks.Invalid when a criterion has no
    evidence that checks out, or a step was dropped. Returns what was checked (None when not gated)."""
    if not _gated(conn, ctx, row):
        return None
    from .tasks import Invalid, display_id

    ensure_schema(conn)
    problems = _children(conn, row["id"])
    crit = [] if _criteria_exempt(conn, row, note) else criteria(row["definition_of_done"])
    results = []
    if crit:
        if accepting:
            mapping = _stored(conn, row["id"]) or _mapping(None, len(crit), row["progress_note"] or "")
            for k, v in _mapping(None, len(crit), note or "").items():
                if k not in mapping:
                    mapping[k] = v
            pool = row["progress_note"] or ""
        else:
            pool = "\n".join(x for x in (note, extra_text) if x)
            mapping = _mapping(_criteria_input.get(), len(crit), pool)
        since = _since(conn, ctx) if not accepting else None
        # Criteria without their own K-line share the whole result: it must hold at least one piece of
        # evidence per such criterion (one commit does not prove five criteria).
        unmapped = [i for i in range(1, len(crit) + 1) if i not in mapping]
        pool = K_LINE_RE.sub("", pool)  # a K-line proves its own criterion, not the others
        shared, shared_items = None, None
        if unmapped and pool.strip():
            from . import evidence as ev

            shared_items = ev.gather(conn, pool, row["id"], since=since)
            usable = [x for x in shared_items if x.status != "failed"]
            if not any(x.status == "failed" for x in shared_items) and 0 < len(usable) < len(unmapped):
                shared = (f"výsledek má {len(usable)} důkaz(y) na {len(unmapped)} kritéria bez vlastního řádku: "
                          "doplň řádky 'K<n>: <důkaz>'")
        for i, c in enumerate(crit, start=1):
            if i in mapping:
                st, why = _verify(conn, c, mapping[i], None, since)
                evid = mapping[i]
            elif shared:
                st, why, evid = "missing", shared, pool
            else:
                st, why = _verify(conn, c, pool, row["id"], since, shared_items)
                evid = pool
            results.append({"idx": i, "criterion": c, "evidence": evid, "status": st, "why": why})
    bad = [r for r in results if r["status"] != "ok"]
    ref = display_id(row["id"])
    if problems or bad:
        parts = []
        if bad:
            parts.append("kritéria hotovo nejsou doložená: " + "; ".join(
                f"K{r['idx']} „{r['criterion'][:80]}“: {r['why']}" for r in bad))
        if problems:
            parts.append("dílčí úkoly nejsou uzavřené: " + ", ".join(problems)
                         + " (dokonči je, nebo k archivovanému napiš důvod přes task_comment)")
        audit.log(conn, ctx, "delivery_incomplete", "task", row["id"], accepting=accepting,
                  missing=[r["idx"] for r in bad], children=problems[:10])
        conn.commit()  # the signal stays although the caller rolls its call back
        how = ("Vrať výsledek (review_task verdict=changes) s tím, co chybí." if accepting else
               "Doplň důkaz ke každému kritériu (řádky 'K1: <důkaz>' ve výsledku, nebo criteria_evidence v "
               "complete_task) a odevzdej znovu. Hotovo znamená, že to funguje skutečnému uživateli.")
        raise Invalid(f"{ref} nelze {'přijmout' if accepting else 'odevzdat jako hotové'}: " + " ".join(parts)
                      + ". " + how)
    if results:
        at = now_iso()
        for r in results:
            conn.execute("""INSERT INTO task_criteria_evidence (task_id, idx, criterion, evidence, status, why,
                            actor_id, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                         (row["id"], r["idx"], r["criterion"][:500], r["evidence"][:4000], r["status"], r["why"],
                          ctx.actor_id, at))
        audit.log(conn, ctx, "delivery_ok", "task", row["id"], accepting=accepting, criteria=len(results))
    return {"criteria": [{k: r[k] for k in ("idx", "criterion", "status")} for r in results], "children_ok": True}


def criteria_view(conn: sqlite3.Connection, row) -> list[dict]:
    """The criteria of a task with the evidence recorded at its last hand-in (for get_task)."""
    crit = criteria(row["definition_of_done"])
    if not crit:
        return []
    stored = _stored(conn, row["id"])
    return [{"k": f"K{i}", "criterion": c, "evidence": stored.get(i, "")[:300]} for i, c in enumerate(crit, start=1)]


def parse_input(raw) -> object:
    """criteria_evidence as an agent may pass it (a list, a dict, or a JSON string of either)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw
