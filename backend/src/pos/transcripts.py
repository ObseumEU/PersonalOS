"""What a run did, kept after it: a pointer to the CLI's own session and a compact transcript summary.

The raw transcripts live only in the CLIs' session files (Claude: <config dir>/projects/<cwd>/<session>.jsonl
in the agent pool; Codex: its sessions folder by thread id), and `runs` kept only the usage and a detail
line, so an audit of what agents tried and failed (2026-10) had to dig. Every finished run with output now
gets one row in `run_transcripts` (runner._finish):

- the pointer: the engine, the CLI session id (Claude's session_id, Codex's thread_id), the working
  folder and the session file's path under the CLI's config folder;
- the summary: every tool call (name, a short redacted input, error yes/no; for an error the start of its
  result), the run's own error and its counts; at most MAX_CALLS calls and MAX_CHARS characters (errors
  are kept first when it is cut), redacted like log lines (pos.observability.redact: tokens, keys, e-mails).

Read with `run_transcript` (the agent itself, its lead, HR, the Performance Coach, the CEO, the owner).
Rows older than KEEP_DAYS are dropped. The table is created on first use (no numbered migration).
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from . import actors
from .core import Ctx, Forbidden, NotFound, now_iso

MAX_CALLS = 150
MAX_CHARS = 12_000
INPUT_CHARS = 160
ERROR_CHARS = 300
KEEP_DAYS = 90
READERS = ("Performance Coach", "Head of People", "CEO")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS run_transcripts (
        run_id INTEGER PRIMARY KEY REFERENCES runs(id), engine TEXT, session_id TEXT, cwd TEXT,
        session_path TEXT, tool_calls INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS run_transcripts_created ON run_transcripts (created_at)")


def _short(value, limit: int) -> str:
    from .observability import redact

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return redact(" ".join(text.split()), limit)


def _result_text(content) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
    return str(content or "")


def _claude_path(cwd: str, session_id: str) -> str:
    """Where Claude Code keeps the session: projects/<cwd with every non-alphanumeric as '-'>/<id>.jsonl."""
    return f"~/.claude/projects/{re.sub(r'[^A-Za-z0-9]', '-', cwd)}/{session_id}.jsonl" if cwd else ""


def summarize(jsonl: str) -> dict:
    """{engine, session_id, cwd, session_path, calls: [...], error, counts} from Claude's stream-json or
    Codex's --json events (an older or unknown format: empty calls)."""
    calls: dict[str, dict] = {}
    order: list[dict] = []
    out = {"engine": None, "session_id": None, "cwd": None, "session_path": None, "error": None,
           "num_turns": None, "cost_usd": None}
    for line in (jsonl or "").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if ev.get("session_id") and not out["session_id"]:
            out.update(engine="claude", session_id=str(ev["session_id"]))
        if kind == "system" and ev.get("subtype") == "init":
            out["cwd"] = ev.get("cwd") or out["cwd"]
        elif kind == "thread.started" and ev.get("thread_id"):  # Codex
            out.update(engine="codex", session_id=str(ev["thread_id"]))
        elif kind == "assistant" and isinstance(ev.get("message"), dict):
            for c in ev["message"].get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    call = {"tool": str(c.get("name") or "?"), "input": _short(c.get("input") or {}, INPUT_CHARS),
                            "error": False}
                    calls[str(c.get("id"))] = call
                    order.append(call)
        elif kind == "user" and isinstance(ev.get("message"), dict):
            for c in ev["message"].get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_result" and str(c.get("tool_use_id")) in calls:
                    call = calls[str(c.get("tool_use_id"))]
                    text = _result_text(c.get("content"))
                    if c.get("is_error") or text.lstrip().lower().startswith(("error", "<tool_use_error>")):
                        call["error"] = True
                        call["result"] = _short(text, ERROR_CHARS)
        elif kind == "result":
            out["num_turns"] = ev.get("num_turns")
            out["cost_usd"] = ev.get("total_cost_usd")
            if ev.get("is_error"):
                out["error"] = _short(ev.get("result") or ev.get("subtype") or "failed", ERROR_CHARS)
        elif kind == "item.completed" and isinstance(ev.get("item"), dict):  # Codex
            item = ev["item"]
            t = item.get("type")
            if t == "command_execution":
                call = {"tool": "shell", "input": _short(item.get("command") or "", INPUT_CHARS),
                        "error": item.get("status") == "failed" or (item.get("exit_code") not in (None, 0))}
                if call["error"]:
                    call["result"] = _short(item.get("aggregated_output") or "", ERROR_CHARS)
                order.append(call)
            elif t == "mcp_tool_call":
                call = {"tool": f"{item.get('server') or 'mcp'}:{item.get('tool') or '?'}",
                        "input": _short(item.get("arguments") or {}, INPUT_CHARS),
                        "error": item.get("status") == "failed" or bool(item.get("error"))}
                if call["error"]:
                    call["result"] = _short(item.get("error") or item.get("result") or "", ERROR_CHARS)
                order.append(call)
            elif t in ("file_change", "web_search"):
                order.append({"tool": t, "input": _short(item.get("changes") or item.get("query") or "", INPUT_CHARS),
                              "error": item.get("status") == "failed"})
        elif kind in ("turn.failed", "error"):  # Codex
            out["error"] = _short((ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict)
                                  else ev.get("message") or ev.get("error") or "failed", ERROR_CHARS)
    if out["engine"] == "claude" and out["session_id"]:
        out["session_path"] = _claude_path(out["cwd"] or "", out["session_id"])
    elif out["engine"] == "codex" and out["session_id"]:
        out["session_path"] = f"$CODEX_HOME/sessions/**/rollout-*-{out['session_id']}.jsonl"
    errors = [c for c in order if c["error"]]
    out["counts"] = {"tool_calls": len(order), "errors": len(errors),
                     "by_tool": dict(sorted(_count(order).items(), key=lambda kv: -kv[1])[:20])}
    out["calls"] = _cap(order)
    return out


def _count(calls: list[dict]) -> dict:
    out: dict[str, int] = {}
    for c in calls:
        out[c["tool"]] = out.get(c["tool"], 0) + 1
    return out


def _cap(calls: list[dict]) -> list[dict]:
    """At most MAX_CALLS calls and MAX_CHARS characters; when cut, every error stays (as far as it fits)
    with the first and the last calls around it, in their order."""
    def size(xs):
        return len(json.dumps(xs, ensure_ascii=False))

    if len(calls) <= MAX_CALLS and size(calls) <= MAX_CHARS:
        return calls
    idx = [i for i, c in enumerate(calls) if c["error"]]
    keep = set(idx[:MAX_CALLS // 2]) | set(range(min(10, len(calls)))) | set(range(max(0, len(calls) - 10), len(calls)))
    out = [dict(c, n=i + 1) for i, c in enumerate(calls) if i in keep]
    while len(out) > 1 and (len(out) > MAX_CALLS or size(out) > MAX_CHARS):
        drop = next((k for k, c in enumerate(out) if not c["error"] and 10 <= k < len(out) - 10), None)
        out.pop(drop if drop is not None else len(out) // 2)
    return out


def record(conn: sqlite3.Connection, run_id: int, jsonl: str) -> None:
    """The run's pointer and summary (runner._finish); never fails the run."""
    if not (jsonl or "").strip():
        return
    ensure_schema(conn)
    s = summarize(jsonl)
    if not s["session_id"] and not s["calls"] and not s["error"]:
        return
    conn.execute("""INSERT OR REPLACE INTO run_transcripts (run_id, engine, session_id, cwd, session_path, tool_calls,
                    errors, summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (run_id, s["engine"], s["session_id"], s["cwd"], s["session_path"], s["counts"]["tool_calls"],
                  s["counts"]["errors"], json.dumps({k: s[k] for k in ("calls", "counts", "error", "num_turns",
                                                                        "cost_usd")}, ensure_ascii=False),
                  now_iso()))
    if run_id % 50 == 0:  # now and then: the old ones go
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM run_transcripts WHERE created_at < ?", (cutoff,))


def may_read(conn: sqlite3.Connection, viewer_id: int, actor_id: int) -> bool:
    from .org import manages

    me = actors.get(conn, viewer_id)
    return bool(me["is_owner"]) or viewer_id == actor_id or me["name"] in READERS \
        or (me["role"] or "") in ("coach", "hr", "ceo") or manages(conn, viewer_id, actor_id)


def get(conn: sqlite3.Connection, ctx: Ctx, run_id: int | None = None, task_id: int | None = None,
        failed_only: bool = False) -> dict | list[dict]:
    """One run's pointer and summary, or (task_id) every run of that task, newest first."""
    ensure_schema(conn)
    if run_id is None and task_id is None:
        raise NotFound("give run_id or task_id")
    where, arg = ("r.id = ?", run_id) if run_id is not None else ("r.task_id = ?", task_id)
    rows = conn.execute(f"""SELECT r.id, r.actor_id, r.task_id, r.status, r.started_at, r.ended_at, r.detail,
                            a.name AS actor_name, t.engine, t.session_id, t.cwd, t.session_path, t.summary
                            FROM runs r JOIN actors a ON a.id = r.actor_id
                            LEFT JOIN run_transcripts t ON t.run_id = r.id WHERE {where}
                            ORDER BY r.id DESC LIMIT 20""", (arg,)).fetchall()
    if not rows:
        raise NotFound(f"no run {run_id}" if run_id is not None else f"no runs of task {task_id}")
    out = []
    for r in rows:
        if not may_read(conn, ctx.actor_id, r["actor_id"]):
            raise Forbidden("the agent itself, its lead, HR, the Performance Coach, the CEO or the owner reads "
                            "its runs")
        s = json.loads(r["summary"] or "{}")
        calls = s.get("calls") or []
        if failed_only:
            calls = [c for c in calls if c.get("error")]
        out.append({"run_id": r["id"], "agent": r["actor_name"], "task_id": r["task_id"], "status": r["status"],
                    "started_at": r["started_at"], "ended_at": r["ended_at"], "detail": (r["detail"] or "")[:500],
                    "session": {"engine": r["engine"], "id": r["session_id"], "cwd": r["cwd"],
                                "path": r["session_path"]} if r["session_id"] else None,
                    "error": s.get("error"), "counts": s.get("counts"), "calls": calls,
                    **({} if r["summary"] else {"note": "no transcript summary (a run before 2026-10 or without output)"})})
    return out[0] if run_id is not None else out


def register_mcp(mcp, session) -> None:
    from mcp.server.mcpserver import Context

    from . import mcp_server, tasks

    mcp_server.TOOL_PERMISSIONS.setdefault("run_transcript", "tasks:read")

    @mcp.tool(description="What a run did: its tool calls (name, short input, which failed and why), its error, "
                          "and the pointer to the CLI session file. run_id, or task_id for every run of a task "
                          "(newest first); failed_only keeps only the failed calls. For the agent itself, its "
                          "lead, HR, the Performance Coach and the CEO.")
    def run_transcript(ctx: Context, run_id: int | None = None, task_id: str | None = None,
                       failed_only: bool = False) -> dict:
        with session(ctx, "run_transcript", run_id=run_id, task_id=task_id) as (conn, c):
            got = get(conn, c, run_id, tasks.parse_id(task_id) if task_id and run_id is None else None, failed_only)
            return got if isinstance(got, dict) else {"runs": got}
