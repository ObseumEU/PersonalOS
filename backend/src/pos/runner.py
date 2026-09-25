"""Runs `codex exec` for an actor and records the run.

Other modules plug in through hooks instead of editing this file:

- `before_run(fn)`: fn(conn, request) may raise `RunBlocked` to stop a run
  (the kill switch, the token budget, the constitution checks);
- `after_run(fn)`: fn(conn, run_row) sees every finished run, including its
  token usage (the budget keeper, the HR agent).

Every run gets a row in `runs`. Changes made with `Ctx(run_id=...)` are tied
to it, so `versioning.rollback_run` can undo a whole run.
"""

import json
import os
import shutil
import sqlite3
import subprocess
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


@dataclass
class RunResult:
    run_id: int
    status: str  # ok | error | blocked
    output: str = ""
    data: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""


_before: list[Callable[[sqlite3.Connection, RunRequest], None]] = []
_after: list[Callable[[sqlite3.Connection, sqlite3.Row], None]] = []


def before_run(fn):
    _before.append(fn)
    return fn


def after_run(fn):
    _after.append(fn)
    return fn


def codex_bin() -> str | None:
    return os.environ.get("POS_CODEX_BIN") or shutil.which("codex")


def available() -> bool:
    return codex_bin() is not None and os.environ.get("POS_CODEX_DISABLED", "") != "1"


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


def _finish(conn, run_id: int, status: str, tin=None, tout=None, detail: str = "") -> sqlite3.Row:
    conn.execute(
        "UPDATE runs SET status = ?, ended_at = ?, input_tokens = ?, output_tokens = ?, detail = ? WHERE id = ?",
        (status, now_iso(), tin, tout, detail[:4000], run_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    for fn in _after:
        fn(conn, row)
    return row


def run(conn: sqlite3.Connection, req: RunRequest) -> RunResult:
    cur = conn.execute(
        "INSERT INTO runs (actor_id, task_id, kind, status, started_at) VALUES (?, ?, ?, 'running', ?)",
        (req.actor_id, req.task_id, req.kind, now_iso()),
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
        try:
            proc = subprocess.run(args, input=req.prompt, capture_output=True, text=True,
                                  encoding="utf-8", timeout=req.timeout_s)
        except subprocess.TimeoutExpired:
            _finish(conn, run_id, "error", detail=f"timeout after {req.timeout_s}s")
            return RunResult(run_id, "error", error="timeout")
        tin, tout = _usage(proc.stdout)
        output = last.read_text(encoding="utf-8").strip() if last.exists() else ""
        if proc.returncode != 0 or not output:
            err = (proc.stderr or proc.stdout)[-2000:]
            _finish(conn, run_id, "error", tin, tout, err)
            return RunResult(run_id, "error", output, None, tin, tout, err)
    data = None
    if req.output_schema:
        try:
            data = json.loads(output)
        except ValueError:
            _finish(conn, run_id, "error", tin, tout, "output was not JSON")
            return RunResult(run_id, "error", output, None, tin, tout, "output was not JSON")
    _finish(conn, run_id, "ok", tin, tout)
    return RunResult(run_id, "ok", output, data, tin, tout)


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*, a.name AS actor_name FROM runs r JOIN actors a ON a.id = r.actor_id
           ORDER BY r.id DESC LIMIT ?""", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]
