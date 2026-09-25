"""Read token usage and subscription limits from what Codex writes.

Two sources, both produced by Codex CLI itself:

* `codex exec --json` prints JSON Lines on stdout. `thread.started` carries the
  thread id and every `turn.completed` carries `usage` (tokens of that turn).
* Every session (exec or interactive) is logged to
  `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl`. Its
  `token_count` events carry cumulative tokens *and* `rate_limits`: the
  subscription windows with `used_percent`, `window_minutes` and `resets_at`.

The parsers are lenient: unknown lines and fields are skipped, so a newer Codex
that adds fields does not break metering.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def billable(self) -> int:
        """Tokens that count against the limits: non-cached input plus output.

        Same "blended" total Codex shows; cached input is much cheaper and
        reasoning tokens are already part of output_tokens.
        """
        return max(self.input_tokens - self.cached_input_tokens, 0) + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            self.input_tokens + other.input_tokens,
            self.cached_input_tokens + other.cached_input_tokens,
            self.output_tokens + other.output_tokens,
            self.reasoning_output_tokens + other.reasoning_output_tokens,
        )

    @classmethod
    def from_dict(cls, data: dict | None) -> TokenUsage:
        data = data or {}
        return cls(
            int(data.get("input_tokens") or 0),
            int(data.get("cached_input_tokens") or 0),
            int(data.get("output_tokens") or 0),
            int(data.get("reasoning_output_tokens") or 0),
        )


@dataclass
class LimitWindow:
    """One subscription window as Codex reports it."""

    name: str  # "primary" (short, usually 5 h) or "secondary" (long, usually a week)
    used_percent: float
    window_minutes: int | None
    resets_at: int | None  # unix seconds


@dataclass
class LimitSnapshot:
    at: datetime
    windows: list[LimitWindow]
    plan_type: str | None = None
    limit_reached: str | None = None
    has_credits: bool | None = None


@dataclass
class ExecRun:
    """What one `codex exec --json` run consumed."""

    thread_id: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    turns: int = 0
    failed: bool = False
    limit_reached: bool = False  # Codex refused the run: subscription usage limit hit


def _json_lines(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_exec_jsonl(lines: Iterable[str]) -> ExecRun:
    """Sum the usage of a `codex exec --json` stdout stream."""
    run = ExecRun()
    for event in _json_lines(lines):
        kind = event.get("type")
        if kind == "thread.started":
            run.thread_id = event.get("thread_id") or run.thread_id
        elif kind == "turn.completed":
            run.usage = run.usage + TokenUsage.from_dict(event.get("usage"))
            run.turns += 1
        elif kind in ("turn.failed", "error"):
            run.failed = True
            error = event.get("error") if isinstance(event.get("error"), dict) else event
            if "usage limit" in str(error.get("message", "")).lower():
                run.limit_reached = True
    return run


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_rate_limits(data: dict | None, at: datetime) -> LimitSnapshot | None:
    if not data:
        return None
    windows = []
    for name in ("primary", "secondary"):
        w = data.get(name)
        if not w or w.get("used_percent") is None:
            continue
        resets_at = w.get("resets_at")
        if resets_at is None and w.get("resets_in_seconds") is not None:
            # Older Codex builds reported a relative reset time.
            resets_at = int(at.timestamp()) + int(w["resets_in_seconds"])
        windows.append(
            LimitWindow(
                name=name,
                used_percent=float(w["used_percent"]),
                window_minutes=w.get("window_minutes"),
                resets_at=int(resets_at) if resets_at is not None else None,
            )
        )
    if not windows:
        return None
    credits = data.get("credits") or {}
    return LimitSnapshot(
        at=at,
        windows=windows,
        plan_type=data.get("plan_type"),
        limit_reached=data.get("rate_limit_reached_type"),
        has_credits=credits.get("has_credits"),
    )


@dataclass
class SessionLog:
    thread_id: str | None
    path: Path
    usage: TokenUsage  # cumulative for the whole session
    snapshots: list[LimitSnapshot]
    last_event_at: datetime | None


def parse_session_log(path: Path) -> SessionLog:
    """Read a rollout file: final cumulative tokens and every limit snapshot."""
    thread_id = thread_id_from_path(path)
    usage = TokenUsage()
    snapshots: list[LimitSnapshot] = []
    last_at: datetime | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in _json_lines(fh):
            at = _parse_time(line.get("timestamp")) or last_at
            payload = line.get("payload") if isinstance(line.get("payload"), dict) else line
            kind = payload.get("type")
            if line.get("type") == "session_meta":
                thread_id = payload.get("id") or payload.get("session_id") or thread_id
                continue
            if kind != "token_count":
                continue
            last_at = at or last_at
            info = payload.get("info") or {}
            if info.get("total_token_usage"):
                usage = TokenUsage.from_dict(info["total_token_usage"])
            if at is not None:
                snap = parse_rate_limits(payload.get("rate_limits"), at)
                if snap:
                    snapshots.append(snap)
    return SessionLog(thread_id, path, usage, snapshots, last_at)


def thread_id_from_path(path: Path) -> str | None:
    # rollout-2026-09-25T11-24-36-<uuid>.jsonl -> <uuid> (a uuid has 5 dash groups)
    stem = path.stem
    if not stem.startswith("rollout-"):
        return None
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 11 else None


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def iter_session_logs(root: Path | None = None, modified_after: float = 0) -> Iterator[Path]:
    sessions = (root or codex_home()) / "sessions"
    if not sessions.is_dir():
        return
    for path in sorted(sessions.rglob("rollout-*.jsonl")):
        try:
            if path.stat().st_mtime > modified_after:
                yield path
        except OSError:
            continue


def find_session_log(thread_id: str, root: Path | None = None) -> Path | None:
    for path in iter_session_logs(root):
        if path.stem.endswith(thread_id):
            return path
    return None
