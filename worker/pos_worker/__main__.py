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
    WORKER_CLAUDE_TOOLS  tools a Claude agent may use (default: pos MCP, read, edit, web)
    WORKER_CLAUDE_MCP    more MCP servers for Claude, as JSON
    WORKER_CODEX_CONFIG  extra `-c key=value` lines: more MCP servers (knowlage ingest,
                     GitHub, Gmail, Discord) with their own tokens in env vars
"""

import logging
import os

from .claude import DEFAULT_TOOLS, ClaudeSession
from .client import PosClient
from .codex import CodexSession
from .loop import Worker


def extra_config() -> list[str]:
    """More MCP servers or Codex settings for this agent, one `key=value` per line
    in WORKER_CODEX_CONFIG. Example for the Mail agent (knowlage ingest):

        mcp_servers.knowlage.url="https://knowlage.example/ingest/mcp"
        mcp_servers.knowlage.bearer_token_env_var="KB_AGENT_KEY"
    """
    raw = os.environ.get("WORKER_CODEX_CONFIG", "").replace("||", chr(10))
    return [line.strip() for line in raw.splitlines() if line.strip()]


def claude_extra_mcp() -> dict:
    """More MCP servers for a Claude agent as JSON in WORKER_CLAUDE_MCP, e.g.
    {"knowlage": {"type": "http", "url": "https://…/ingest/mcp", "headers": {"Authorization": "Bearer …"}}}."""
    import json

    raw = os.environ.get("WORKER_CLAUDE_MCP", "").strip()
    return json.loads(raw) if raw else {}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    url = os.environ.get("POS_URL", "http://localhost:8000")
    key = os.environ["POS_AGENT_KEY"]
    mcp_url = os.environ.get("POS_MCP_URL", url.rstrip("/") + "/mcp")
    workdir = os.environ.get("WORKER_WORKDIR", "/work")
    os.makedirs(workdir, exist_ok=True)

    def new_session(engine: str, model: str | None, me: dict):
        if engine == "claude":
            return ClaudeSession(
                binary=os.environ.get("CLAUDE_BIN", "claude"),
                workdir=workdir,
                model=model,
                system_prompt=me.get("guardrails", ""),
                # The agent reaches PersonalOS through the pos MCP server, as itself.
                mcp_servers={"pos": {"type": "http", "url": mcp_url, "headers": {"Authorization": f"Bearer {key}"}},
                             **claude_extra_mcp()},
                allowed_tools=os.environ.get("WORKER_CLAUDE_TOOLS", DEFAULT_TOOLS).split(),
            )
        return CodexSession(
            binary=os.environ.get("CODEX_BIN", "codex"),
            workdir=workdir,
            sandbox=os.environ.get("WORKER_SANDBOX", "workspace-write"),
            config=[f'mcp_servers.pos.url="{mcp_url}"', 'mcp_servers.pos.bearer_token_env_var="POS_AGENT_KEY"',
                    *([f'model="{model}"'] if model else []), *extra_config()],
        )

    Worker(PosClient(url, key), new_session, poll_wait=int(os.environ.get("WORKER_POLL", "60"))).run_forever()


if __name__ == "__main__":
    main()
