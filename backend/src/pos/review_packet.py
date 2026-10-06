"""The review packet: what a reviewer needs to judge a result, built in code, in the run's prompt.

Measured on prod 2026-10-04: the first 40 review items cost $9.25 (≈ $0.23 each; the CEO 22 of them),
~6 tool calls each, and every call re-reads a ~20k-token context: the reviewer explored (get_task, the
report, comments, the repo) to find what the platform already knew. Now a review item is served with
a compact packet instead of "read T-x and its report":

- the result's definition of done, the hand-in note and the owner's report takeaway;
- the evidence: the verification line, the commits named (with `git show --stat` when the repository
  is reachable here, POS_REVIEW_REPOS), what was sent outside or waits for approval, the last comments;
- the risk class (pos.review_policy.decide) and a **checklist per kind** (code, outbound, docs,
  routine, plan, general), and the verdict call to make.

Routine packets (routine checks, documents) run at low effort (the run's size S: pos_worker.triage
size_settings) and are **batched**: up to BATCH_MAX open review items of the same reviewer and kind are
served as one run; reviewing a result closes its own item (pos.review_work.close_for), so the batched
items never start runs of their own. Code, outbound, plans and anything high risk get one packet per run
at the agent's own effort.

The estimate before/after on a database snapshot (no change):
`python -m pos.review_packet --estimate 2026-10-04 2026-10-05 [--db snap.db]`.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

from . import review_policy, verification

BATCH_MAX = 5
BATCH_KINDS = ("routine", "docs")
NOTE_MAX = 3000          # the hand-in note of a single packet
NOTE_MAX_BATCHED = 1200  # each one in a batch
COMMENTS = 3
STAT_LINES = 15

CHECKLISTS = {
    "code": [
        "Tests: the verification line shows the tests passing (e.g. '12 passed', CI green). No line, or a "
        "failure → return with 'spusťte testy a napište, co ukázaly'.",
        "The diff matches the definition of done: every DoD point maps to a change in the files changed; "
        "nothing unrelated (no drive-by refactors, no stray files).",
        "A commit sha is named and exists (the stat below); a claim of 'deployed/merged' names where.",
    ],
    "outbound": [
        "What was sent (or waits for approval) matches the decision and the task: the recipient, the "
        "amounts, the dates, the promises. Nothing promised beyond what was decided or approved.",
        "Tone and language: Czech unless the recipient wrote otherwise, polite, no internal ids or jargon.",
        "The evidence of sending is there (a message id, an approval id) or the reason it waits.",
    ],
    "docs": [
        "Every point of the definition of done is covered in the result (name the missing one when "
        "returning).",
        "Facts and numbers name their source (a goal metric, a chunk id, a link); none contradicts what "
        "is below.",
    ],
    "routine": [
        "The summary says what was checked and what it showed (not just 'OK').",
        "Every finding has its own task (T-…) or is explicitly 'nothing to act on'.",
    ],
    "plan": [
        "Each step ends in a shipped outcome (sent, published, deployed, decided) with an owner, a date "
        "and a number to watch.",
        "Money and commitments are explicit and within the budget; open decisions name a recommendation.",
        "Every point of the definition of done is covered.",
    ],
    "general": [
        "Every point of the definition of done is covered by the result.",
        "The result carries evidence (the verification line, a link, a sha, an id) for what it claims done.",
    ],
}


def kind_of(conn: sqlite3.Connection, row, note: str | None) -> str:
    """The checklist a result gets: code | outbound | plan | routine | docs | general."""
    if review_policy.is_code(conn, row, note):
        return "code"
    if review_policy.sent_outside(conn, row["id"]) or (row["source"] or "").startswith(review_policy.HIGH_SOURCES):
        return "outbound"
    if review_policy.is_plan(row):
        return "plan"
    if review_policy.is_routine(conn, row) or review_policy.is_triage(row):
        return "routine"
    if (row["topic"] or "").lower() in review_policy.DOC_TOPICS or review_policy.DOC_TITLE_RE.search(row["title"] or ""):
        return "docs"
    return "general"


def batchable(conn: sqlite3.Connection, row, note: str | None, kind: str | None = None) -> bool:
    """A routine or document result with nothing high risk about it: reviewed at low effort, in a batch."""
    kind = kind or kind_of(conn, row, note)
    return (kind in BATCH_KINDS and not review_policy.is_high(conn, row, note)
            and not review_policy.DECISION_RE.search(note or ""))


# ------------------------------------------------------------------ evidence

SHA_RE = re.compile(r"(?<![0-9a-f])([0-9a-f]{7,40})(?![0-9a-f])")
SHA_CONTEXT_RE = re.compile(r"commit|sha|merge|push|agent/dev|main|\.\.", re.IGNORECASE)


def commits_named(note: str | None) -> list[str]:
    """Commit shas the note names (a hex run with a digit and a letter, near a git word)."""
    out = []
    for line in (note or "").splitlines():
        if not SHA_CONTEXT_RE.search(line):
            continue
        for sha in SHA_RE.findall(line):
            if re.search(r"\d", sha) and re.search(r"[a-f]", sha):
                out.append(sha)
    return list(dict.fromkeys(out))[:5]


def review_repos() -> list[Path]:
    """Repositories readable here for a diff stat: POS_REVIEW_REPOS (comma-separated), else this checkout."""
    raw = os.environ.get("POS_REVIEW_REPOS", "")
    paths = [Path(p.strip()) for p in raw.split(",") if p.strip()]
    if not paths:
        from .tools import repo_root

        root = repo_root()
        paths = [root] if root else []
    from .evidence import with_submodules

    # with the submodules: a Kniha web commit (/agent-work/kniha/web) gets its diff stat too
    return [p for p in with_submodules(paths) if (p / ".git").exists()]


def diff_stat(sha: str, repos: list[Path] | None = None) -> str | None:
    """`git show --stat` of a commit in the first repository that has it; None when none does."""
    for repo in repos if repos is not None else review_repos():
        try:
            p = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "show", "--stat", "--format=%h %s (%an)", sha],
                               capture_output=True, text=True, encoding="utf-8", timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        if p.returncode == 0 and p.stdout.strip():
            lines = p.stdout.strip().splitlines()
            more = len(lines) - STAT_LINES
            return "\n".join(lines[:STAT_LINES] + ([f"… {more} more lines"] if more > 0 else []))
    return None


def _sent(conn: sqlite3.Connection, task_id: int) -> list[str]:
    out = []
    for a in conn.execute("SELECT id, action, status, details FROM approvals WHERE task_id = ? ORDER BY id DESC LIMIT 3",
                          (task_id,)):
        try:
            d = json.loads(a["details"] or "{}")
        except ValueError:
            d = {}
        what = d.get("summary") or d.get("subject") or d.get("to") or ""
        out.append(f"approval #{a['id']} {a['action']} ({a['status']}){': ' + str(what)[:160] if what else ''}")
    for r in conn.execute("SELECT action, detail FROM audit_log WHERE entity = 'task' AND entity_id = ? "
                          "AND action LIKE 'outbound:%' ORDER BY id DESC LIMIT 3", (task_id,)):
        try:
            d = json.loads(r["detail"] or "{}")
        except ValueError:
            d = {}
        line = d.get("line") or d.get("summary") or d.get("to") or ""
        out.append(f"{r['action']}{': ' + str(line)[:200] if line else ''}")
    return out


def _comments(conn: sqlite3.Connection, task_id: int) -> list[str]:
    rows = conn.execute("""SELECT c.body, c.kind, a.name FROM task_comments c LEFT JOIN actors a ON a.id = c.author_id
                           WHERE c.task_id = ? AND c.archived_at IS NULL AND c.kind IN ('comment', 'return', 'review')
                           ORDER BY c.id DESC LIMIT ?""", (task_id, COMMENTS)).fetchall()
    return [f"{r['name'] or '?'} ({r['kind']}): {' '.join((r['body'] or '').split())[:300]}" for r in reversed(rows)]


def _takeaway(conn: sqlite3.Connection, task_id: int) -> str:
    try:
        r = conn.execute("SELECT report FROM task_reports WHERE task_id = ? AND viewer_id = 0", (task_id,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    if r is None:
        return ""
    try:
        rep = json.loads(r["report"] or "{}")
    except ValueError:
        return ""
    lines = [str(rep.get("takeaway") or "").strip()]
    for d in (rep.get("decisions") or [])[:3]:
        lines.append(f"- decision asked: {d.get('question', '')}")
    return "\n".join(x for x in lines if x)


# ------------------------------------------------------------------ the packet

def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rstrip() + f"\n… ({len(text) - n} more characters: get_task)"


def section(conn: sqlite3.Connection, row, note_max: int = NOTE_MAX, repos: list[Path] | None = None) -> dict:
    """One result's packet: {'text', 'kind', 'risk'}."""
    from . import actors, tasks

    note = row["progress_note"] or ""
    ref = tasks.display_id(row["id"])
    kind = kind_of(conn, row, note)
    d = review_policy.decide(conn, row, note)
    who = actors.get(conn, row["assignee_id"])["name"] if row["assignee_id"] else "nobody"
    risk = d.risk + (f" ({d.reason})" if d.reason else "")
    parts = [f"## {ref} {row['title']}",
             f"Assignee: {who}. Kind: {kind}. Risk: {risk}."
             + (f" Returned before: {row['returned_count']}×." if row["returned_count"] else "")]
    parts.append("**Definition of done:** " + (row["definition_of_done"] or "(none set: judge by the title and notes)"))
    parts.append("**Result (hand-in note):**\n" + (_clip(note, note_max) or "(empty)"))
    take = _takeaway(conn, row["id"])
    if take:
        parts.append("**Report for the owner:**\n" + take)
    ev = []
    m = verification.LINE_RE.search(note)
    if m:
        line = note[m.start():].strip().splitlines()[0]
        ev.append(f"verification line: {line[:300]}")
    else:
        ev.append("verification line: MISSING")
    if kind == "code" or commits_named(note):
        for sha in commits_named(note):
            stat = diff_stat(sha, repos)
            ev.append(f"commit {sha}:\n```\n{stat}\n```" if stat else
                      f"commit {sha} (no repository here: `git show --stat {sha}` in your workdir if you have one)")
        if not commits_named(note):
            ev.append("commits: NONE named")
    ev += [f"sent/approval: {x}" for x in _sent(conn, row["id"])]
    parts.append("**Evidence:**\n" + "\n".join(f"- {x}" for x in ev))
    comments = _comments(conn, row["id"])
    if comments:
        parts.append("**Last comments:**\n" + "\n".join(f"- {c}" for c in comments))
    parts.append("**Checklist (" + kind + "):**\n" + "\n".join(f"{i}. {c}" for i, c in enumerate(CHECKLISTS[kind], 1)))
    return {"text": "\n\n".join(parts), "kind": kind, "risk": d.risk, "batchable": batchable(conn, row, note, kind)}


HOW = ("Decide from this packet. Open more (get_task, the report, git show) only when a checklist item "
       "cannot be judged from it. Verdict per result: review_task(task_id, verdict='accept'|'changes', "
       "comment=<Czech, one or two sentences: what is fine, or exactly what must change>). A missing "
       "verification line or evidence for a 'done' claim is a 'changes' with 'doložte …'.")


def build(conn: sqlite3.Connection, item, batch: list | None = None, repos: list[Path] | None = None) -> dict | None:
    """The packet for review work item `item` (and the other items in `batch`): {'notes',
    'definition_of_done', 'size', 'kind', 'batch'}; None when `item` is not a review item."""
    from . import review_work, tasks

    tid = review_work.reviewed_id(item)
    if tid is None:
        return None
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
    if row is None:
        return None
    others = []
    for it in batch or []:
        oid = review_work.reviewed_id(it)
        orow = conn.execute("SELECT * FROM tasks WHERE id = ?", (oid,)).fetchone() if oid else None
        if orow is not None and oid != tid:
            others.append((it, orow))
    note_max = NOTE_MAX_BATCHED if others else NOTE_MAX
    first = section(conn, row, note_max, repos)
    secs = [first] + [section(conn, orow, note_max, repos) for _, orow in others]
    item_ref = tasks.display_id(item["id"])
    refs = [tasks.display_id(tid)] + [tasks.display_id(o["id"]) for _, o in others]
    if others:
        head = (f"# Review batch: {len(secs)} results ({first['kind']}) waiting for your review\n"
                f"Review each one ({', '.join(refs)}). Reviewing a result closes its own review item. Then "
                f"complete this item ({item_ref}) with one line per result (accepted / returned and why).")
    else:
        head = (f"# Review packet: {refs[0]}\nReview this result, then complete this item ({item_ref}) with "
                "one line (accepted / returned and why).")
    notes = "\n\n".join([head, HOW] + [s["text"] for s in secs])
    small = all(s["batchable"] for s in secs)
    return {"notes": notes, "definition_of_done": f"{', '.join(refs)} accepted or returned with a reason.",
            "size": "S" if small else None, "kind": first["kind"],
            "batch": [o["id"] for o, _ in others]}


def batch_for(conn: sqlite3.Connection, item, live_sql: str = "", live_args: tuple = ()) -> list:
    """Other open review items of the same reviewer whose results are batchable like `item`'s
    (routine or docs, not high risk), oldest first, up to BATCH_MAX - 1. `live_sql`: a condition
    excluding items another worker is on."""
    from . import review_work

    tid = review_work.reviewed_id(item)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone() if tid else None
    if row is None or not batchable(conn, row, row["progress_note"]):
        return []
    kind = kind_of(conn, row, row["progress_note"])
    out = []
    for it in conn.execute(f"""SELECT * FROM tasks WHERE assignee_id = ? AND source LIKE 'review:%' AND id != ?
                               AND archived_at IS NULL AND status = 'next' {('AND NOT ' + live_sql) if live_sql else ''}
                               ORDER BY id""", (item["assignee_id"], item["id"], *live_args)).fetchall():
        oid = review_work.reviewed_id(it)
        orow = conn.execute("SELECT * FROM tasks WHERE id = ?", (oid,)).fetchone() if oid else None
        if orow is None or orow["status"] != "review" or orow["archived_at"]:
            continue
        if kind_of(conn, orow, orow["progress_note"]) == kind and batchable(conn, orow, orow["progress_note"], kind):
            out.append(it)
        if len(out) >= BATCH_MAX - 1:
            break
    return out


def serve(conn: sqlite3.Connection, item, task: dict, live_sql: str = "", live_args: tuple = ()) -> dict:
    """`task` (the served dict of review item `item`) with the packet as its notes and the run's size.
    Fail-open: any error serves the item as it is."""
    try:
        pk = build(conn, item, batch_for(conn, item, live_sql, live_args))
    except Exception:  # noqa: BLE001 - the item's own notes still say what to do
        import logging

        logging.getLogger(__name__).exception("review packet for T-%s", item["id"])
        return task
    if pk is None:
        return task
    out = {**task, "notes": pk["notes"], "definition_of_done": pk["definition_of_done"],
           "review_packet": {"kind": pk["kind"], "batch": pk["batch"]}}
    if pk["size"]:
        out["run_size"] = pk["size"]  # the worker runs it at low effort with the small cap
    return out


# ------------------------------------------------------------------ the estimate

def _usage(conn: sqlite3.Connection, task_id: int) -> dict:
    r = conn.execute("""SELECT COALESCE(SUM(e.cost_usd), 0) cost, COALESCE(SUM(e.cache_creation_tokens), 0) cw,
                               COALESCE(SUM(e.cache_read_tokens), 0) cr, COALESCE(SUM(e.output_tokens), 0) ot
                        FROM engine_usage e WHERE e.task_id = ?""", (task_id,)).fetchone()
    t = conn.execute("SELECT COALESCE(SUM(tool_calls), 0) FROM runs WHERE task_id = ?", (task_id,)).fetchone()[0]
    return {"cost": r["cost"], "cw": r["cw"], "cr": r["cr"], "ot": r["ot"], "calls": t}


# Factors for the dry estimate, from the live review-packet runs (pos.evals scenario review_packet,
# 2026-10-04) against the prod runs of the same day: tool calls per packet run, the share of output
# tokens at low effort, the share of a batch's fixed cost each batched result carries.
PACKET_CALLS = 3          # ToolSearch + review_task + complete_task (prod: 6.3 per review)
LOW_EFFORT_OUTPUT = 0.6   # output tokens at low vs medium effort on the same packet
PACKET_FIRST_WRITE = 1.0  # the packet replaces the get_task/report reads: the first write stays ~ the same


def estimate(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """Review items run between `since` and `until` (dates): what they cost, and a dry estimate of the
    same reviews with the packet, low effort for routine packets, batching and the CEO's offload."""
    from datetime import datetime, timedelta

    from . import business, review_work

    end = (datetime.fromisoformat(until) + timedelta(days=1)).date().isoformat()
    items = conn.execute("""SELECT t.* FROM tasks t WHERE t.source LIKE 'review:%' AND EXISTS
                            (SELECT 1 FROM runs r WHERE r.task_id = t.id AND r.started_at >= ? AND r.started_at < ?)
                            ORDER BY t.id""", (since, end)).fetchall()
    ceo = business.ceo_id(conn)
    before = {"items": 0, "cost": 0.0, "ceo_items": 0, "ceo_cost": 0.0, "calls": 0}
    after = {"runs": 0, "cost": 0.0, "ceo_items": 0, "ceo_cost": 0.0, "batched": 0}
    by_kind: dict[str, int] = {}
    batches: dict[tuple, int] = {}
    for it in items:
        u = _usage(conn, it["id"])
        before["items"] += 1
        before["cost"] += u["cost"]
        before["calls"] += u["calls"]
        if it["assignee_id"] == ceo:
            before["ceo_items"] += 1
            before["ceo_cost"] += u["cost"]
        tid = review_work.reviewed_id(it)
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is None or not u["cost"]:
            continue
        note = row["progress_note"]
        kind = kind_of(conn, row, note)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        calls = max(u["calls"], 1)
        per_call_read = u["cr"] / (calls + 1)  # each call (and the final answer) re-reads the context
        cost_per = u["cost"] / max(u["cw"] * 7.9e-6 + u["cr"] * 0.27e-6 + u["ot"] * 21.9e-6, 1e-9)
        cr = per_call_read * (min(calls, PACKET_CALLS) + 1)
        ot = u["ot"] * (LOW_EFFORT_OUTPUT if batchable(conn, row, note, kind) else 1.0) \
            * min(1.0, (PACKET_CALLS + 1) / (calls + 1) + 0.25)
        cw = u["cw"] * PACKET_FIRST_WRITE * min(1.0, (PACKET_CALLS + 1) / (calls + 1) + 0.3)
        cost = (cw * 7.9e-6 + cr * 0.27e-6 + ot * 21.9e-6) * cost_per
        reviewer = it["assignee_id"]
        if reviewer == ceo and not review_policy.needs_ceo(conn, row, note) and not business.owner_request(conn, row):
            reviewer = review_policy.ceo_offload(conn, row) or ceo
        if batchable(conn, row, note, kind):
            key = (reviewer, kind, (it["created_at"] or "")[:13])  # the same reviewer, kind and hour
            n = batches.get(key, 0)
            batches[key] = n + 1
            if n % BATCH_MAX:  # one run per BATCH_MAX results: the others add their section only
                cost *= 0.35
                after["batched"] += 1
            else:
                after["runs"] += 1
        else:
            after["runs"] += 1
        after["cost"] += cost
        if reviewer == ceo:
            after["ceo_items"] += 1
            after["ceo_cost"] += cost
    r = {k: round(v, 2) if isinstance(v, float) else v for k, v in before.items()}
    a = {k: round(v, 2) if isinstance(v, float) else v for k, v in after.items()}
    return {"since": since, "until": until, "before": r, "after_estimate": a, "kinds": by_kind,
            "per_review_before": round(before["cost"] / max(before["items"], 1), 3),
            "per_review_after": round(after["cost"] / max(before["items"], 1), 3)}


def main(argv: list[str] | None = None) -> None:
    from .config import get_settings
    from .db import connect

    p = argparse.ArgumentParser(description="The review packet of a review item, or the cost estimate.")
    p.add_argument("--db", default=None)
    p.add_argument("--estimate", nargs=2, metavar=("SINCE", "UNTIL"))
    p.add_argument("--item", help="print the packet a review item would be served with (T-123)")
    a = p.parse_args(argv)
    conn = connect(Path(a.db) if a.db else get_settings().db_path)
    try:
        if a.item:
            from . import tasks

            item = conn.execute("SELECT * FROM tasks WHERE id = ?", (tasks.parse_id(a.item),)).fetchone()
            pk = build(conn, item, batch_for(conn, item)) if item else None
            print(pk["notes"] if pk else "not a review item")
        else:
            print(json.dumps(estimate(conn, *a.estimate), ensure_ascii=False, indent=1))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
