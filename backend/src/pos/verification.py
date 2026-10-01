"""Self-verification before a hand-in: an agent checks its own result and says what it checked.

Before handing a result in, an agent runs a short check that fits the task (the tests for code,
re-reading the requirements and the definition of done for a document, opening the URL in the
browser for a web change) and writes one line in the result, e.g.

    Ověřeno: pytest tests/test_x.py, 12 passed
    Verified: opened https://example.cz/video, the video plays (HTTP 206)

A hand-in without such a line still goes through (a nudge, never a block): the agent gets a
platform note in the answer and in its next prompts (pos.business.nudges reads the audit line),
and the review policy (pos.review_policy) never auto-accepts it.
"""

import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import audit
from .core import Ctx

# "Ověřeno:", "Overeno -", "Verified:", "Verification:", "Kontrola:", "Self-check:", "Test:"/"Tests:",
# at the start of a line (Markdown bullets and bold allowed).
LINE_RE = re.compile(r"(?im)^[\s>*_\-#]*(?:\*\*)?(ověřen[oí]|overen[oi]|verified|verification|self[- ]check|"
                     r"kontrola|zkontrolováno|otestováno|tested|tests?|testy)(?:\*\*)?\s*[:\-–—]")
# Tests that passed: "12 passed", "tests pass", "testy prošly", "all green".
TESTS_PASS_RE = re.compile(r"\b\d+\s+passed\b|\btests?\s+(?:pass(?:ed)?|green|ok)\b|\btesty\s+(?:prošly|prosly|"
                           r"zelené|zelene|ok)\b|\ball green\b|\bvšechny testy\b", re.IGNORECASE)
TESTS_FAIL_RE = re.compile(r"\b[1-9]\d*\s+(?:failed|errors?)\b|\btests?\s+fail|\btesty\s+(?:selhaly|padaj)",
                           re.IGNORECASE)

NUDGE = ("Platform note: your hand-in has no verification line. Before handing in, check your result the way "
         "the task needs (code: run the tests; a document: re-read the requirements and the definition of done; "
         "a web change: open the URL with the browser) and write one line 'Ověřeno: <what you checked and what "
         "it showed>'. Add it now as a task comment (task_comment) if you can.")
GUIDE = ("- Before you hand in, verify your result the way the task needs: code → run the tests; a document → "
         "re-read the requirements and the definition of done; a web change → open the URL with the browser. "
         "End the result with one line 'Ověřeno: <what you checked, what it showed>'. Check the knowledge base "
         "(the `knowledge` tool, the passages in your task) before acting, and cite the chunk ids you used "
         "(`<doc>:c<n>`) in the result.")


def has_line(text: str | None) -> bool:
    return bool(LINE_RE.search(text or ""))


def tests_passed(text: str | None) -> bool:
    t = text or ""
    return bool(TESTS_PASS_RE.search(t)) and not TESTS_FAIL_RE.search(t)


def on_hand_in(conn: sqlite3.Connection, ctx: Ctx, row, note: str | None) -> str | None:
    """An agent handed a result in: without a verification line it gets the nudge (returned to
    the caller) and an audit line its next prompts read. People are never nudged."""
    who = conn.execute("SELECT kind FROM actors WHERE id = ?", (ctx.actor_id,)).fetchone()
    if who is None or who["kind"] == "human" or row["assignee_id"] != ctx.actor_id:
        return None
    if has_line(note):
        audit.log(conn, ctx, "handin_verified", "task", row["id"])
        return None
    audit.log(conn, ctx, "handin_unverified", "task", row["id"])
    return NUDGE


def nudges(conn: sqlite3.Connection, actor_id: int) -> list[str]:
    """A line for the agent's next prompts when its recent hand-ins lacked the verification line."""
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    n = conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'handin_unverified' AND actor_id = ? "
                     "AND at >= ?", (actor_id, since)).fetchone()[0]
    if not n:
        return []
    return [f"In the last 7 days {n} of your hand-ins had no verification line. Before handing in, check the "
            "result (tests for code, re-read the requirements for documents, open the URL for web changes) and "
            "end it with 'Ověřeno: …'."]
