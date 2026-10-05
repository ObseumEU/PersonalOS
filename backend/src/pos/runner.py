"""Runs `codex exec` for an actor and records the run.

Other modules plug in through hooks instead of editing this file:

- `before_run(fn)`: fn(conn, request) may raise `RunBlocked` to stop a run
  (the kill switch, the token budget, the constitution checks);
- `after_run(fn)`: fn(conn, run_row, jsonl) sees every finished run with the
  raw `codex exec --json` output (the budget keeper, the HR agent).

Every run gets a row in `runs`. Changes made with `Ctx(run_id=...)` are tied
to it, so `versioning.rollback_run` can undo a whole run.
"""

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import audit
from .core import Ctx, now_iso


class RunBlocked(Exception):
    """Raised by a before_run hook to refuse a run (e.g. the kill switch is on)."""


@dataclass
class RunRequest:
    actor_id: int
    kind: str  # e.g. "suggest", "task"
    prompt: str
    task_id: int | None = None
    output_schema: dict | None = None
    timeout_s: int = 180
    sandbox: str = "read-only"
    extra_args: list[str] = field(default_factory=list)
    engine: str = "codex"  # codex | claude (outside workers may run either)
    model: str | None = None
    # Claude only: a lean system prompt instead of Claude Code's own (a much faster first token for
    # a tool-less call), and the effort level (low for editing, not reasoning).
    system_prompt: str | None = None
    effort: str | None = None


@dataclass
class RunResult:
    run_id: int
    status: str  # ok | error | blocked | cancelled
    output: str = ""
    data: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""


_before: list[Callable[[sqlite3.Connection, RunRequest], None]] = []
_after: list[Callable[[sqlite3.Connection, sqlite3.Row, str], None]] = []


def before_run(fn):
    _before.append(fn)
    return fn


def after_run(fn):
    _after.append(fn)
    return fn


def codex_bin() -> str | None:
    return os.environ.get("POS_CODEX_BIN") or shutil.which("codex")


def claude_bin() -> str | None:
    name = os.environ.get("POS_CLAUDE_BIN") or shutil.which("claude")
    if name and sys.platform == "win32" and name.lower().endswith(".cmd"):
        import re

        # npm's claude.cmd only starts a native exe; call it directly so
        # arguments like --json-schema keep their quotes.
        text = Path(name).read_text(encoding="utf-8", errors="replace")
        if m := re.search(r'"%dp0%\\([^"]+\.exe)"', text):
            exe = Path(name).parent / m[1]
            if exe.exists():
                return str(exe)
    return name


def available(engine: str = "codex") -> bool:
    if engine == "claude":
        return claude_bin() is not None and os.environ.get("POS_CLAUDE_DISABLED", "") != "1"
    return codex_bin() is not None and os.environ.get("POS_CODEX_DISABLED", "") != "1"


def _claude_args(req: "RunRequest", binary: str) -> list[str]:
    """A tool-less, isolated Claude call: no tools, no MCP, no user settings."""
    args = [binary, "-p", "--output-format", "stream-json", "--verbose", "--tools", "",
            "--strict-mcp-config", "--no-session-persistence", "--restricted"]
    if req.model:
        args += ["--model", req.model]
    if req.system_prompt:
        args += ["--system-prompt", req.system_prompt]
    if req.effort:
        args += ["--effort", req.effort]
    if req.output_schema:
        args += ["--json-schema", json.dumps(req.output_schema)]
    return args


def _claude_output(jsonl: str) -> tuple[str, dict | None, str]:
    """(text, structured output, error) from the stream-json result event."""
    for line in reversed(jsonl.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            if ev.get("is_error"):
                return "", None, str(ev.get("result") or ev.get("subtype") or "failed")
            return str(ev.get("result") or ""), ev.get("structured_output"), ""
    return "", None, "no result from claude"


def _usage(jsonl: str) -> tuple[int | None, int | None]:
    """Sum token usage from `codex exec --json` events."""
    tin = tout = None
    for line in jsonl.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        usage = ev.get("usage") or (ev.get("msg") or {}).get("usage") or {}
        if "input_tokens" in usage:
            tin = (tin or 0) + int(usage.get("input_tokens") or 0)
            tout = (tout or 0) + int(usage.get("output_tokens") or 0)
    return tin, tout


def _new_group() -> dict:
    """Start codex in its own process group so the whole tree can be stopped."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _status(conn, run_id: int) -> str:
    return conn.execute("SELECT status FROM runs WHERE id = ?", (run_id,)).fetchone()["status"]


def cancel(conn: sqlite3.Connection, run_id: int, reason: str) -> bool:
    """Stop a running run (kill switch, owner's Stop button). Nothing is deleted."""
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["status"] != "running":
        return False
    if row["pid"]:
        kill_process_tree(row["pid"])
    conn.execute("UPDATE runs SET status = 'cancelled', ended_at = ?, detail = ? WHERE id = ?",
                 (now_iso(), reason[:4000], run_id))
    return True


def cancel_all(conn: sqlite3.Connection, reason: str, actor_id: int | None = None) -> list[int]:
    sql = "SELECT id FROM runs WHERE status = 'running'" + (" AND actor_id = ?" if actor_id else "")
    ids = [r["id"] for r in conn.execute(sql, (actor_id,) if actor_id else ())]
    return [i for i in ids if cancel(conn, i, reason)]


def reported_model(jsonl: str) -> str | None:
    """The model a CLI says it used: Claude's `system/init` event, or any
    top-level `model` field in Codex's JSON events."""
    for line in jsonl.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            for m in (ev.get("model"), (ev.get("payload") or {}).get("model") if isinstance(ev.get("payload"), dict) else None):
                if isinstance(m, str) and m:
                    return m
    return None


def work_counts(jsonl: str) -> tuple[int | None, int | None]:
    """(tool calls, model turns) of a run, from Claude's stream-json or Codex's
    JSON events; (None, None) when the output says nothing."""
    tools = turns = 0
    claude_turns = None
    seen = False
    for line in jsonl.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "assistant" and isinstance(ev.get("message"), dict):  # Claude
            seen = True
            turns += 1
            tools += sum(1 for c in ev["message"].get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use")
        elif kind == "result" and ev.get("num_turns") is not None:  # Claude's own count
            claude_turns = int(ev["num_turns"])
        elif kind == "turn.completed":  # Codex
            seen = True
            turns += 1
        elif kind == "item.completed" and isinstance(ev.get("item"), dict):
            seen = True
            if ev["item"].get("type") in ("command_execution", "mcp_tool_call", "file_change", "web_search"):
                tools += 1
    if not seen and claude_turns is None:
        return None, None
    return tools, claude_turns if claude_turns is not None else turns


def _model_for(req: "RunRequest") -> str | None:
    from . import engines

    return req.model or engines.default_model(req.engine)


def _finish(conn, run_id: int, status: str, tin=None, tout=None, detail: str = "", jsonl: str = "") -> sqlite3.Row:
    conn.execute(
        "UPDATE runs SET status = ?, ended_at = ?, input_tokens = ?, output_tokens = ?, detail = ?, "
        "model = COALESCE(?, model), tool_calls = ?, turns = ? WHERE id = ?",
        (status, now_iso(), tin, tout, detail[:4000], reported_model(jsonl) if jsonl else None,
         *work_counts(jsonl or ""), run_id),
    )
    if jsonl:  # the CLI session's pointer and a compact summary of the tool calls (pos.transcripts)
        try:
            from . import transcripts

            transcripts.record(conn, run_id, jsonl)
        except Exception:  # noqa: BLE001 - a summary never fails the run's record
            import logging

            logging.getLogger("pos.runner").exception("run %s: transcript summary failed", run_id)
    conn.commit()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    for fn in _after:
        fn(conn, row, jsonl)
    return row


def run(conn: sqlite3.Connection, req: RunRequest) -> RunResult:
    cur = conn.execute(
        "INSERT INTO runs (actor_id, task_id, kind, status, started_at, engine, model) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (req.actor_id, req.task_id, req.kind, now_iso(), req.engine, _model_for(req)),
    )
    run_id = cur.lastrowid
    ctx = Ctx(actor_id=req.actor_id, via="runner", run_id=run_id)
    audit.log(conn, ctx, "run_start", "task" if req.task_id else None, req.task_id, kind=req.kind)
    conn.commit()

    try:
        for fn in _before:
            fn(conn, req)
    except RunBlocked as e:
        _finish(conn, run_id, "blocked", detail=str(e))
        return RunResult(run_id, "blocked", error=str(e))

    if req.engine == "claude":
        return _run_claude(conn, req, run_id)
    binary = codex_bin()
    if not available() or binary is None:
        _finish(conn, run_id, "error", detail="codex CLI not available")
        return RunResult(run_id, "error", error="codex CLI not available")

    with tempfile.TemporaryDirectory(prefix="pos-run-") as tmp:
        last = Path(tmp) / "last.txt"
        args = [binary, "exec", "--json", "--skip-git-repo-check", "--ephemeral",
                "-s", req.sandbox, "-C", tmp, "-o", str(last), *req.extra_args]
        if req.output_schema:
            schema = Path(tmp) / "schema.json"
            schema.write_text(json.dumps(req.output_schema), encoding="utf-8")
            args += ["--output-schema", str(schema)]
        args.append("-")  # prompt from stdin
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", **_new_group())
        conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (proc.pid, run_id))
        conn.commit()
        try:
            stdout, stderr = proc.communicate(req.prompt, timeout=req.timeout_s)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            proc.communicate()
            _finish(conn, run_id, "error", detail=f"timeout after {req.timeout_s}s")
            return RunResult(run_id, "error", error="timeout")
        if _status(conn, run_id) == "cancelled":  # stopped by the kill switch or the owner
            return RunResult(run_id, "cancelled", error="cancelled")
        proc = subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
        tin, tout = _usage(proc.stdout)
        jsonl = proc.stdout
        output = last.read_text(encoding="utf-8").strip() if last.exists() else ""
        if proc.returncode != 0 or not output:
            err = (proc.stderr or proc.stdout)[-2000:]
            _finish(conn, run_id, "error", tin, tout, err, proc.stdout)
            return RunResult(run_id, "error", output, None, tin, tout, err)
    data = None
    if req.output_schema:
        try:
            data = json.loads(output)
        except ValueError:
            _finish(conn, run_id, "error", tin, tout, "output was not JSON", jsonl)
            return RunResult(run_id, "error", output, None, tin, tout, "output was not JSON")
    _finish(conn, run_id, "ok", tin, tout, jsonl=jsonl)
    return RunResult(run_id, "ok", output, data, tin, tout)


def _run_claude(conn: sqlite3.Connection, req: "RunRequest", run_id: int) -> "RunResult":
    binary = claude_bin()
    if not available("claude") or binary is None:
        _finish(conn, run_id, "error", detail="claude CLI not available")
        return RunResult(run_id, "error", error="claude CLI not available")
    with tempfile.TemporaryDirectory(prefix="pos-run-") as tmp:
        proc = subprocess.Popen(_claude_args(req, binary), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=tmp, **_new_group())
        conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (proc.pid, run_id))
        conn.commit()
        try:
            stdout, stderr = proc.communicate(req.prompt, timeout=req.timeout_s)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc.pid)
            proc.communicate()
            _finish(conn, run_id, "error", detail=f"timeout after {req.timeout_s}s")
            return RunResult(run_id, "error", error="timeout")
    if _status(conn, run_id) == "cancelled":
        return RunResult(run_id, "cancelled", error="cancelled")
    text, data, err = _claude_output(stdout)
    if err or proc.returncode != 0:
        err = err or (stderr or "")[-2000:] or f"exit {proc.returncode}"
        _finish(conn, run_id, "error", detail=err, jsonl=stdout)
        return RunResult(run_id, "error", text, None, error=err)
    if req.output_schema and data is None:
        try:
            data = json.loads(text)
        except ValueError:
            _finish(conn, run_id, "error", detail="output was not JSON", jsonl=stdout)
            return RunResult(run_id, "error", text, None, error="output was not JSON")
    row = _finish(conn, run_id, "ok", jsonl=stdout)
    return RunResult(run_id, "ok", text, data, row["input_tokens"], row["output_tokens"])


def list_runs(conn: sqlite3.Connection, limit: int = 50, actor_id: int | None = None) -> list[dict]:
    """Recent runs; with `actor_id`, only runs on tasks that member may read."""
    where, params = "", []
    if actor_id is not None:
        from .visibility import visible_sql

        cond, params = visible_sql("task", actor_id, "t")
        where = f"WHERE r.task_id IS NULL OR EXISTS (SELECT 1 FROM tasks t WHERE t.id = r.task_id AND {cond})"
    rows = conn.execute(
        f"""SELECT r.*, a.name AS actor_name FROM runs r JOIN actors a ON a.id = r.actor_id {where}
           ORDER BY r.id DESC LIMIT ?""", (*params, limit)
    ).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ runs done by outside workers
# A Codex worker (worker/, one per agent) runs codex itself. It asks here to
# start a run, so the same gates apply (kill switch, pause, budget,
# constitution), and reports the finished run with its raw --json output, so
# the same after-run hooks record token usage.

def start_external(conn: sqlite3.Connection, req: RunRequest) -> RunResult:
    cur = conn.execute(
        "INSERT INTO runs (actor_id, task_id, kind, status, started_at, engine, model) "
        "VALUES (?, ?, ?, 'running', ?, ?, ?)",
        (req.actor_id, req.task_id, req.kind, now_iso(), req.engine, _model_for(req)),
    )
    run_id = cur.lastrowid
    audit.log(conn, Ctx(req.actor_id, via="worker", run_id=run_id), "run_start",
              "task" if req.task_id else None, req.task_id, kind=req.kind, worker=True, engine=req.engine)
    conn.commit()
    try:
        for fn in _before:
            fn(conn, req)
    except RunBlocked as e:
        _finish(conn, run_id, "blocked", detail=str(e))
        return RunResult(run_id, "blocked", error=str(e))
    return RunResult(run_id, "running")


def finish_external(conn: sqlite3.Connection, run_id: int, actor_id: int, status: str, jsonl: str = "",
                    detail: str = "") -> sqlite3.Row:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None or row["actor_id"] != actor_id:
        raise ValueError(f"run {run_id} is not yours")
    if row["status"] != "running":  # cancelled by the kill switch or the owner meanwhile
        return row
    if status not in ("ok", "error", "cancelled"):
        raise ValueError("status must be ok, error or cancelled")
    tin, tout = _usage(jsonl)
    return _finish(conn, run_id, status, tin, tout, detail, jsonl)
