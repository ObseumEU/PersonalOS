"""Streams a headless Claude Code session (`claude -p --output-format
stream-json`) with the same interface as CodexSession, so the worker loop can
use either runtime.

- Safe points: every completed tool call (a `tool_result` in a `user` event)
  is reported as `item.completed`, like Codex's items.
- Mid-run messages: stop at a safe point and continue the same conversation
  with `claude -p --resume <session_id>`.
- The constitution and guardrails go in with `--append-system-prompt`; the
  `pos` MCP server is configured with the agent's own key; tools are limited
  to an allow-list (no blanket permission bypass).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .codex import kill_tree

def resolve_binary(name: str) -> str:
    """On Windows npm installs `claude.cmd`, which only starts a native
    claude.exe. Calling the exe directly keeps arguments intact (cmd.exe mangles
    multi-line or quoted arguments)."""
    import re

    path = shutil.which(name) or name
    if sys.platform == "win32" and path.lower().endswith(".cmd"):
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return path
        if m := re.search(r'"%dp0%\\([^"]+\.exe)"', text):
            exe = Path(path).parent / m[1]
            if exe.exists():
                return str(exe)
    return path


# Tools a Claude agent may use without asking. `mcp__pos` = every tool of the
# pos MCP server (the agent's permissions there are enforced by PersonalOS).
DEFAULT_TOOLS = "mcp__pos Read Glob Grep Write Edit WebSearch WebFetch"


@dataclass
class ClaudeSession:
    binary: str = "claude"
    workdir: str = "."
    model: str | None = "claude-opus-5-5"
    system_prompt: str = ""
    mcp_servers: dict = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=lambda: DEFAULT_TOOLS.split())
    # --restricted: no command-running tools unless named, no user/project settings;
    # file tools stay inside the working directory.
    restricted: bool = True
    # Built-in tools that exist at all (--tools); in restricted mode Bash only
    # exists when named here, and then only the allow-listed commands run.
    builtin_tools: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    thread_id: str | None = None  # the Claude session id, for --resume
    lines: list[str] = field(default_factory=list)
    proc: subprocess.Popen | None = None
    last_message: str = ""
    failed: str = ""

    def _mcp_file(self) -> str | None:
        if not self.mcp_servers:
            return None
        fd, path = tempfile.mkstemp(prefix="pos-mcp-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": self.mcp_servers}, f)
        return path

    def _args(self, mcp_file: str | None, prompt_file: str | None = None) -> list[str]:
        binary = resolve_binary(self.binary)
        args = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        if self.restricted:
            args.append("--restricted")
        if self.model:
            args += ["--model", self.model]
        if prompt_file:
            args += ["--append-system-prompt-file", prompt_file]
        if mcp_file:
            args += ["--mcp-config", mcp_file, "--strict-mcp-config"]
        if self.builtin_tools:
            args += ["--tools", ",".join(self.builtin_tools)]
        if self.allowed_tools:
            args += ["--allowedTools", *self.allowed_tools]
        if self.thread_id:
            args += ["--resume", self.thread_id]
        return args

    def _prompt_file(self) -> str | None:
        if not self.system_prompt:
            return None
        fd, path = tempfile.mkstemp(prefix="pos-rules-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.system_prompt)
        return path

    def run(self, prompt: str) -> Iterator[dict]:
        mcp_file = self._mcp_file()
        prompt_file = self._prompt_file()
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        env = {**os.environ, **(self.env or {}), "PYTHONUTF8": "1"}
        try:
            self.proc = subprocess.Popen(self._args(mcp_file, prompt_file), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=self.workdir,
                                         env=env, **kw)
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
                if ev.get("session_id"):
                    self.thread_id = ev["session_id"]
                if not isinstance(ev, dict):
                    continue
                t = ev.get("type")
                msg = ev.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    content = []
                if t == "assistant":
                    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    if any(texts):
                        self.last_message = "\n".join(x for x in texts if x)
                    yield {"type": "agent_message", "raw": ev}
                elif t == "user" and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                    yield {"type": "item.completed", "item": {"type": "tool_result"}, "raw": ev}
                elif t == "result":
                    if ev.get("is_error"):
                        self.failed = str(ev.get("result") or ev.get("subtype") or "failed")
                    elif ev.get("result"):
                        self.last_message = str(ev["result"])
                    yield {"type": "turn.completed", "raw": ev}
                else:
                    yield {"type": t or "event", "raw": ev}
            self.proc.wait()
            if self.proc.returncode not in (0, None) and not self.failed:
                self.failed = (self.proc.stderr.read() if self.proc.stderr else "")[-1000:] or f"exit {self.proc.returncode}"
        finally:
            for f in (mcp_file, prompt_file):
                if f:
                    try:
                        os.unlink(f)
                    except OSError:
                        pass

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    @property
    def jsonl(self) -> str:
        return "\n".join(self.lines)
