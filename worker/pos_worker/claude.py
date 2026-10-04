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

# The built-in tools an agent gets when its profile names none (--tools always names them: the
# CLI's default set adds ~20 tools agents never use, e.g. Agent/Task, Skill, Cron*, worktrees).
DEFAULT_BUILTIN = ("Read", "Glob", "Grep", "Write", "Edit", "WebSearch", "WebFetch")
# Never given to an agent, even when a profile names them: sub-agents, skills and the CLI's own
# to-do list (PersonalOS has tasks), whose descriptions and listings cost ~2k tokens on every turn.
NEVER_BUILTIN = ("Agent", "Task", "Skill", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
                 "TaskStop", "SendMessage", "ListAgents", "EnterWorktree", "ExitWorktree", "CronCreate",
                 "CronDelete", "CronList", "ScheduleWakeup", "PushNotification", "NotebookEdit")
# Up to this many tools (pos and others) their schemas go in upfront (ENABLE_TOOL_SEARCH=false); more,
# and the CLI defers them behind ToolSearch (names only; one round trip loads the schemas of the tools
# first used). ToolSearch exists only when --tools names it: without it every MCP schema goes in
# upfront (the Kniha agents' 57k-token prompt before 2026-10). Measured in the pool image (Claude Code
# 2.1.287) on the Access manager's 29 pos tools: 22.2k tokens upfront vs 9.3k deferred (~440 tokens a
# schema), so only a very small set goes in upfront. WORKER_TOOL_SEARCH_ABOVE overrides it.
TOOL_SEARCH_ABOVE = 12


def builtin_set(configured: list[str], disallowed: list[str], tool_count: int) -> tuple[list[str], bool]:
    """(the --tools list, tool search on?) for an agent: its profile's built-ins (else DEFAULT_BUILTIN),
    without the disallowed and NEVER_BUILTIN ones, plus ToolSearch when it has more than
    TOOL_SEARCH_ABOVE tools in all."""
    base = [t for t in (configured or DEFAULT_BUILTIN) if t not in disallowed and t not in NEVER_BUILTIN
            and t != "ToolSearch"]
    above = int(os.environ.get("WORKER_TOOL_SEARCH_ABOVE") or TOOL_SEARCH_ABOVE)
    search = tool_count + len(base) > above
    return base + (["ToolSearch"] if search else []), search


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
    # None = the CLI's default set (only for direct use; the worker always names them, builtin_set).
    builtin_tools: list[str] | None = None
    # ToolSearch (deferred MCP schemas) on or off; None = the CLI's default.
    tool_search: bool | None = None
    # Tools removed from the model's context altogether (e.g. pos MCP tools the
    # agent never needs); every listed tool definition costs tokens on every turn.
    disallowed_tools: list[str] = field(default_factory=list)
    effort: str | None = None  # low | medium | high | xhigh | max (--effort)
    max_budget_usd: float | None = None  # per-run cap (--max-budget-usd)
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

    def _settings_file(self) -> str | None:
        """Bash exists for this agent: every command goes through PersonalOS's
        command guard first (pos_worker.command_hook, a PreToolUse hook)."""
        if "Bash" not in (self.builtin_tools or []):
            return None
        from .command_hook import settings

        fd, path = tempfile.mkstemp(prefix="pos-settings-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings(), f)
        return path

    def _args(self, mcp_file: str | None, prompt_file: str | None = None, settings_file: str | None = None) -> list[str]:
        binary = resolve_binary(self.binary)
        args = [binary, "-p", "--output-format", "stream-json", "--verbose"]
        if self.restricted:
            args.append("--restricted")
        if settings_file:
            args += ["--settings", settings_file]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if self.max_budget_usd:
            args += ["--max-budget-usd", str(self.max_budget_usd)]
        if prompt_file:
            args += ["--append-system-prompt-file", prompt_file]
        if mcp_file:
            args += ["--mcp-config", mcp_file, "--strict-mcp-config"]
        if self.builtin_tools is not None:  # "" = no built-in tool at all
            args += ["--tools", ",".join(self.builtin_tools)]
            # No skills: their listing (~1.7k tokens a turn) is for people, agents have the tool library.
            args.append("--disable-slash-commands")
        if self.allowed_tools:
            args += ["--allowedTools", *self.allowed_tools]
        if self.disallowed_tools:
            args += ["--disallowedTools", *self.disallowed_tools]
        if self.thread_id:
            args += ["--resume", self.thread_id]
        return args

    def cli_env(self) -> dict[str, str]:
        """Environment for the CLI that keeps the fixed prompt small: tool search as decided, and no
        auto-memory (agents keep memory in PersonalOS, memory_get/memory_update)."""
        out = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}
        if self.tool_search is not None:
            out["ENABLE_TOOL_SEARCH"] = "true" if self.tool_search else "false"
        return out

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
        settings_file = self._settings_file()
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        # MCP_TOOL_TIMEOUT: a sandbox command may run up to an hour (sandbox_exec timeout ≤ 3600 s).
        env = {"MCP_TOOL_TIMEOUT": "3700000", **os.environ, **(self.env or {}), **self.cli_env(), "PYTHONUTF8": "1"}
        try:
            self.proc = subprocess.Popen(self._args(mcp_file, prompt_file, settings_file), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
            for f in (mcp_file, prompt_file, settings_file):
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
