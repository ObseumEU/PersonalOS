"""Run one agent as a Codex worker.

    POS_URL=http://api:8000 POS_AGENT_KEY=pos_… python -m pos_worker

Environment:
    POS_URL          PersonalOS API (inside compose: http://api:8000)
    POS_MCP_URL      MCP endpoint the agent's Codex uses (default POS_URL + /mcp)
    POS_AGENT_KEY    the agent's API key (shown once when the agent is created)
    WORKER_WORKDIR   where Codex works (default /work)
    WORKER_SANDBOX   codex sandbox mode (default workspace-write)
    WORKER_POLL      long-poll seconds (default 60)
    CODEX_BIN        codex binary (default codex); CODEX_HOME holds its login
    CLAUDE_BIN       claude binary (default claude); logged in, or CLAUDE_CODE_OAUTH_TOKEN
    WORKER_CLAUDE_TOOLS  tools a Claude agent may use (default: pos MCP, read, edit, web);
                     entries separated by '|' when they contain spaces
    WORKER_CLAUDE_BUILTIN  built-in tools that exist at all, comma-separated (e.g. Bash,Read,Edit)
    WORKER_CLAUDE_DISALLOWED  tools hidden from a Claude agent (saves their definitions on every turn)
    WORKER_CLAUDE_EFFORT     Claude effort level (low, medium, high, xhigh, max)
    WORKER_CLAUDE_MAX_USD    Claude cost cap per run (--max-budget-usd)
    WORKER_MAX_STEPS     stop a run after this many completed steps and hand the task back (0 = no cap)
    WORKER_CLAUDE_MCP    more MCP servers for Claude, as JSON
    WORKER_TOOLS_DIR PersonalOS checkout with agents/*/tools and shared/tools (default: WORKER_WORKDIR)
    WORKER_CODEX_CONFIG  extra `-c key=value` lines: more MCP servers (knowlage ingest,
                     GitHub, Gmail, Discord) with their own tokens in env vars
"""

import logging
import os
import sys

from .claude import DEFAULT_TOOLS, ClaudeSession
from .client import PosClient
from .codex import CodexSession
from .loop import Worker
from . import tools as tool_library


def extra_config() -> list[str]:
    """More MCP servers or Codex settings for this agent, one `key=value` per line
    in WORKER_CODEX_CONFIG. Example for the Mail agent (knowlage ingest):

        mcp_servers.knowlage.url="https://knowlage.example/ingest/mcp"
        mcp_servers.knowlage.bearer_token_env_var="KB_AGENT_KEY"
    """
    raw = os.environ.get("WORKER_CODEX_CONFIG", "").replace("||", chr(10))
    return [line.strip() for line in raw.splitlines() if line.strip()]


def tool_list(raw: str) -> list[str]:
    """Split an allow-list on '|' when given (entries like "Bash(git commit:*)"
    contain spaces), else on whitespace."""
    parts = raw.split("|") if "|" in raw else raw.split()
    return [p.strip() for p in parts if p.strip()]


def claude_extra_mcp() -> dict:
    """More MCP servers for a Claude agent as JSON in WORKER_CLAUDE_MCP, e.g.
    {"knowlage": {"type": "http", "url": "https://…/ingest/mcp", "headers": {"Authorization": "Bearer …"}}}."""
    import json

    raw = os.environ.get("WORKER_CLAUDE_MCP", "").strip()
    return json.loads(raw) if raw else {}


def main() -> None:
    if os.environ.get("POS_CHILD_PIDFILE"):  # the real interpreter pid (a venv python.exe is only a launcher)
        open(os.environ["POS_CHILD_PIDFILE"], "w").write(str(os.getpid()))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    url = os.environ.get("POS_URL", "http://localhost:8000")
    key = os.environ["POS_AGENT_KEY"]
    mcp_url = os.environ.get("POS_MCP_URL", url.rstrip("/") + "/mcp")
    workdir = os.environ.get("WORKER_WORKDIR", "/work")
    os.makedirs(workdir, exist_ok=True)

    def browser(me: dict) -> dict:
        """The guarded browser (pos_worker.browser_guard) for agents with browser:use."""
        if "browser:use" not in (me.get("permissions") or []):
            return {}
        return {"browser": {"type": "stdio", "command": sys.executable, "args": ["-m", "pos_worker.browser_guard"],
                            "env": {"POS_URL": url, "POS_AGENT_KEY": key,
                                    **{k: v for k, v in os.environ.items() if k.startswith(("BROWSER_", "PLAYWRIGHT"))}}}}

    def new_session(engine: str, model: str | None, me: dict):
        tools = me.get("tools") or []  # the tool library: skills, MCP tools, scripts
        if engine == "claude":
            skills = tool_library.skills_text(tools)
            allowed = tool_list(os.environ.get("WORKER_CLAUDE_TOOLS", DEFAULT_TOOLS))
            return ClaudeSession(
                binary=os.environ.get("CLAUDE_BIN", "claude"),
                workdir=workdir,
                model=model,
                system_prompt=me.get("guardrails", "") + (f"\n\n{skills}" if skills else ""),
                # The agent reaches PersonalOS through the pos MCP server, as itself.
                mcp_servers={"pos": {"type": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {key}"}},
                             **browser(me), **claude_extra_mcp(), **tool_library.claude_servers(tools)},
                allowed_tools=allowed + tool_library.claude_allowed(tools)
                + (["mcp__browser"] if browser(me) and allowed else []),
                builtin_tools=[t for t in os.environ.get("WORKER_CLAUDE_BUILTIN", "").split(",") if t],
                disallowed_tools=tool_list(os.environ.get("WORKER_CLAUDE_DISALLOWED", "")),
                effort=os.environ.get("WORKER_CLAUDE_EFFORT") or None,
                max_budget_usd=float(os.environ.get("WORKER_CLAUDE_MAX_USD") or 0) or None,
            )
        return CodexSession(
            binary=os.environ.get("CODEX_BIN", "codex"),
            workdir=workdir,
            sandbox=os.environ.get("WORKER_SANDBOX", "workspace-write"),
            config=[f'mcp_servers.pos.url="{mcp_url}"', 'mcp_servers.pos.bearer_token_env_var="POS_AGENT_KEY"',
                    *([f'model="{model}"'] if model else []), *extra_config(), *tool_library.codex_config(tools),
                    *([f'mcp_servers.browser.command="{sys.executable.replace(chr(92), "/")}"',
                       'mcp_servers.browser.args=["-m","pos_worker.browser_guard"]',
                       'mcp_servers.browser.env_vars=["POS_URL","POS_AGENT_KEY","BROWSER_CDP","BROWSER_ALLOW",'
                       '"BROWSER_HEADED","BROWSER_MAX_MINUTES","PLAYWRIGHT_MCP"]'] if browser(me) else [])],
        )

    Worker(PosClient(url, key), new_session, poll_wait=int(os.environ.get("WORKER_POLL", "60")),
           max_steps=int(os.environ.get("WORKER_MAX_STEPS") or 0),
           tools_dir=str(tool_library.tools_root(workdir))).run_forever()


if __name__ == "__main__":
    main()
