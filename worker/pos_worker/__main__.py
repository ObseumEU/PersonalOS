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
    WORKER_CODEX_CONFIG  extra `-c key=value` lines: more MCP servers (knowlage ingest,
                     GitHub, Gmail, Discord) with their own tokens in env vars
"""

import logging
import os

from .client import PosClient
from .codex import CodexSession
from .loop import Worker


def extra_config() -> list[str]:
    """More MCP servers or Codex settings for this agent, one `key=value` per line
    in WORKER_CODEX_CONFIG. Example for the Mail agent (knowlage ingest):

        mcp_servers.knowlage.url="https://knowlage.example/ingest/mcp"
        mcp_servers.knowlage.bearer_token_env_var="KB_AGENT_KEY"
    """
    raw = os.environ.get("WORKER_CODEX_CONFIG", "").replace("||", "
")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    url = os.environ.get("POS_URL", "http://localhost:8000")
    key = os.environ["POS_AGENT_KEY"]
    mcp_url = os.environ.get("POS_MCP_URL", url.rstrip("/") + "/mcp")
    workdir = os.environ.get("WORKER_WORKDIR", "/work")
    os.makedirs(workdir, exist_ok=True)

    def new_session() -> CodexSession:
        return CodexSession(
            binary=os.environ.get("CODEX_BIN", "codex"),
            workdir=workdir,
            sandbox=os.environ.get("WORKER_SANDBOX", "workspace-write"),
            # The agent reaches PersonalOS through the pos MCP server, as itself.
            config=[f'mcp_servers.pos.url="{mcp_url}"', 'mcp_servers.pos.bearer_token_env_var="POS_AGENT_KEY"',
                    *extra_config()],
        )

    Worker(PosClient(url, key), new_session, poll_wait=int(os.environ.get("WORKER_POLL", "60"))).run_forever()


if __name__ == "__main__":
    main()
