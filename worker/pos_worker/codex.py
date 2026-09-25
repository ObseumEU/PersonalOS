"""Streams a `codex exec --json` session and can continue it with `resume`.

`codex exec` is one-shot. To let an agent take in new information mid-run, the
worker stops the process at a safe point (after a completed item) and
continues the same conversation with `codex exec resume <thread_id>`.
"""

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field


def kill_tree(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


@dataclass
class CodexSession:
    binary: str = "codex"
    workdir: str = "."
    sandbox: str = "workspace-write"
    config: list[str] = field(default_factory=list)  # extra -c key=value overrides
    env: dict[str, str] | None = None
    thread_id: str | None = None
    lines: list[str] = field(default_factory=list)  # every --json line, for token accounting
    proc: subprocess.Popen | None = None
    last_message: str = ""
    failed: str = ""

    def _args(self, resume: bool) -> list[str]:
        args = [self.binary, "exec", "--json", "--skip-git-repo-check", "-C", self.workdir, "-s", self.sandbox]
        for c in self.config:
            args += ["-c", c]
        if resume and self.thread_id:
            args += ["resume", self.thread_id]
        return [*args, "-"]  # the prompt comes on stdin

    def run(self, prompt: str) -> Iterator[dict]:
        """Start (or resume) the session and yield its events as they arrive."""
        resume = self.thread_id is not None
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        env = {**os.environ, **(self.env or {}), "PYTHONUTF8": "1"}
        self.proc = subprocess.Popen(self._args(resume), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env, **kw)
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(prompt)
        self.proc.stdin.close()
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            self.lines.append(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "thread.started" and ev.get("thread_id"):
                self.thread_id = ev["thread_id"]
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                self.last_message = item.get("text", "")
            if ev.get("type") in ("turn.failed", "error"):
                self.failed = (ev.get("error") or {}).get("message") or ev.get("message") or "failed"
            yield ev
        self.proc.wait()
        if self.proc.returncode not in (0, None) and not self.failed:
            self.failed = (self.proc.stderr.read() if self.proc.stderr else "")[-1000:] or f"exit {self.proc.returncode}"

    def stop(self) -> None:
        """Stop at the current point; the conversation can be resumed later."""
        if self.proc and self.proc.poll() is None:
            kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    @property
    def jsonl(self) -> str:
        return "\n".join(self.lines)
